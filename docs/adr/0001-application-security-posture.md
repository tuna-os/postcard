# ADR 0001: Application security posture

- Status: accepted
- Date: 2026-11-04
- Deciders: maintainer
- Context: the project goal is an email client that is *very* secure without
  compromising ease of use or clean GNOME design. This record sets the threat
  model, surveys the available mechanisms (passwords, OAuth, passkeys,
  biometrics, message encryption, storage encryption), and decides what the
  app does about each — including what it deliberately does *not* do, and what
  would change that.

## Threat model

The app defends, in priority order:

1. **A stolen or lost device, powered off.** Full-disk encryption is the
   owner's job; the app adds its own layer so a disk image without the login
   secret yields no mail: SQLCipher cache + keyring-held key, owner-only file
   modes, no plaintext sidecars.
2. **A network adversary.** TLS verification everywhere, OAuth instead of
   reusable passwords, no credential in logs/errors/UI, per-provider error
   vocabularies that never carry server text.
3. **Malicious mail content.** WebKit with scripts-never-run, remote images
   off by default, attachments opened from owner-only spools, MIME parsing
   bounded and off the main loop.
4. **Opportunistic access on a logged-in session** (shoulder surfing, an
   unattended unlocked desktop). App-level lock on session lock/suspend.
   This is explicitly *not* a cryptographic boundary: the session owner can
   always read their own files.
5. **A curious or compromised mail server.** Only end-to-end encryption
   answers this; see below. Nothing else in the app claims to.

