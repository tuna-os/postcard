# Supply chain: pinned toolchains, updates, and triage (issue #18)

Every input that can move under us is either pinned below or listed as an
accepted residual. A dep bump is a normal PR: CI re-proves it, and the audit
steps fail the build if the new graph has a known vulnerability or a
license outside the allow-list.

## Version sources (each version declared exactly once)

| Input | Declared in | Consumed by |
|---|---|---|
| Python tool pins (`ruff`, `pytest`, `pyright`, `pip-audit`, `pip-licenses`, `sqlcipher3`, `PyGObject-stubs`) | `requirements.txt` (as `-c` constraints, never `-r`) | `just init`, `tests.yml`, pytest-only installs (`-c requirements.txt pytest`) |
| Rust toolchain | `rust-toolchain.toml` (`channel`) | `tuta-native.yml` (installs it, then plain `cargo`) |
| Rust dependency graph | `native/tuta/Cargo.lock` | `cargo --locked` build/test; `cargo deny check` |
| Rust audit config | `native/tuta/deny.toml` | `cargo deny check` |
| Go toolchain patch | `proton-native.yml` (`go-version`) | `actions/setup-go` |
| Go dependency graph | `native/proton/go.mod` + `go.sum` | `go vet/test/build` (sumdb-verified) |
| Tuta SDK source | `native/tuta/upstream` gitlink + `sdk/BASE`, `sdk/EXPECTED` inside it | `sdk-prepare.py` (verified, then extracted) |
| GNOME runtime/SDK | `in.gxanshu.postcard.json` (`runtime-version: 51`) | flatpak-builder, local and CI |
| Flatpak module sources | same manifest (blueprint-compiler tag+commit, sqlcipher3 URL+sha256) | flatpak-builder |
| GitHub Actions | commit SHA + `# vX.Y.Z` comment at each `uses:` | every workflow |
| Release builder image | `release.yml` (`:gnome-51@sha256:…`, mirrored in `BUILDER_DIGEST`) | build + publish containers |
| actionlint | version + sha256 inline in `tests.yml` | workflow lint step |
| syft (SBOM) | `syft-version` input in `release.yml` | `anchore/sbom-action` |
| cargo-deny | `--version` in `tuta-native.yml` | `cargo install --locked` |
| Runner generation | `runs-on: ubuntu-24.04` (all jobs) | every workflow |

### Declared toolchains

- **Python:** language target 3.12 (`pyproject.toml` ruff target, pyright
  `pythonVersion`, CI asserts `python3.12` on the pinned runner); shipping
  runtime 3.14.9 (verified: `org.gnome.Sdk//51` and `org.gnome.Platform//51`
  both report Python 3.14.9). Host tool pins must support 3.12+.
- **Rust:** tested toolchain 1.99.1 (`rust-toolchain.toml`); minimum floor 1.84
  (`rust-version` in `native/tuta/Cargo.toml`, inherited from the SDK
  workspace). CI proves the declared compiler builds and tests the locked
  graph (`cargo test --locked`, `cargo build --locked`).
- **Go:** 1.24.13 (`go-version`); `go 1.24` directive stays the floor.
- **SDKs:** GNOME 51 (manifest); Tuta SDK = tutabridge `da12fd4` + tutanota
  `f5e2d86` (release 362.261020.0) + twelve hashed patches, all verified by
  `sdk-prepare.py` (see `native/tuta/README.md`).

## Update automation

`.github/dependabot.yml` opens weekly `chore(deps)` PRs for the four
ecosystems it understands: `github-actions` (follows SHA pins, keeps the
`# vX.Y.Z` comment accurate), `pip` (`requirements.txt`), `cargo`
(`native/tuta`), `gomod` (`native/proton`). Everything else is manual:

| Manual pin | Bump command |
|---|---|
| Builder image digest | `curl` the ghcr token API, read `docker-content-digest` for tag `gnome-51`, update both `image:` lines and `BUILDER_DIGEST` |
| `rust-toolchain.toml` | set `channel`, run the tuta workflow, update `native/tuta/README.md` if the lane keeps a copy |
| `go-version` | pick the latest `1.24.x` from `go.dev/dl/?mode=json`, update `proton-native.yml` |
| `cargo install --version` (deny) | latest from `crates.io/api/v1/crates/cargo-deny`, update `tuta-native.yml` |
| actionlint | latest release + `checksums.txt` entry, update version, URL, and sha256 in `tests.yml` |
| `syft-version` | latest stable `anchore/syft` tag, update `release.yml` |
| `runs-on` | deliberately per Ubuntu LTS; 24.04 is supported to 2029 |

