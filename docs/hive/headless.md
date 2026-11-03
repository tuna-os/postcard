# Headless setup, verify, and smoke entrypoints

Issue [#22](https://github.com/tuna-os/postcard/issues/22): the supported
ways to set up, verify, and smoke-test Postcard without a display, without
live credentials, and without touching real data. Three paths run the same
toolchain; pick one per machine and stay consistent.

| path | what it is | when to use it |
|---|---|---|
| Builder | GNOME Builder opens the repo and runs the Flatpak | interactive dev on a GNOME desktop (CONTRIBUTING.md) |
| `just` | the justfile's recipes around `flatpak-builder` | terminal dev loop; the documented default |
| direct | the literal commands CI runs (no `just`) | headless runners, containers, and the Hive health loop |

Builder and `just` wrap the same manifest (`in.gxanshu.postcard.json`) and the
same GNOME 51 runtime/SDK; the direct commands below are quoted from
`.github/workflows/tests.yml` and `.github/workflows/gui-tests.yml`, which
deliberately avoid `just` so CI needs nothing beyond the runner image.

## Setup

Host prerequisites for every path: `flatpak` and `flatpak-builder`.
The GUI suite additionally needs `xvfb`. Python, GTK, and meson come from the
GNOME SDK inside the Flatpak — never from the host, except for the
host-side lint/test tooling below.

**`just` path** (CONTRIBUTING.md; one-time per machine):

```bash
just init     # Flathub remote, runtime + SDK, .venv with lint/test tooling
```

`just init` maps to `flatpak-builder --install-deps-from=flathub
--install-deps-only` plus a `--system-site-packages` venv where PyGObject
comes from apt (`python3-gi`) and `PyGObject-stubs==2.18.0` is installed
with `--no-deps`. Keep the stubs pin in step with `tests.yml`.

**Direct path** (from `gui-tests.yml` and `tests.yml`):

```bash
sudo apt-get install -y flatpak flatpak-builder xvfb
flatpak remote-add --if-not-exists --user \
    flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak-builder --install-deps-from=flathub --install-deps-only \
    --user .flatpak/build in.gxanshu.postcard.json
```

Host-side lint/test tooling (from `tests.yml`; no Flatpak involved):

```bash
sudo apt-get install -y python3-gi gir1.2-glib-2.0 \
    gir1.2-gdkpixbuf-2.0 python3-cairo python3-pytest \
    python3-dev libssl-dev
pip install ruff pyright
pip install --no-deps PyGObject-stubs==2.18.0
pip install sqlcipher3==0.6.4
```

The `sqlcipher3==0.6.4` pin must match `requirements.txt` and the
`sqlcipher3` module source in `in.gxanshu.postcard.json`; the stubs pin must
match the justfile. The Hive health loop checks both.

## Verify

| goal | `just` | direct (what CI runs) |
|---|---|---|
| lint + format + types | `just check` | `ruff check src tests`, `ruff format --check src tests`, `pyright src/postcard` |
| host unit tests | `just test` | `python3 -m pytest` |
| GTK suite (sandboxed) | `just test-gui` | `xvfb-run` + `flatpak run --command=python3 … -m pytest tests/gui/` (see `gui-tests.yml`) |
| manifest lint | `just lint` | `flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest in.gxanshu.postcard.json` |
| translation template | `just pot` | host `meson` + `ninja postcard-pot` (opt-in; needs gettext on the host) |

Notes:

- `just test` depends on `just check`, and `just build` / `just run` /
  `just bundle` depend on `just test` — a lint, type, or test failure blocks
  the Flatpak build itself, not just CI. `release.yml` calls `tests.yml`
  before publishing for the same reason.
- `[tool.pytest.ini_options]` sets `pythonpath = ["src"]`, so the package
  resolves without being installed. No `conftest.py`, no editable install.
- The GTK suite runs inside the installed Flatpak under Xvfb with the repo
  exposed (`--filesystem="$PWD"`) and a pure-Python pytest installed into
  `.flatpak/pytest-libs` (gitignored; the sandbox has a private `/tmp`).
  GUI tests skip without a display, so a bare host `pytest` never fails for
  lack of one.
- Native-helper suites mirror their workflows: Proton runs `gofmt`,
  `go vet`, `go test`, `go build` in `native/proton/` (Go 1.24, matching
  `native/proton/go.mod`) plus the compiled-binary contract test in
  `tests/core/net/test_proton_session.py`; Tuta runs `sdk-prepare.py`,
  `cargo +1.99.1 fmt --check`, `cargo +1.99.1 test`, and `cargo +1.99.1
  build` for `native/tuta/` plus `tests/core/net/test_tuta_session.py`.
  Both contract tests run without an account.

## Smoke

Ordered from cheapest to most complete. None of them needs an account, a
display, or the network beyond the first runtime download.

1. **Static smoke** — `sh web/build.sh /tmp/site-smoke` assembles the
   landing page exactly as `site.yml` and `release.yml` do; the install
   buttons must point at
   `https://github.com/gxanshu/postcard/releases/latest/download/postcard.flatpakref`
   (the per-asset download counter — the project's only install metric).
2. **Build smoke** — `just build`, or headless without installing:
   `flatpak-builder --force-clean --repo=/tmp/smoke-repo .flatpak/build
   in.gxanshu.postcard.json`. A fresh checkout drops `just build`'s
   `--disable-updates` (there are no trusted cloned sources yet); CI's
   `gui-tests.yml` does the same.
3. **Installed smoke** — `flatpak info in.gxanshu.postcard`, then the
   headless probe `xvfb-run -a flatpak run in.gxanshu.postcard --help`
   (bounded with `timeout`; GApplication answers `--help` without showing
   the UI, but keep Xvfb in case GTK initializes first).
4. **Suite smoke** — `just test-gui`, the same sandboxed Xvfb run CI does.
5. **Human smoke** — `just run` (or `just run-debug` /
   `POSTCARD_LOG=debug just run`). Four window crashes have shipped in code
   no test could reach; GTK-layer changes still end here, on a maintainer's
   display, never in automation.

## Safety rules for automation

- **No live credentials.** Scheduled and Hive-driven runs never export
  `POSTCARD_*` variables and never run `pytest -m live`. A bare `pytest`
  deselects live tests even when variables happen to be set; live tests are
  read-only, need dedicated test accounts plus a browser bootstrap, and
  follow `tests/live/README.md` — a maintainer runs them by hand.
- **No destructive data.** Automation never flags, moves, sends, or deletes
  mail; never writes to a keyring; never force-pushes, never publishes to
  the `pages` branch, and never touches `site/repo/`. The
  `force_orphan` (release) / `keep_files` (site) split in CLAUDE.md is the
  reason installed copies keep updating — any new publisher must preserve
  it, and the health loop asserts it statically.
- **Redacted output.** Logs attached to issues or PRs carry no passwords,
  tokens, addresses, or message contents; skim `POSTCARD_LOG=debug` output
  before pasting, and never raise `imaplib`/`smtplib` debug levels (both
  echo the login).
