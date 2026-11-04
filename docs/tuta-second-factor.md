# Tuta second factors and sessions (issue #2)

How Postcard signs in to Tuta, which second factors it supports, and why the
ones it does not support are reported by name instead of failing silently.

## Decision: TOTP now, security keys not yet

Tuta offers three second factors: TOTP authenticator codes, legacy U2F keys,
and WebAuthn keys. Postcard supports TOTP only:

- A TOTP code is six digits the user reads off their authenticator app and
  types into the dialog. The login is retried with the password plus the
  code in a single helper invocation, so no challenge state crosses the
  helper boundary and no new IPC was needed.
- U2F and WebAuthn are challenge ceremonies: the server sends a challenge
  that must be signed by the hardware key, which needs either a browser
  (WebAuthn) or a CTAP client channel (U2F) the one-shot stdio helper has
  no access to. Teaching the helper to proxy such a ceremony is a project
  of its own, not a login flag.

Unsupported factors are reported honestly at every layer: the helper
inspects the challenge types and returns `unsupported_u2f` or
`unsupported_webauthn` when the account offers only one kind (a mix, or a
type the helper predates, stays the generic `unsupported_second_factor`),
and the dialog surfaces the message naming the key the account needs. The
account is never left half-added: the failure returns to the form.

Codes rotate every 30 seconds, so a code typed near rotation fails as
`invalid_totp`. The dialog treats that as expiry, not user error, and keeps
the TOTP page up for the current code.

## Session resume: revoked, outage, and refused version

A stored session is resumed by every helper operation, so no dedicated
refresh call exists: after a restart, the first sync already distinguishes
the three states, and nothing else needs to ask.

| Helper error | Meaning | What the UI says |
|---|---|---|
| `session_revoked` | The server rejected the token: revoked, expired, or logged out elsewhere | Remove the account and add it again |
| `session_invalid` | Our stored material is corrupt, not the server's state | Remove the account and add it again |
| `client_outdated` | The server refused the SDK's client version | Update the app |
| `transport_failed` / `timeout` | Outage or no connectivity | Check the connection and retry |
| `resume_failed` | Anything else resume can fail with | Generic retryable failure |

A refused client version is never treated as a bad password: re-authenticating
cannot fix it, only an SDK update can. See `resume_error` in
`native/tuta/src/main.rs` for the mapping from SDK errors.

## Revocation on account removal

Removing a Tuta account closes its session server-side through the helper's
`revoke` operation (the SDK's `CloseSessionService`), then clears the local
keyring entry. Revocation is best-effort and idempotent: an already-gone
session counts as closed, and a failure only logs -- the local credential is
cleared either way, so removal never strands a secret in the keyring.

Only the session is ever stored, in libsecret under the account id; the
password lives in memory for the duration of the login flow and is refused
by `store_tuta_session` if a caller ever mixes the two up.
