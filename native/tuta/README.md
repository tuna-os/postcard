# Experimental native Tuta backend

Developer prototype for [#1](https://github.com/tuna-os/postcard/issues/1).
This is **not usable Tuta account support in a released Postcard build**.
The Add Account dialog is deliberately unchanged. No live account has been
used to validate these operations.

## Architecture and dependencies

Postcard worker -> one private stdin/stdout request -> `postcard-tuta` -> Tuta
HTTPS API. The helper runs to completion and exits. It does not listen on a
socket, launch a bridge, speak IMAP/SMTP/JMAP, or write a local cache.
Reconstructed MIME is an internal input to Postcard's existing message reader,
not an IMAP connection.

The pinned `upstream` submodule is **source reuse only**:

- TutaBridge `da12fd4a479d6885f6643f05690b78205e2adbec` supplies its
  `tutabridge-tuta` native mail extensions and verified SDK preparation script.
  Neither `tutabridge-core` nor its IMAP/SMTP server is linked or run.
- The official SDK base is Tuta release `362.261020.0`, commit
  `f5e2d8628f17be558fe63130396ff625bbe69c77` from `tutao/tutanota`.
- Its twelve patches cover optional encrypted values, address normalization,
  account KDFs, batched entity loads, blobs, interactive sessions, owner keys,
  AEAD reads and bucket keys. The exact series and hashes are recorded in
  `upstream/sdk/EXPECTED`; the preparation script and native crate build script
  verify the prepared source. Review these patches when updating the pin.
- Both upstream components are GPLv3; the extensions use GPL-3.0-or-later,
  compatible with this fork's GPLv3 distribution. Their license files remain
  in the submodule. No upstream branding/assets are used.

This pins an SDK that may be older than Tuta's minimum accepted client version.
A successful build does not prove server compatibility. Do not spoof a client
version to bypass an incompatibility; update and validate the SDK instead.
Tuta's API is undocumented and third-party integrations are unsupported:
https://tuta.com/support/general . No documented JMAP endpoint was found.

## Build and offline checks

From the repository root, with Rust 1.99.1 and a C compiler available:

```sh
git submodule update --init --recursive
python3 native/tuta/upstream/scripts/sdk-prepare.py
cargo test --locked --manifest-path native/tuta/Cargo.toml
cargo build --locked --manifest-path native/tuta/Cargo.toml
POSTCARD_TUTA_TEST_BINARY="$PWD/native/tuta/target/debug/postcard-tuta" python3 -m pytest
```

The initial build fetches locked dependencies. Offline Flatpak vendoring and
shipping the helper inside the sandbox are still tracked in #6. The normal
Flatpak manifest does not build or install the experimental helper yet.
No host bridge application is needed.

## IPC v1

One UTF-8 JSON object is sent on stdin, followed by EOF; the helper returns
one JSON object on stdout. Passwords, TOTP values and sessions never go in
argv or environment variables. Stderr is discarded by the Python adapter.
Error replies contain a fixed code, not SDK/server exception text.
The helper caps requests at 1 MiB and operations at 90 seconds; the parent
allows 100 seconds including process startup and kills/reaps on timeout.

Requests include `version: 1` and an `op`:

| Operation | Parameters | Result |
| --- | --- | --- |
| `login` | `email`, `password`, optional `totp` | Resumable `session` |
| `sync` | `session`, optional `folder`, `limit`, `offset` | Folder tree, headers, complete selected-folder ID snapshot on offset zero |
| `message` | `session`, compound `id` | Decrypted body, original headers, recipients and attachments |
| `mark_read` | `session`, `ids`, `is_read` | Empty success object |
| `move` | `session`, one-element `ids`, compound `destination` | Empty success object |

Responses are `{"version":1,"result":{...}}` or
`{"version":1,"error":{"code":"..."}}`. Unknown operations and incompatible
versions fail. Sending and starring are unsupported and never fall back to
SMTP or silently disable encryption. Move results do not guess a destination
message ID: Tuta can move a mail to a different underlying list.

`login()` in `core/net/tuta_session.py` returns a `Credential` containing session
material for libsecret. Its repr hides the secret. The GTK setup flow has not
been implemented; this API is not a user login interface.
Do not print the result or store it in a source/fixture file.

Replies are validated where they are consumed: a misshapen sync, header or
body raises `invalid_response` (or `attachment_failed`) rather than leaking
an internal error into the UI. Folder types the helper does not know map to
`other` on both sides, so a newer server-side folder kind stays browsable.
The ID snapshot is type-checked before anything prunes from it: pruning from
a corrupt snapshot would wipe the folder's cache.

## Limits before enabling accounts

- Sessions are resumed once per operation; pooling and interactive challenge
  cancellation are unfinished. A retry can leave a pending login session
  visible in the official client's session list. Revocation is local-only:
  the pinned SDK exposes no logout API, so removing the account clears the
  keyring entry and the server-side session is left to expire.
- Only TOTP is prototyped; WebAuthn/U2F is unsupported.
- Full entry lists are loaded per sync; large-mailbox performance is untested.
  A failed listing is never returned as an authoritative empty snapshot.
- Header lists lack original Message-ID/reply references until a body is read;
  threading parity remains part of #3. Badge counts and event-bus push are also
  unfinished; this backend uses polling.
- Body/attachment paths are implemented against SDK types, but not tested
  against a live account or all legacy/AEAD formats. Unknown legacy bodies
  fail explicitly rather than displaying an empty successful fetch.
- Postcard still caches decrypted mail/metadata/FTS in plaintext SQLite. The
  helper's memory-only file client does not fix that. #5 blocks production use.
- Native sending/drafts, encrypted external delivery and safe ambiguous-send
  recovery remain #4. No downgrade to non-confidential delivery is implemented.

See [ROADMAP.md](../../ROADMAP.md) for the release gates and Proton Mail plan.
