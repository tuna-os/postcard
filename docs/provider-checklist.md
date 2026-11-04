# Provider development checklist

How to add a mail provider to Postcard -- a new builtin driver or an external
add-on -- and the evidence gates each one must pass. The boundary itself lives
in `src/postcard/core/providers/`: `api.py` (version, requests, capabilities,
descriptors, results, errors), `driver.py` (the `ProviderDriver` interface)
and `registry.py` (dispatch, trust, discovery).

## Concepts

- **Provider id**: the stable string stored in `Account.protocol`
  (`imap`, `graph`, `tuta`, `proton`, `jmap`, ...). It must never change once
  accounts exist under it.
- **Descriptor**: the id plus a display name, a capability set and the API
  version range the driver was written against. The UI reads descriptors only;
  it never branches on the id.
- **Driver**: the `ProviderDriver` implementation. Methods run on a worker
  thread, do network only, and never touch the database or GTK. The account
  arrives as a snapshot and the credential arrives resolved.
- **Registry**: maps ids to drivers. Builtins register in code; add-ons load
  from installed entry points after a trust check.

## Adding a builtin driver

1. Implement `ProviderDriver` in `src/postcard/mail_sync.py` next to the other
   builtins, delegating to the existing per-backend functions. No new driver
   logic belongs in the driver -- it unpacks the request and calls through.
2. Give it a `ProviderDescriptor` with a new stable id and exactly the
   capabilities it implements. Omit what it cannot do; the UI disables those
   actions from the descriptor.
3. Register it in `register_builtin_drivers`. Add the id to `BUILTIN_IDS` in
   `registry.py` so no add-on can ever claim it.
4. If accounts are stored under the new protocol, keep the old path working
   until stored accounts migrate -- see `_JmapDriver`, which serves `jmap`
   accounts over IMAP without pretending to be a JMAP implementation.

## Adding an external add-on

1. Ship an installed Python distribution advertising an entry point:
   group `postcard.providers`, name = the provider id, target = a
   `ProviderDriver` instance or a no-arg class returning one. Only installed
   distributions are consulted -- nothing loads from mail or cache
   directories, and nothing is ever downloaded.
2. Declare the API range on the descriptor (`min_api_version`,
   `max_api_version` against `PROVIDER_API_VERSION`). Out-of-range drivers
   stay unloaded with `IncompatibleProviderError`.
3. The user trusts the id by adding one line to
   `$XDG_DATA_HOME/postcard/trusted_providers` and restarts. Untrusted ids
   are listed from installed metadata but never imported.
4. Trust is coarse on purpose: an add-on runs with the application's own mail
   and credential privileges. This boundary is versioning and dispatch, not
   an OS security sandbox.

## Semantics to preserve

- **Fail closed.** Unknown, disabled and incompatible providers raise
  (`UnknownProviderError`, `DisabledProviderError`,
  `IncompatibleProviderError`). No caller falls back to IMAP/SMTP -- not the
  operations, not the inbox watcher, not the UI.
- **Immutable requests.** Operations take frozen request dataclasses
  (`FetchRequest`, `FlagRequest`, ...); collections inside them are tuples.
- **Complete vs partial.** A `SyncResult` marked `PARTIAL` was not fully
  verified: the caller merges without pruning what it did not see. Mark
  `COMPLETE` only when `all_uids` is authoritative.
- **Per-id outcomes.** Batch operations report each id (`PerIdOutcome`, or
  `MoveResult` for moves). A failure part-way reports how far it got.
- **Delivery confirmation.** `sent_copy` returns the Sent copy's id, or None
  when the copy is absent *or* the provider keeps no copy to check -- the
  `SENT_COPY_LOOKUP` capability tells those apart. A failed check raises
  rather than reading as absent, so a retry never sends a second copy.
- **Duplicate ids lose.** The first registration wins. An add-on reusing a
  builtin id, or a second add-on reusing any loaded id, is rejected without
  replacing anything.
- **Isolation.** One broken add-on -- bad import, bad object, id mismatch,
  version mismatch -- is recorded in `registry.load_errors` and the rest
  still load.
- **No secrets in logs or errors.** Registry and driver errors name the
  provider and the reason in plain words; credentials never travel in them.

## Evidence gates

- `just test` green, including `tests/core/providers/`: registry dispatch and
  fail-closed lookups, builtin capability matrix, request immutability,
  discovery isolation (broken import, non-driver, id mismatch, version
  mismatch, duplicate ids), trust-file parsing and corruption, missing
  provider account recovery (operations raise, UI support reads False,
  watcher polls without opening a backend).
- `just check` green (ruff + pyright over `src/postcard`).
- `just test-gui` green: the capability-driven action states ride the GUI
  suite, and the composer banner plus the disabled Send path need a display.
- Installed-Flatpak smoke: add/remove an account of the touched kind,
  exercise its unsupported actions, confirm toasts/banners name the provider.

## Follow-ups (explicitly out of scope)

- The Flatpak extension mount XML in the manifest for add-on delivery.
- GSettings keys for provider enablement. Until they exist, builtins are
  always enabled and add-ons use the file-based trust list above.
- Push/watch backends beyond IMAP IDLE; JMAP event sources stay future work.
