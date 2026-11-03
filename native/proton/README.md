# Proton native helper

`main.go` speaks the same stdio JSON envelope as the
[Tuta helper](../tuta/README.md): one request line on stdin, one reply line
on stdout, protocol version pinned at `1`. `login` is implemented over
`go-proton-api` (SRP, plus a TOTP challenge when the account sets one);
every other known operation still answers `unsupported_operation`, which
`proton_session.py` surfaces as a fixed, UI-safe code — a real account can
never silently get nothing back.

## Protocol

Request: `{"version": 1, "op": "<name>", ...}` (max 64 KiB per line).
Reply: `{"version": 1, "ok": true, ...}` or
`{"version": 1, "error": {"code": "<fixed>"}}`.

| code                  | meaning                                              |
|-----------------------|------------------------------------------------------|
| `unsupported_version` | `version` is present but not `1` — caller must stop |
| `invalid_request`     | malformed JSON, missing field, oversize, unknown op |
| `login_failed`        | wrong password or unknown username                   |
| `second_factor_required` | TOTP challenge, no code given                     |
| `invalid_totp`        | TOTP code malformed                                  |
| `unsupported_second_factor` | only non-TOTP factors available               |
| `second_factor_failed` | TOTP rejected by the server                         |
| `request_failed`      | network fault or unexpected API error                |
| `timeout`             | the 90-second operation budget ran out               |
| `unsupported_operation` | known op (`sync`, `message`, `mark_read`, `move`) not yet implemented |

`login` takes `email`, `password` and an optional `totp`, and returns
`result.session`: `login` (echoed for the identity check), `user_id`, `uid`,
`access_token`, `refresh_token` and `password_mode` (1 or 2, telling sync
whether the mailbox keys need a separate passphrase). Mailbox keys stay
locked until sync first decrypts. The helper identifies to the API as
`Other`, the third-party slot — never as an official client.

A missing `version` is `invalid_request`, not `unsupported_version`: the helper
only negotiates with callers that speak the envelope. Blank lines are skipped.
The reply is always exactly one line.

## Verifying

```sh
cd native/proton
gofmt -l .            # must print nothing
go vet ./...
go test ./...
go build -o postcard-proton .
echo '{"version":1,"op":"sync"}' | ./postcard-proton
```

The Python side exercises a compiled binary without an account — mirroring
the Tuta contract test — when `POSTCARD_PROTON_TEST_BINARY` points at one:

```sh
POSTCARD_PROTON_TEST_BINARY=$PWD/native/proton/postcard-proton \
  python3 -m pytest tests/core/net/test_proton_session.py
```

## Research: what the real helper will use

Surveyed from the module cache (see `proton-native.yml` for the pinned
versions once the helper takes dependencies):

- [`go-proton-api`](https://github.com/ProtonMail/go-proton-api) `v0.4.0`
  (latest) covers the whole roadmap: SRP login + 2FA (`auth.go`),
  `GetMessageMetadataPage` / `CountMessages` / `GetFullMessage` for sync,
  `CreateDraft` + `SendDraft` for sending,
  `LabelMessages` / `MarkMessagesRead` for flags,
  `NewEventStream` for real-time arrive events, and `AuthRevoke` /
  `AuthRevokeAll` for session revocation. It pulls in
  `go-srp v0.0.5` and `gopenpgp/v2 v2.4.10` for its own crypto.
- Unlike Tuta — whose `tuta-sdk` crate has no send API, which is why Tuta
  sending is deferred — Proton sending is a library call, so the helper can
  implement `send` in the same pass as `sync`.

## Roadmap

1. Done: framing + fixed codes + CI.
2. Done: `login` via SRP, session tokens cached in the session blob the way
   the Tuta helper's works. Live validation waits on the test account's
   abuse limitation clearing (see `tests/live/README.md`).
3. `sync` over `GetMessageMetadataPage`, with the event stream driving
   arrive notifications the way IMAP IDLE does. Mailbox keys unlock here,
   on first decrypt — one-password accounts reuse the login password,
   two-password accounts need their mailbox passphrase per operation.
4. `message`, `mark_read`, `move`, `send` (draft + send), session revocation
   via `AuthRevoke` — no local-only gap like Tuta's pinned SDK leaves.
