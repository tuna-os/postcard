# Live validation

The offline suites prove the code against fixtures and fakes; these tests
prove it against the providers. Every file here is marked `live`, which a
bare `pytest` deselects, and every test skips unless its credentials are set —
so a normal run never touches the network, even on a machine that has the
variables exported.

```sh
pytest -m live tests/live/          # everything with credentials present
pytest -m live tests/live/test_tuta_live.py   # one provider
```

## Rules

- Read-only. A live test may sign in, list folders and read pages; it never
  flags, moves, sends or deletes. Anything that writes needs its own review
  and a dedicated test mailbox, not an amendment to these files.
- Dedicated test accounts. Tuta login leaves a server session behind —
  revocation is local-only until the session expires — so never run these
  against a personal mailbox.
- Secrets travel by environment variable only, are never printed, and never
  land in an assertion message. `require_env` skips by variable *name*.

## Variables

| variable | provider | meaning |
|---|---|---|
| `POSTCARD_TUTA_TEST_BINARY` | Tuta | helper binary; else `postcard-tuta` on `PATH` |
| `POSTCARD_TUTA_EMAIL` | Tuta | test account address |
| `POSTCARD_TUTA_PASSWORD` | Tuta | test account password |
| `POSTCARD_TUTA_TOTP` | Tuta | current second-factor code, when the account uses one |
| `POSTCARD_MS_CLIENT_ID` | Microsoft | Entra application (client) id |
| `POSTCARD_MS_REFRESH_TOKEN` | Microsoft | stored refresh token (see below) |
| `POSTCARD_MS_EMAIL` | Microsoft | account address; defaults to `live@example.com` |

## Bootstrapping the Microsoft token

The Microsoft test covers the refresh path, not the first interactive login,
so it needs a refresh token minted once up front: register the Entra app,
complete one interactive sign-in with the same client id and scopes the app
requests (`Mail.ReadWrite`, `Mail.Send`, `offline_access`), then store the
refresh token with `ms_oauth.encode_stored` and export that JSON as
`POSTCARD_MS_REFRESH_TOKEN`. When the app's Microsoft dialog lands behind a
registered client id, that dialog is the mint; until then any standards-
compliant OAuth client with those scopes produces an equivalent token.

## Provider status

- Tuta: login plus a one-page sync through the compiled helper.
- Microsoft: refresh plus a one-page folder read through Graph.
- Proton: no live test yet — the helper is still the `unsupported_operation`
  stub, and there is nothing account-side to validate. It lands with the real
  helper, following this directory's rules.