## Triage and exception policy

The audit steps block merges. When one fails:

1. **Prefer the fix:** bump the pinned input (dependabot usually has the PR
   already) so the finding disappears.
2. **If no fix exists**, file an issue, then add a time-boxed exception that
   names it:
   - `pip-audit`: append `--ignore-vuln <CVE> # <issue URL>, re-check <date>`
     to the audit step. Never ignore silently.
   - `cargo deny`: add `{ id = "RUSTSEC-...", reason = "<issue URL>,
     re-check <date>" }` to `[advisories] ignore` in `deny.toml`.
     Vulnerabilities, unsoundness, and notices fail unconditionally in deny
     0.20; yanked only warns; unmaintained is deliberately not gated
     (`none`) — the graph carries six such advisories on stable crates with
     no upgrade path, and unmaintained is not vulnerable. A crate with no
     machine-readable license needs a `[[licenses.clarify]]` entry instead
     of an allow-listing.
   - `pip-licenses` / `cargo deny` licenses: a new license needs a human
     allow-listing it in the workflow step or `deny.toml`, with the issue
     that reviewed it. New copyleft in the Rust graph is allowed (the product
     is GPL-3.0-or-later) but must be a conscious edit, not drive-by.
3. **Re-check dates are real:** when one lapses, either the fix has landed
   (drop the exception) or the issue gets a new date with a comment saying why.

Shipped exception: `deny.toml` ignores `RUSTSEC-2023-0071` (rsa Marvin timing
sidechannel, no fixed release exists) with re-check 2027-05-01. It landed
without a tracking issue number — the tuta lane owns filing it.

License posture: dev tools are never shipped, so permissive + LGPL tooling is
fine; the one shipped Python dependency is `sqlcipher3`, whose pinned sdist
carries a zlib-style permissive license (verified in `LICENSE` of
`sqlcipher3-0.6.4.tar.gz`; it reports UNKNOWN to `pip-licenses` because it
ships no `License:` metadata, which is why it is checked by hand instead of
in the automated step). The Rust graph allow-list in `deny.toml` is the exact
identifier union over the locked crates plus both GPL-3 variants for the
workspace/SDK crates.

## SBOM and provenance

Each release ships three extra assets beside `postcard.flatpakref` (additive;
the signing flow, `force_orphan` pages publish, and the download-counter rule
in `CLAUDE.md` are unchanged):

- `sbom.spdx.json`: SPDX SBOM of the release source (same commit the build
  uses), generated by pinned syft via `anchore/sbom-action`.
- `build-provenance.json`: version, git SHA, builder version, builder image
  digest, and the per-arch signed ostree commit IDs.
- SLSA provenance via `actions/attest-build-provenance`, verifiable with
  `gh attestation verify`. Needs `id-token: write` + `attestations: write`,
  which only the publish job holds.

## Permissions

Workflows default to `contents: read`. Write goes only to jobs that publish:
`release.yml` `publish` (`contents` + `id-token` + `attestations`) and
`site.yml` `publish` (`contents`). Validation jobs (`check`, `test`, `sbom`,
GUI/native suites) are read-only; the release `build` job signs with the GPG
secret rather than the token, so it needs no write permission either.

## Private security reporting — open item

`SECURITY.md` currently routes vulnerabilities to the **public** issue
tracker, and private vulnerability reporting is not mentioned anywhere. That
is the gap this lane could not close from a config PR: GitHub's private
vulnerability reporting is a repository setting, and the reporting text lives
in `SECURITY.md`, which this lane may not edit. Recommendation for the owning
lane: enable Settings → Security → Private vulnerability reporting, then
point `SECURITY.md` at it for anything that is not already public.

## Accepted residuals (unpinned, knowingly)

- `apt-get` packages in CI (`python3-gi`, `flatpak-builder`, …): unpinned
  Debian revisions, bounded by the pinned `ubuntu-24.04` image.
- Flathub runtime/SDK branches (`org.gnome.Platform//51`): branch pins, move
  with Flathub updates; ostree is content-addressed underneath.
- Python transitive dependencies (e.g. `pluggy` under `pytest`): float under
  the direct pins; no hash lockfile. `pip-audit` covers the direct pins;
  Dependabot keeps them fresh weekly. A `pip-compile --generate-hashes`
  lockfile is the follow-up if this ever needs to be airtight.
- Go toolchain modules and the Rust advisory DB are fetched at CI time from
  immutable upstreams (go.dev/sumdb, RustSec DB) and verified on use.
