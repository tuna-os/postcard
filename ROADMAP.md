# Postcard provider roadmap

The Tuna OS fork is exploring encrypted-mail providers while retaining the
GNOME/libadwaita interface. Existing IMAP/SMTP and Microsoft Graph functionality
remain the upstream baseline. Neither Tuta nor Proton is production-ready here.

## Tuta native support — first priority

Use Tuta's native HTTPS APIs and SDK cryptography. Do not require IMAP/SMTP,
a bridge daemon, or assume a JMAP endpoint exists.

| Work | Tracking | State |
| --- | --- | --- |
| Native SDK/helper and Postcard routing | [#1](https://github.com/tuna-os/postcard/issues/1) | Experimental foundation |
| Account setup, resumable sessions and 2FA | [#2](https://github.com/tuna-os/postcard/issues/2) | Planned |
| Complete mailbox sync, history, bodies and attachments | [#3](https://github.com/tuna-os/postcard/issues/3) | Prototype; replies validated, live validation pending |
| Mutations, drafts and encryption-preserving sending | [#4](https://github.com/tuna-os/postcard/issues/4) | Read/move prototype; sending blocked (see below) |
| Local-cache privacy and credential lifecycle | [#5](https://github.com/tuna-os/postcard/issues/5) | Release prerequisite |
| Flatpak, SDK compatibility and end-to-end validation | [#6](https://github.com/tuna-os/postcard/issues/6) | Release prerequisite |

The initial adapter is developer-only. The normal Add Account dialog does not
expose Tuta until authentication, local storage, packaging and encryption
requirements are ready. Do not use a real mailbox to test the prototype against
Postcard's plaintext SQLite/FTS cache without understanding that limitation.

Decisions recorded while building the prototype:

- Every helper reply is validated on the Python side: a misshapen sync, header
  or body raises `invalid_response` (or `attachment_failed`) instead of leaking
  a `KeyError` into the UI, and a folder role neither side knows becomes
  `other` rather than failing the sync.
- Revocation is local-only. The pinned SDK exposes no logout/delete-session
  API, so removing the account clears the session from the keyring and the
  server-side session is left to expire. Revisit if the SDK gains one.
- Sending stays unavailable. The pinned SDK's `MailFacade` has no send or
  draft API; the only send implementation upstream lives in TutaBridge's
  `bridge` crate (`DraftService` + `SendDraftService` with sender/recipient
  resolution and attachment encryption), which this helper deliberately does
  not link. Porting that flow is real crypto work and needs a live account
  to validate, so it waits behind #5 and #6 with the rest of #4.

## Proton Mail native support — next provider

Track research and implementation in
[#7](https://github.com/tuna-os/postcard/issues/7). Evaluate Proton's
[go-proton-api](https://github.com/ProtonMail/go-proton-api),
[gopenpgp](https://github.com/ProtonMail/gopenpgp) and
[go-srp](https://github.com/ProtonMail/go-srp) for a native API adapter with
on-device key handling. Proton Bridge's source can inform protocol behavior;
its IMAP server should not become a requirement for this integration.

Start with authentication, key unlocking and a read-only proof of concept.
Then implement labels/events, attachments, recipient encryption/signature
verification and sending. Apply the same cache, credentials, offline-fixture
and live-validation gates as Tuta. Account eligibility and API compatibility
must be verified rather than inferred from library availability.

The `proton` protocol id is already reserved: Proton account rows fail closed
with a friendly "not supported yet" instead of falling through to IMAP, and
their inbox watcher polls. `proton_session.py` pins the helper IPC contract
the future binary must speak (same stdio shape as Tuta: versioned JSON,
stdin-only secrets, fixed error vocabulary), with the adapter tested against
fixtures. What remains is the helper itself plus reply mapping and routing.

## Native Microsoft 365 — sign in without Online Accounts

Some organizations block GNOME Online Accounts and some desktops lack it, so
Graph accounts should be addable inside Postcard. The protocol half is done in
`core/net/ms_oauth.py`: a public-client authorization-code flow with PKCE over
the stdlib (authorize URL, code exchange, refresh), storing only the refresh
token in the keyring and minting access tokens per operation. The loopback
listener that catches the authorization code is done in
`core/net/ms_loopback.py`: ephemeral 127.0.0.1 port, state validation, fixed
replies that never reflect the code. `core/net/ms_login.py` ties the two
together into one worker-thread call the GTK layer can drive with an injected
browser opener. Mail itself keeps going over the existing Graph modules
unchanged.

What remains, in order:

1. Register an Entra application for Postcard and fill in `MS_CLIENT_ID`. The
   id is public, not a secret — PKCE replaces the client secret a shipped app
   cannot keep.
2. Build the GTK half: call `ms_login.log_in` on a worker thread with a
   main-loop browser opener, store the returned refresh token, and add the
   onboarding UI.
3. Add re-authentication UX for refused refresh tokens, and reword the Graph
   401 message, which currently assumes the token came from Online Accounts.

## Local-cache privacy — the encrypted-at-rest plan

All providers share one SQLite cache, currently plaintext: message bodies,
FTS text, contacts and threading headers. Already in place: the process runs
under umask 077 and every database open re-tightens the cache directory and
database files (including WAL sidecars, which do not inherit the database
file's mode) to owner-only. The full plan, tracked in #5:

- Encrypt the database file with SQLCipher, keyed by a random key generated
  once and held in the keyring beside the account passwords.
- Migrate in place: on first unlock after the upgrade, attach the plaintext
  database and export it into the encrypted one, then remove the original.
- Keep the key out of logs, errors and crash reports the same way session
  material already is; a locked keyring means the app waits, not that it falls
  back to plaintext.

Until that lands, no provider that decrypts server-side encryption on-device
(Tuta first, Proton next) is offered in the Add Account dialog.
