# RFC-0001: Rewrite Postcard in Rust and join the GTK office suite

Date: 2026-11-04 · Status: **draft** · Tracks: [#74](https://github.com/tuna-os/postcard/issues/74)

> This is a draft for discussion, not a decision. It proposes *how* a Rust
> rewrite would go and whether Postcard should become the fourth app in
> [tuna-os/gtk-office-suite](https://github.com/tuna-os/gtk-office-suite),
> with the spikes that must answer the open questions first. No code moves
> until the spikes report.

## Summary

Postcard is ~18.5k lines of Python (+18.5k lines of tests) plus a Rust Tuta
helper and a Go Proton helper, shipped as a Flatpak. The proposal: port it
to Rust in phases — a GTK-free `postcard-core` crate first, then provider
drivers, then a gtk-rs shell — and land as a fourth app in the
gtk-office-suite workspace, sharing its platform layer. The Python app keeps
shipping throughout; each phase is independently useful and the port is
proven at every step by running the existing Python test-suite as an oracle.

## Why consider this

1. **An email client is an untrusted-input engine.** Every byte of every
   message — headers, MIME trees, HTML bodies, attachments, server
   responses — is attacker-controlled. MIME parsing, charset handling and
   protocol state machines are exactly the code memory safety helps most,
   and today they run in CPython with `bytes` buffers the app can neither
   lock nor zero (ADR 0001 §6).
2. **The suite already solved the platform problems.** Raw gtk-rs +
   libadwaita with no tokio in the apps, a `gtk_test` harness that fixed
   the display-less-skip masking Postcard still has, `cargo --locked`
   Flatpak builds, `deny.toml` supply-chain gates, gettext i18n, GNOME HIG
   guidelines with CI enforcement. Joining means adopting solved problems,
   not re-solving them.
3. **The ecosystem is ready.** `mail-parser` (Stalwart, fast/robust),
   `lettre` (SMTP/compose), `imap`/`async-imap` + `imap-codec`, `melib`
   (IMAP/JMAP/maildir/SQLite under one GPL-3.0 roof), `rusqlite` with
   `bundled-sqlcipher`, `oo7` (Secret Service *and* the Flatpak Secret
   portal — the ADR 0001 §8 migration for free), `zbus` (typed D-Bus that
   deletes the raw `Gio.DBusProxy` code in `goa.py`/`tray.py`), Sequoia
   (the ADR 0001 §4 OpenPGP plan, already Rust).
4. **Two helpers already point the way.** The Tuta bridge is Rust today and
   becomes a crate dependency instead of a subprocess; the JSON-over-stdin
   IPC pattern the helpers use is exactly the strangler seam (see Phase 1).

## Why not (yet): honest costs

- **It is a port of ~37k lines of battle-tested behaviour**, including a
  delivery state machine (#10–#12, #54), UIDVALIDITY handling, Graph delta
  quirks, and German-mailbox folder classification. Every line carries a
  bug someone already found. A rewrite re-pays that tuition unless the
  oracle strategy below is enforced ruthlessly.
- **The provider long tail stays long.** Tuta's SDK is Rust already (easy
  win); the Proton helper is Go (`proton` crate bindings are immature —
  keep the subprocess); Graph/JMAP/Fastmail/MS-OAuth are hand-rolled over
  `urllib` and port line-for-line.
- **WebKit.** The reading pane needs `webkit6-rs` bindings; the sentinel
  suite (scripts-never-run, remote-images-blocked) must be re-proven, and
  any binding gap blocks the shell phase.
- **Contributor and agent velocity.** The Hive fleet currently moves fast
  in Python; Rust reviews are slower and the borrow checker will eat the
  first month. Dual maintenance during the strangler period is real work.
- **Relm4 is out of scope either way.** The suite uses raw gtk-rs + glib
  main loop deliberately; Relm4's Elm layer fights gtk-rs's imperative API
  at exactly the complex-widget level a mail client lives at
  (conversation lists, WebKit embeds, drag-and-drop). The shell follows
  the suite: raw gtk-rs, threads + `idle_add`, same as today's Python
  threading model — which is why the port is mechanical, not architectural.

## What the suite shares (and what it does not)

Joining the workspace buys, concretely:

| Shared | How |
|---|---|
| `suite-common` GTK widgets | toasts, dialogs, file dialogs, shortcuts, menus — the mail shell uses them instead of hand-rolling |
| `gtk_test` harness | display-failure-is-failure widget tests; deletes Postcard's skip-masking gap |
| `suite-common-core` (select) | `atomic_save`, `session`, `search`, `undo` patterns where mail fits them; document-model pieces (styles, units, print) do *not* transfer |
| Tooling | `deny.toml` + lockfile discipline, `cargo --locked` Flatpak modules, gettext-rs, GNOME-guideline CI, screenshot/Xvfb workflows |
| Process | RFC/ADR culture, conformance-vs-assertion testing, hive task shapes |

What does **not** transfer: the document models (mail is not a document),
the collab sidecar, and any Python code (it becomes oracle fixtures, then
is deleted). A shared `suite-mail-core` published to crates.io is the
explicit end state, so the mail core must be dependency-clean (no GTK, no
tokio in the default build — same rule as `suite-collab`).

## Proposed phases

**Phase 0 — spikes (no product code).** Each spike is time-boxed and
reports measurements, not opinions:

1. **Shell spike:** a `postcard-shell-spike` binary in a scratch workspace
   member rendering the three-pane window with `suite-common` widgets over
   canned data. Answers: Blueprint/template story in gtk-rs, `webkit6-rs`
   adequacy for the sentinel suite, startup cost.
2. **`melib` evaluation:** can its IMAP/JMAP/maildir/SQLite backends serve
   as the sync engine, or is it reference-only? Criteria: UIDPLUS/IDLE
   support, error taxonomy mappability, SQLite schema control (we need
   *our* schema + SQLCipher + FTS5), GPL-3.0 compatibility (yes).
   Default hypothesis: reference-only; the sync semantics (#10–#12) are
   Postcard-specific and port line-for-line onto `imap`/`rusqlite`.
3. **`rusqlite` + `bundled-sqlcipher` migration proof:** open the shipped
   Python-created cache, verify `cipher_status`, migrate a version,
   measure open + query parity. Must prove zero user-visible migration.
4. **Bridge measurement:** PyO3 in-process vs JSON-IPC-subprocess for
   calling ported Rust from the shipping Python shell during the
   strangler period. Criteria: failure isolation (a parser panic must not
   take the UI), packaging (one Flatpak either way), per-call overhead on
   a 50 MB message (the #15 budget).

**Phase 1 — `postcard-core`: MIME, threading, compose, store.** Port
`message_parser` (or adopt `mail-parser` behind our API — the spike
decides), `threader`, `compose`, `database` (rusqlite + SQLCipher +
FTS5), `lifecycle`, error vocabularies. The Python suite becomes an
oracle: every core test gets a fixture-driven twin asserting identical
behaviour (same inputs, same outputs, byte-identical DB effects). The
Python shell calls the core through the Phase 0 bridge; users see no
change. The Tuta helper folds in as a crate dependency.

**Phase 2 — provider drivers.** Port sessions and `mail_sync` driver by
driver (IMAP/SMTP, Graph, JMAP, Fastmail/MS OAuth, Tuta ops, Proton via
the kept Go helper). Each driver lands behind the Phase 0 bridge with its
fake-transport tests ported and the live harness still passing. `zbus`
replaces the raw D-Bus clients (GOA, tray, ScreenSaver/login1 from #65);
`oo7` replaces libsecret and takes the Secret portal (closes #68 by
construction).

**Phase 3 — the GTK shell.** Raw gtk-rs main window reusing
`suite-common` widgets, `gtk_test` suites replacing `tests/gui`,
Blueprint `.blp` reused as-is (same compiler, same resource paths).
Feature-parity checklist per view; the Python shell is deleted view by
view. WebKit sentinel suite re-proven first — no shell ships without it.

**Phase 4 — suite membership.** Move the crates into `gtk-office-suite`
as `postcard-core` + `postcard` workspace members (or a git-subtree
mirror if org history must stay separate — owner's call), unify the
Flatpak manifests, AppStream IDs (`in.gxanshu.postcard` stays — REVISIT:
suite uses `org.tunaos.*`; renaming breaks installed updates and needs
its own migration plan), and CI gates. Publish `postcard-core` to
crates.io only after the API stabilises.

## What stays Python (possibly forever)

- One-shot tooling (`web/build.sh` equivalents, linters config) where Rust
  buys nothing.
- The decision to delete the Python shell is Phase 3's exit gate, not this
  RFC's: if the shell never reaches parity, the product is a hybrid and
  that must be a conscious, documented state — not drift.

## Open questions for the spikes

1. `mail-parser` vs port: does its API cover the #15 bounds (50 MiB / 25
   MiB / 1000 parts / 1 MiB headers) and refusal semantics, or do we wrap
   it?
2. `webkit6-rs` completeness for the sentinel suite (process-per-view
   settings, script-message handlers for the height probe, policy hooks).
3. PyO3 vs IPC bridge: measured per-call cost and panic isolation (spike 4
   decides; author's prior: IPC, matching the helpers).
4. Subtree vs monorepo move for Phase 4, and the AppStream ID question.
5. `melib` verdict (spike 2): adopt, borrow, or ignore.

## Consequences if accepted

- Four spike issues land first; product phases do not start until all four
  report.
- Python feature work continues in parallel but new core logic must be
  written portability-first (pure functions, fixture-testable, no GTK
  leakage) so the port tracks rather than chases.
- The Hive fleet splits: a Rust lane (toolchain per `rust-toolchain.toml`
  conventions) alongside the Python lanes, with the oracle suite as the
  shared contract.

## References

- [gtk-office-suite ARCHITECTURE.md](https://github.com/tuna-os/gtk-office-suite/blob/main/docs/ARCHITECTURE.md)
  (core/shell split, the pattern this RFC follows)
- [gtk-office-suite gtk_test.rs](https://github.com/tuna-os/gtk-office-suite/blob/main/suite-common/src/gtk_test.rs)
  (display-failure-is-failure harness)
- [melib](https://docs.rs/melib/latest/melib/) (GPL-3.0 mail core candidate)
- [mail-parser](https://lib.rs/crates/mail-parser),
  [lettre](https://lib.rs/crates/lettre), [rusqlite
  bundled-sqlcipher](https://lib.rs/crates/rusqlite),
  [oo7](https://github.com/bilelmoussaoui/oo7)
- ADR 0001 (security posture — the rewrite must preserve every guarantee:
  SQLCipher cache, keyring secrets, fail-closed vocabularies, renderer
  leash)