Out of scope: protecting a running session's owner from themselves
(forensic memory/disk analysis of the owner's own processes), anonymity
metadata (headers necessarily leak correspondents), and server-side account
takeover beyond pushing users to provider 2FA.

## What already exists

- Passwords and sessions in the system keyring via libsecret, never in the
  database; resumable sessions per mechanism; fail-closed `CacheError` /
  `TutaError` / `ProtonError` vocabularies; no secret ever logged or toasted.
- SQLCipher cache with a 32-byte random key generated into the keyring,
  verified plaintext migration, 0600/0700 modes, wrong-key/damage fails
  closed.
- TLS verify-by-default with a documented loopback exception for local
  bridges (ProtonMail Bridge, hydroxide).
- OAuth where providers demand it: Microsoft loopback flow, Fastmail JMAP
  OAuth; GOA import for Google/Microsoft OAuth.
- TOTP for Tuta and Proton; refusal to silently downgrade (no plaintext
  fallback anywhere).

## Decisions

### 1. Passwords: OAuth first, app passwords second, typed passwords last

Basic authentication is dead at the majors: Gmail ended it in May 2025,
Outlook/Exchange Online in April 2026. What remains:

- **OAuth 2.0 (XOAUTH2)** for Gmail, Microsoft 365, Fastmail. This is #29,
  and it is now a *functional* requirement, not just hardening; without it
  Gmail/Microsoft accounts cannot sign in at all.
- **App passwords** as the documented fallback where providers issue them
  (Google 16-character passwords with 2-Step Verification on). The UI must
  say so explicitly when a password is rejected, instead of a generic
  "wrong password".
- **Typed account passwords** only for providers that still take them
  (smaller IMAP hosts), stored in the keyring exactly as today.

Gmail OAuth has a human cost: the `https://mail.google.com/` scope needs
Google verification plus a CASA security assessment. That process, plus
Microsoft publisher verification, is tracked as its own issue because no
agent can do it; #20 (distribution identity) is its prerequisite.

### 2. Passkeys: they arrive through the browser, not the app

Linux has no platform authenticator (Chrome's
`isUserVerifyingPlatformAuthenticatorAvailable()` returns false; GNOME 51
adds passkey *login* at GDM, not an app API), and a Flatpak cannot reach
FIDO2 hardware without `--device=all`, which blows a hole in the sandbox.
The credentials portal (`xdg-credentials-portal`/`credentialsd`) that would
fix this is experimental as of FOSDEM 2026.

So the app will not implement native WebAuthn enrollment/ceremonies. Users
still get passkeys where they matter: every OAuth flow opens in the system
browser, where platform/browser passkeys work normally. The app holds only
the resulting tokens in the keyring. Revisit when a credentials portal
ships in the GNOME runtime; the Tuta U2F stance in
`docs/tuta-second-factor.md` already follows this rule.

### 3. Biometrics: convenience gate only, never a security boundary

`fprintd` verification is callable over D-Bus by the session user for their
own enrolled prints, so an in-app "fingerprint to unlock" is feasible with
one `--talk-name=net.reactivated.Fprint`. But the Linux fingerprint stack
is convenience, not security: no liveness guarantees on most readers,
templates replayable in principle, and it cannot gate anything the session
owner can read directly.

Decision: an *optional* app lock may offer fingerprint unlock as a faster
alternative to retyping, presented honestly as a casual-access gate. It
never encrypts, never replaces the keyring, and never gates destructive
actions. Fingerprint-only authentication (no fallback) is rejected: readers
fail and fingers change.

### 4. Message encryption: Autocrypt L1 + WKD over a Sequoia helper

The ecosystem converged while nobody watched: OpenPGP is RFC 9580 (2024)
with post-quantum algorithms in RFC 9980 (July 2026), key discovery works
through WKD (provider-published) and Autocrypt gossip (in-band), and Delta
Chat proves the UX can be invisible. Thunderbird/RNP is the reference
implementation.

For this app the stages are:

1. **Autocrypt Level 1 + WKD lookup** (this decision): attach our key to
   outgoing mail, harvest peers' keys from headers, fetch WKD keys before
   sending, encrypt opportunistically with an honest per-recipient
   indicator. No web-of-trust UI in stage 1; gossip keys are TOFU and the
   UI must not over-claim.
2. Signatures and verification display.
3. Key management UI (import/export/revoke), Autocrypt v2 if it stabilises.

Implementation follows the project's native-helper pattern: a small
Sequoia-OpenPGP (Rust, RFC 9580) bridge binary speaking the same JSON-over-
stdin IPC as the Tuta/Proton helpers. Rationale: no `gpg` subprocess
robustness traps (the reason Thunderbird moved to RNP), memory-safe key
handling the Python side cannot provide, and supply-chain practice
(cargo-sources.json, deny.toml) already established. GPGME/python-gnupg are
rejected for the same robustness reasons.

Tuta and Proton mail stays inside their own cryptosystems end to end; the
OpenPGP layer never re-encrypts or transcodes it, and external delivery
from those accounts follows #4's explicit user-selected ordinary-delivery
opt-in, never silent.

### 5. Storage encryption: harden what we have

The SQLCipher design stands (random 32-byte key in the keyring, verified
migration, 0600/0700, fail closed). The gaps to close:

- `PRAGMA cipher_memory_security = ON` (off by default; locks and wipes
  SQLCipher's own pages).
- Pin and assert the KDF parameters (4.x default 256,000 PBKDF2-HMAC-SHA512
  iterations is acceptable; assert rather than assume, via
  `PRAGMA cipher_status`, and fail closed on mismatch).
- A startup and CI verification that the cache file is actually encrypted
  (magic-bytes negative, open-without-key negative, open-with-key positive)
  — the pending "full SQLCipher verification" item.
- Attachment spools and temp files stay owner-only; the FTS index lives
  inside the encrypted database, not beside it.

Key rotation (rekey on demand) and a duress story are explicitly deferred:
rotation without a re-auth ceremony adds little, and the login password
owns the keyring anyway.

### 6. Memory: minimise lifetimes, contain what matters, document limits

CPython cannot reliably zero secrets: `str`/`bytes` are immutable, copies
are made by every layer (logging, tracebacks, JSON, base64), and the GC
moves objects. `ctypes.memset` into immutable objects is version-fragile
and breaks interning assumptions. Honest posture:

- Short lifetimes as the primary control: fetch credentials on the worker,
  use, `del`, never stash on `self`, never format into messages (already
  the rule; audit it).
- For the two highest-value buffers (DB key, account passwords in transit),
  investigate `bytearray` + `OPENSSL_cleanse` via `cryptography`, and prefer
  libraries that keep secrets in C/Rust heaps (opaque pointers) over bytes
  the app holds.
- No `mlock` promises: the default locked-memory limit (~2.5 MB) and the
  Flatpak sandbox make it unreliable; SQLCipher's `cipher_memory_security`
  covers its own pages.
- Document all of this in the code where the buffers live. No theatre.

### 7. Session lock: follow the KeePassXC model

On `org.gnome.ScreenSaver` `ActiveChanged(true)` and
`org.freedesktop.login1` `PrepareForSleep(true)`: stop watchers, close
pooled sessions, drop the in-memory DB key handle (re-open the cache on
unlock through the normal keyring path), and blank the window to the
locked page. Needs `--system-talk-name=org.freedesktop.login1` plus the
existing session-bus patterns. Unlock is automatic on session activity
(the keyring is already unlocked); the optional fingerprint gate from §3
rides on top for users who enable the lock.

### 8. Sandbox and secrets: least privilege, in order

Current finish-args are already tight (no home, no host D-Bus). The
remaining work, ordered by value:

1. Keep libsecret against `org.freedesktop.secrets` for now: both
   gnome-keyring and the new oo7 daemon implement that name, so the app
   survives the Fedora 45 migration untouched.
2. Spike migration to `org.freedesktop.portal.Secret` (the oo7 file-backend
   pattern) to drop `--talk-name=org.freedesktop.secrets`. libsecret cannot
   speak the portal, so this means either a small portal client over D-Bus
   plus app-side file encryption (new crypto code — high review bar) or a
   Rust helper. Do not ship a hand-rolled keyring on a deadline.
3. Never add `--device=all` for FIDO2 (see §2); never add home/host access
   for convenience.

### 9. Transport: verify, and prove it in CI

The verify-by-default TLS contexts plus the loopback-bridge exception stay.
Add a negative test per transport (IMAP/SMTP/Graph/JMAP): a wrong-host
certificate must fail the connection, and STARTTLS downgrade (server
offers plain, client refuses to send credentials) must be covered. Enable
ruff's `S` (flake8-bandit) subset in `just check` to catch `ssl._create_
unverified_context`, weak hashes, and `subprocess(shell=True)` at review
time.

### 10. Content: keep the renderer on a short leash

The WebKit sentinel suite (scripts never run/download, remote images stay
blocked until allowed, foreign schemes launch nothing) stays the gate for
every renderer change. No `<script>` execution for features either: the
height probe uses the main-frame API, not injected JS. Any future
interactive mail feature (forms, payments) needs a new ADR, not an
exception.

## Consequences

- #29 becomes the top security item and is functional, not optional; the
  Gmail verification/CASA process is tracked separately as human work.
- New issues: SQLCipher hardening, session lock, Autocrypt/WKD spike,
  Secret-portal spike, memory-hygiene audit, TLS negative tests + ruff `S`,
  optional fingerprint lock, hardware-2FA revisit trigger.
- The app promises, in this order: encrypted cache, verified transport,
  OAuth over passwords, provider E2EE preserved, opportunistic OpenPGP with
  honest UI, and convenience gating that never pretends to be cryptography.

## References

- `docs/tuta-second-factor.md` (U2F/WebAuthn stance, consistent with §2)
- [Guidance on End-to-End Email Security](https://dkg.gitlab.io/e2e-mail-guidance/)
  (dkg IETF draft, July 2025)
- [RFC 9580](https://www.rfc-editor.org/rfc/rfc9580.html) (OpenPGP) and
  [RFC 9980](https://www.rfc-editor.org/rfc/rfc9980.html) (PQC in OpenPGP)
- [SQLCipher design](https://www.zetetic.net/sqlcipher/design/)
- [FOSDEM 2026: credentials for Linux](https://archive.fosdem.org/2026/events/attachments/838A8N-credentials-for-linux-bringing-passkeys-to-linux/slides/267430/fosdem_ravbuub.pdf)
  (passkeys/credentialsd/Flatpak status)
- [oo7](https://github.com/bilelmoussaoui/oo7) (Secret Service + portal)
