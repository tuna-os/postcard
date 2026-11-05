# Secret-portal spike (issue #68)

Status: prototype, not wired in. `src/postcard/core/portal_secrets.py` plus
`tests/core/test_portal_secrets.py` answer whether the app can drop
`--talk-name=org.freedesktop.secrets` by keeping its secrets in an encrypted
file keyed by `org.freedesktop.portal.Secret` (the oo7 file-backend pattern
from ADR 0001 §8). Nothing imports the module; the wiring decision stays with
the owner. Verdict: **NO-GO for now** — see §6. Desktop coverage is much
better than the ADR assumed (KDE fixed its backend in 2024), but portal-only
storage would still brick credentials on the long tail of compositors, and
the cipher the file needs is new crypto code with a high review bar.

## 1. What the API actually is

The Secret portal is **not** a keyring. It hands a sandboxed app one opaque
per-app master secret; storing items and encrypting them is the app's job.
That is the whole oo7 file-backend pattern: retrieve the master secret,
seal a local file with a key derived from it.

Frontend (`org.freedesktop.portal.Secret`, version 1, on bus
`org.freedesktop.portal.Desktop`, path `/org/freedesktop/portal/desktop`):

```
RetrieveSecret(IN h fd, IN a{sv} options, OUT o handle)
```

- `fd` is a *writable* file descriptor (an index into the call's Unix fd
  list) that the backend writes the raw secret bytes to. The app creates a
  pipe, passes the write end, and reads the read end after the call
  completes. Over Gio this is `call_with_unix_fd_list_sync` with a
  `GUnixFDList`, exactly as the prototype does.
- There is no app-id parameter: the portal daemon knows the caller's
  sandbox. The reply is only a `Request` handle; completion arrives as an
  `org.freedesktop.portal.Request::Response(u response, a{sv} results)`
  signal, so the client subscribes before calling. The repo already solves
  this race with a `handle_token` it derives the Request path from
  (`preferences_dialog.py` background-portal flow); the prototype copies
  that pattern. No `Session` object is involved — Secret uses Request only.
- `options` carries `handle_token` (s) and `token` (s). `results` may carry
  a new `token` the app must pass back next time; it is an opaque lookup
  hint, not key material, so persisting it in plaintext beside the sealed
  file is acceptable.
- The secret's format and length are opaque and may not suit a cipher
  directly — the docs say to expand it with a KDF. The prototype derives a
  32-byte file key with HKDF-SHA256 (RFC 5869) over a random per-file salt.
- Frontend since xdg-desktop-portal **1.5.0** (2019): the version
  requirement is a non-issue on any maintained distro.

Backend (`org.freedesktop.impl.portal.Secret`, implemented by the desktop):

```
RetrieveSecret(IN o handle, IN s app_id, IN h fd, IN a{sv} options,
               OUT u response, OUT a{sv} results)
```

Typical backends keep the per-app secret in the user's keyring under the
app id — gnome-keyring and KWallet both do exactly this.

## 2. Who implements the backend

| Environment | Backend | Since | Notes |
|---|---|---|---|
| GNOME | gnome-keyring-daemon | 2019 | The reference backend; the portals.conf docs use `Secret=gnome-keyring` as the example |
| Fedora 45+ / oo7 distros | oo7-portal | oo7 stack | Ships with oo7 in place of gnome-keyring |
| KDE Plasma | kwalletd (KWallet) | Frameworks 6.2 (Oct 2024) | Bug 466197, RESOLVED FIXED; secret stored in KWallet |
| Sway/Hyprland/wlroots, COSMIC, Xfce, MATE, LXQt | none | — | No portal package (`-gtk`, `-wlr`, `-hyprland`, …) implements Secret; it lives in keyring daemons, not portal packages |
| Headless / CI | none | — | No portal at all |

Two caveats. First, "none" is soft on systems where the user happens to run
`gnome-keyring-daemon`: a `portals.conf` entry can point Secret at it, but
that is host configuration the Flatpak cannot rely on. Second, the KDE row
is the surprise of this spike — anything on Plasma 6.2+ (over a year old at
the time of writing) is covered, so GNOME + current KDE is the large
majority of Flatpak desktop users. The gap is the long tail, not half the
user base.

## 3. The prototype

`src/postcard/core/portal_secrets.py` mirrors the `secrets.py` contract the
app actually uses — store/lookup/delete per account id plus the 32-byte
SQLCipher cache key — split across two seams so each is testable alone:

- `PortalTransport` (protocol) / `DBusPortalTransport` (live Gio client):
  pipe creation, fd-list call, `handle_token` pre-subscription, Request
  wait on a private `MainContext` so a worker thread can block on it, token
  carry-forward, bounded secret read. **Not yet exercised against a live
  portal** — that needs a sandboxed run on GNOME or current KDE.
- `PortalSecretStore(path, transport, cipher)`: a JSON envelope
  (`salt`/`token`/`sealed`) holding one sealed dict, atomic owner-only
  writes (tmp file + `os.replace`, 0600 at creation), fail-closed reads
  (any damage raises `PortalSecretError`, never `None`-then-overwrite),
  and `database_key()` with the same malformed-key behaviour as
  `secrets.database_key` (warn, raise, file untouched).
