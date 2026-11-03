# Hive task contracts

Issue [#22](https://github.com/tuna-os/postcard/issues/22): runnable issue
contracts and an auditable Hive maintenance loop.

## What this contract is built on

Only mechanisms confirmed in this repository:

- **Issue and PR states** (`open` / `closed`, draft / merged) as reported by
  `gh issue view` / `gh pr view`.
- **Labels that exist in this repo** (`gh label list`). No Hive configuration
  file, key, or API was found anywhere in the tree — a repo-wide search for
  `hive` matches prose only — so this contract defines **no new keys, files,
  or endpoints**. Anything that needs a capability not listed here is a
  human-decision blocker on the task, not an implied feature.

Labels observed (abridged to the ones this contract uses; run
`gh label list` for the full set with descriptions):

| label | meaning here |
|---|---|
| `stage/010-actionable` | triaged; eligible for pickup |
| `stage/015-triage-needed` | awaiting a maintainer's triage label |
| `stage/017-possible-existing` | prior art found; confirm the gap or close first |
| `stage/020-claimed` | claimed by a worker |
| `stage/030-pr-in-progress` | a PR is under way for the issue |
| `stage/040-covered-by-pr` | an open PR covers the issue |
| `stage/045-needs-decision` | issue parked on a person |
| `stage/050-review-pending` | review requested; awaiting a verdict |
| `stage/060-reviewed` | verdict approve; a gate is still pending |
| `stage/070-fixing` / `071-needs-author` / `072-returned-to-contributor` | red PR: who must act |
| `stage/085-needs-decision` | PR parked on a human decision |
| `stage/090-ready-for-human-merge` | only a maintainer's `/lgtm` `/approve` is missing |
| `stage/095-pre-merge-soak` / `stage/100-merge-queue` | merge pipeline stages |
| `stage/stalled` | no activity for longer than the stage expects |
| `author/hive` / `author/human` / `author/clanker` | who authored the PR |
| `role/trusted` / `role/contributor` / `role/newcomer` / `role/untrusted` | author trust tier |
| `dco` / `no-dco` | commits signed off or not (`git rebase --signoff`) |
| `hive/likely-done` | a merged PR references the issue; pending confirmation |
| `bug`, `documentation`, `enhancement`, … | default GitHub labels |

## Task states

The four states from #22 map onto open/closed plus one stage label:

| state | issue | label | who moves it |
|---|---|---|---|
| **ready** | open | `stage/010-actionable` | maintainer, after triage |
| **blocked** | open | `stage/045-needs-decision`, with a comment naming the blocker | worker or maintainer, when progress stops |
| **needs-human** | open | `stage/045-needs-decision` (issue) or `stage/085-needs-decision` (PR) | whoever hits a human-only decision |
| **review-required** | open | `stage/050-review-pending` → `stage/060-reviewed` → `stage/090-ready-for-human-merge` | reviewer swarm, then a maintainer |

The rest of the lifecycle, for completeness:

- New work filed with the
  [implementation-task template](../.github/ISSUE_TEMPLATE/implementation_task.yml)
  arrives as `stage/015-triage-needed`. A maintainer confirms it is
  machine-actionable (goal, non-goals, acceptance commands, privacy impact,
  dependencies, human-decision blockers all filled in) and moves it to
  `stage/010-actionable`, or to `stage/017-possible-existing` /
  `stage/045-needs-decision` with a comment saying why.
- A worker claims with `stage/020-claimed`, then `stage/030-pr-in-progress`
  once a branch is pushed and `stage/040-covered-by-pr` once the PR exists.
- `stage/stalled` is a flag, not a stage: anyone seeing it re-checks whether
  the task is still claimed, still blocked, or closable.
- Closing is confirmation, not cleanup: an issue closes when its acceptance
  commands pass on the merged head. `hive/likely-done` marks the
  merge-referenced-but-unconfirmed window.

## Dependencies

- Declared in the task's Dependencies section as `Blocked by #N` /
  `Blocks #M`. A task with an open blocker is not `ready`, even if triaged.
- Backlog dependencies are acceptance gates, not permission to skip work: the
  blocked task stays open and labelled, and its PR states which dependency
  release it was verified against.
- A dependency that turns out to be a human decision (access, judgement,
  timing) becomes a human-decision blocker on the blocked task too, so the
  stall is visible without reading the other issue.

## Bot-PR evidence requirements

Every Hive-authored PR (`author/hive`) must carry, in its body or as
attachments:

1. **Linked issues.** `Fixes #N` / `Refs #N` for every task it advances, so
   the merge visibly moves the tracked state.
2. **Real command outcomes.** Paste the outcome of the acceptance commands
   from the task — `just check`, `just test`, `just test-gui`, `just build`
   as applicable — not a paraphrase. Compilation alone is never evidence;
   neither is a green diff without a run.
3. **Runtime artifacts.** Test logs, redacted debug excerpts, screenshots or
   screen recordings for UI changes. Artifacts must be free of real mail and
   secrets (see below).
4. **Rollback notes.** How to undo the change: revert commit, migration
   reversibility, cache-format compatibility, what a user on the previous
   release sees. "Revert the single commit; no migration" is a complete note
   when it is true.
5. **Redacted logs.** No passwords, app passwords, OAuth or refresh tokens,
   and no private message contents — in the body, the logs, or the artifacts.
   `POSTCARD_*` variables are never exported in automation and never appear
   in pasted output. The `imaplib.Debug` / `smtplib.set_debuglevel` rule from
   CLAUDE.md applies: both echo credentials.

Form requirements come from observed repo rules, not Hive policy:
Conventional Commits (`feat:`, `fix:`, `chore:`), terse, no AI/co-author
trailers (CLAUDE.md); signed-off commits, or the PR wears `no-dco`
(the label text itself prescribes `git rebase --signoff`).

## The maintenance loop

`.github/workflows/hive-health.yml` runs the scheduled checks from #22 —
dependency pins, workflow drift, release/install smoke, stale failures, and
performance baselines. Its rules:

- **Read-only plus issues.** Checks inspect the tree, run the documented
  headless entrypoints, and query run history. The only writes are
  creating/commenting on/closing its own tracking issues via the `gh` CLI.
- **Deduplicated.** One tracking issue per check, titled
  `[hive-health] <check>`. An open issue with that title gets a comment, not
  a sibling.
- **Never silently changing policy.** A check that finds drift files an issue
  labelled `stage/015-triage-needed` for a maintainer to triage; it never
  edits a workflow, bumps a pin, or merges anything. A tracking issue closes
  only with an "all clear" comment naming the run that verified it.
- **No credentials, no destructive data.** The loop runs the same
  credential-free entrypoints documented in `headless.md`; live tests stay
  deselected and no step writes to a mailbox, a keyring, or the release
  branches.
