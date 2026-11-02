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
| Complete mailbox sync, history, bodies and attachments | [#3](https://github.com/tuna-os/postcard/issues/3) | Prototype; live validation pending |
| Mutations, drafts and encryption-preserving sending | [#4](https://github.com/tuna-os/postcard/issues/4) | Read/move prototype; sending unavailable |
| Local-cache privacy and credential lifecycle | [#5](https://github.com/tuna-os/postcard/issues/5) | Release prerequisite |
| Flatpak, SDK compatibility and end-to-end validation | [#6](https://github.com/tuna-os/postcard/issues/6) | Release prerequisite |

The initial adapter is developer-only. The normal Add Account dialog does not
expose Tuta until authentication, local storage, packaging and encryption
requirements are ready. Do not use a real mailbox to test the prototype against
Postcard's plaintext SQLite/FTS cache without understanding that limitation.

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