- `Cipher` (protocol): **no implementation ships, on purpose.** Wiring
  needs a real AEAD, and a placeholder cipher would be worse than none.
  The tests use a marker fake that proves the store seals without saying
  how.

`tests/core/test_portal_secrets.py` (35 cases, fake transport only, no live
portal needed) pins: the exact bus/path/interface/version strings,
per-account round trips and isolation, overwrite, delete-true/false,
persistence across instances, no-plaintext-at-rest, 0600 mode, no tmp
leftovers, envelope shape, token replay, KDF-not-raw-master key use,
per-file salts, stable 32-byte cache keys coexisting with passwords,
malformed cache keys failing closed with the file untouched, nine damaged
file shapes failing closed, dead-portal failures on every operation,
transport-error wrapping vs `PortalSecretError` pass-through, and
unwritable paths failing closed.

Note for the wiring lane: the module ships unwired, so it deliberately has
**no** `src/meson.build` install entry and no `po/POTFILES.in` entry (it
holds no `_(…)` strings). Adding wiring must add the meson entry.

## 4. Migration sketch (if this ever goes GO)

1. Settle the cipher first: either a reviewed AEAD (AES-GCM via
   `cryptography`, if it is acceptable in the SDK) or — better fit for this
   codebase — a small Rust helper reusing oo7's file backend over the same
   JSON-over-stdin IPC as the Tuta/Proton helpers. Python hand-rolled
   crypto is rejected; that is the ADR's high review bar, not this spike's.
2. Put the sealed file at `$XDG_DATA_HOME/postcard/secrets.json` (0600),
   beside the database, not in it.
3. First run after upgrade: read every `in.gxanshu.postcard.Account` item
   and the `mail-cache` key out of the keyring via libsecret, write the
   sealed file, then `password_clear` the originals only after a verified
   re-read of the file.
4. Keep a dual-read window of one release: portal file first, keyring
   fallback, re-seal on fallback hit. Rollback is then "stop writing the
   file"; the keyring originals must survive until the window closes.
5. GOA accounts are untouched throughout: their tokens are fetched live
   and never stored anywhere.
6. Never migrate silently on a desktop without a backend: no backend means
   no `retrieve_secret`, which must read as "could not sign in", never as
   an empty store the app then overwrites.

## 5. What wiring would change

- Manifest: drop `--talk-name=org.freedesktop.secrets`; nothing replaces
  it — every Flatpak may talk to the portal bus already. That is the entire
  sandbox gain, plus per-app file secrecy against *other* sandboxed apps
  sharing the secrets bus. It is not a boundary against the session owner,
  who can read their own files (ADR: out of scope).
- `src/meson.build`: add the module (and the helper, if Rust) to the
  install sources.
- `po/POTFILES.in`: nothing — no user-facing strings in the module.
- GSettings: nothing — no new settings.
- CI: nothing live is testable — no portal backend exists headless, so the
  fake-transport suite stays the gate, as it is for this spike.
- Runtime: needs only glib/Gio fd-passing, already in the GNOME 51 SDK.
- Threading: `retrieve_secret` blocks on IPC and keyring unlock, so it
  runs on the worker like `secrets.credential_for`, never on the main
  loop; the private-`MainContext` wait in the prototype exists for that.

## 6. Recommendation: NO-GO (for now)

1. **The long tail has no backend.** Portal-only storage bricks sign-in on
   Sway/Hyprland, COSMIC, Xfce, MATE, LXQt, and older KDE. Keeping a
   libsecret fallback un-bricks them but keeps the talk-name — which
   nullifies the stated goal. There is no third option; Flatpak
   permissions are static.
2. **The cipher is the expensive half.** The portal hands out key
   material, not storage. A correct AEAD file backend is either new crypto
   code (high review bar per the ADR) or a Rust helper (real work, new
   supply chain surface). The prototype's store is the easy part.
3. **The current posture is already reasonable.** libsecret against
   `org.freedesktop.secrets` survives the Fedora 45 oo7 migration
   untouched (ADR §8.1), and the talk-name is narrow compared to what it
   could be.

Flip conditions, either of which reopens this: a Secret backend lands in
`xdg-desktop-portal-gtk` (the common fallback, which would cover the long
tail at once), or the project declares GNOME + current-KDE its supported
set and accepts the Rust-helper cipher cost. Until then, keep this module
as the researched starting point and change nothing.

## References

- [Secret portal, app side](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Secret.html) (interface v1, `RetrieveSecret` signature, token/KDF notes)
- [Secret portal, backend side](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.impl.portal.Secret.html)
- [xdg-desktop-portal NEWS](https://github.com/flatpak/xdg-desktop-portal/blob/HEAD/NEWS.md) (Secret portal added in 1.5.0)
- [KDE bug 466197](https://bugs.kde.org/show_bug.cgi?id=466197) (RESOLVED FIXED, Frameworks 6.2, secret in KWallet)
- [oo7](https://github.com/linux-credentials/oo7) (file backend + `oo7-portal`, libsecret-compatible format)
- [Snap Secret-portal guide](https://snapcraft.io/docs/how-to-guides/snap-development/use-the-secret-portal/) (independent ≥1.5.0 confirmation)
- `docs/adr/0001-application-security-posture.md` §8 (the posture this spike tests)
