package main

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	proton "github.com/ProtonMail/go-proton-api"
)

const loginTimeout = 90 * time.Second

// The API allowlists client identities; "Other" is the third-party slot.
// Impersonating an official client would be dishonest and fragile.
const appVersion = "Other"

type loginParams struct {
	Email    string `json:"email"`
	Password string `json:"password"`
	TOTP     string `json:"totp"`
}

// login proves the SRP credentials and returns the resumable session the
// later operations rebuild their client from. Mailbox keys stay locked: they
// unlock when sync first needs to decrypt, never here.
func login(raw []byte) envelope {
	var params loginParams
	if err := json.Unmarshal(raw, &params); err != nil {
		return failure("invalid_request")
	}
	if params.Email == "" || params.Password == "" {
		return failure("invalid_request")
	}
	ctx, cancel := context.WithTimeout(context.Background(), loginTimeout)
	defer cancel()
	client, auth, err := proton.New(proton.WithAppVersion(appVersion)).NewClientWithLogin(ctx, params.Email, []byte(params.Password))
	if err != nil {
		return failure(loginFailure(err))
	}
	defer client.Close()
	if code, ok := secondFactor(ctx, client, auth, params.TOTP); !ok {
		return failure(code)
	}
	return envelope{Version: protocolVersion, Result: map[string]any{
		"session": map[string]any{
			"login":         params.Email,
			"user_id":       auth.UserID,
			"uid":           auth.UID,
			"access_token":  auth.AccessToken,
			"refresh_token": auth.RefreshToken,
			"password_mode": int(auth.PasswordMode),
		},
	}}
}

// secondFactor completes a TOTP challenge. It reports ok unless the account
// needs a code it was not given, cannot use, or rejected.
func secondFactor(ctx context.Context, client *proton.Client, auth proton.Auth, totp string) (string, bool) {
	if auth.TwoFA.Enabled == 0 {
		return "", true
	}
	if auth.TwoFA.Enabled&proton.HasTOTP == 0 {
		return "unsupported_second_factor", false
	}
	if totp == "" {
		return "second_factor_required", false
	}
	if len(totp) != 6 || !allDigits(totp) {
		return "invalid_totp", false
	}
	if err := client.Auth2FA(ctx, proton.Auth2FAReq{TwoFactorCode: totp}); err != nil {
		return "second_factor_failed", false
	}
	return "", true
}

func allDigits(value string) bool {
	for i := 0; i < len(value); i++ {
		if value[i] < '0' || value[i] > '9' {
			return false
		}
	}
	return true
}

// loginFailure keeps the fixed vocabulary: credential rejections read as
// login_failed, an expired context as timeout, and everything else —
// network faults, unexpected API errors — as request_failed. Server text
// never leaves this function.
func loginFailure(err error) string {
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return "timeout"
	}
	var apiErr *proton.APIError
	if errors.As(err, &apiErr) {
		switch apiErr.Code {
		case proton.PasswordWrong, proton.UsernameInvalid:
			return "login_failed"
		}
	}
	return "request_failed"
}
