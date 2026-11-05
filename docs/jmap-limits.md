# JMAP capabilities and live-interop limits

What Postcard's JMAP backend (`src/postcard/core/net/jmap_*.py`) speaks, what
it refuses, and what has never been tried against a live server. Part 3 wires
this into `mail_sync`; until then nothing here runs outside tests.

## Supported capabilities

Required at discovery; the session is refused without them:

- `urn:ietf:params:jmap:core` (RFC 8620) — requests, state tokens, blobs.
- `urn:ietf:params:jmap:mail` (RFC 8621) — mailboxes, queries, messages.

Optional; sending is refused with `submissionUnsupported` without it:

- `urn:ietf:params:jmap:submission` (RFC 8621) — outbox delivery.

Everything else the server advertises (vacation, quotas, calendars, contacts)
is ignored, and calls outside mail are refused before they reach the network.
Mail only is a deliberate boundary, not a gap: no calendars or contacts.

## Methods used

Reads (any session): `Mailbox/get`, `Mailbox/changes`, `Email/query`,
`Email/queryChanges`, `Email/get`, `Email/changes`, `EmailSubmission/get`.

Writes (a session built `writable=True`, and never on an account the server
marked `isReadOnly`): `Email/set` (flags, mailbox membership, destroy,
draft create), `EmailSubmission/set` (send).

`Email/import` is deliberately absent: uploading server-side state the client
never saw could resurrect mail the user deleted elsewhere.

## Server limits honoured

| Limit | How |
|---|---|
| `maxCallsInRequest` | `batch_calls` splits multi-call requests; every helper sends one call per request, which always fits. |
| `maxObjectsInGet` | `Email/get` pages respect it (default 500 when omitted). |
| `maxObjectsInSet` | `Email/set` chunks respect it (default 100 when omitted). |
| `Retry-After` | 429 responses sleep the asked delay, capped at 30 s. |
| `isReadOnly` account | Writes refused pre-network with `readOnly`; reads still go out. |
| Blob/push URL origin | Anything outside the session's https origin is refused; credentials never follow a redirect host. |

Retry policy: reads retry on 429/503/504 (4 attempts). Writes retry only on
429 — a 503/504 answer to a `/set` may already have acted, so it is raised
for the caller to reconcile instead of sent again.

## State tokens and durable cursors

All three state kinds are opaque strings: Mailbox state, Email state, and one
query state per mailbox. Rules every caller follows:

1. Advance a stored cursor only after the cache commit lands. Deltas return
   the new state beside their data; a failure raises with the old state
   untouched, so the next sync resumes where the failed one started.
2. `cannotCalculateChanges` means the token expired: re-walk (per-mailbox
   query state), re-list (Mailbox state), or re-walk every mailbox and then
   read `current_email_state` (Email state). `needs_full_sync` tests this.
3. Never treat a failure as an authoritative empty mailbox. An error says
   nothing about what the mailbox holds.
4. `stateMismatch` (a `/set` with a stale `ifInState`) means re-read the
   state and rebuild the change, never resend it blindly.

Query state tracks membership (entered/left); flag changes elsewhere arrive
via `Email/changes`. Ids are opaque server strings throughout — never numeric
UIDs, never derived from names — and patch segments are escaped (RFC 6901).

## Submission uncertainty

A submission is decided only by a parsed JMAP response: a submission id
(store it — it is the durable handle — then confirm with
`EmailSubmission/get`), or a per-object refusal. No answer at all (dropped
connection) or an HTTP 5xx with no parsed body is **uncertain**: the message
may or may not be sent. Uncertain submissions are returned, not raised, so
the caller must handle them, and must be settled with
`resolve_uncertain_submission` (sent / still-a-draft / gone) — never by
resubmitting blindly, which can send twice. A 4xx or a method-level error
means nothing was created and is raised as a plain failure.

## Push and the polling fallback

`watch_push` holds the session's event source and reports state changes as a
hint to sync. Dropped connections reconnect with capped exponential backoff;
after 5 consecutive failures it raises `pushUnavailable` and the caller polls
instead. Cancellation stops between every chunk. A malformed event is logged
and skipped — the next sync reconciles whatever it announced.

## OAuth scopes (verified live)

The native Fastmail flow requests `urn:ietf:params:oauth:scope:mail` plus
`offline_access`, and nothing else. Per the OAuth Profile for Open Public
Clients (§5.4.1) the mail scope covers the JMAP `core`, `mail` and
`submission` capabilities the backend needs; `offline_access` buys the
refresh token. The per-protocol `urn:ietf:params:jmap:*` URNs are not
requested: the live registration endpoint refuses them with
`invalid_client_metadata`, and the live authorization-server metadata
does not advertise them.

## Live-interop limits

- Fastmail: discovery plus `Mailbox/get` runs against the live API in
  `tests/live/test_fastmail_live.py` (needs credentials, not run in CI).
  Writes, submission and push have never run against Fastmail.
- Generic servers (Stalwart, Cyrus, Dovecot JMAP): fixture-tested only, never
  against a live instance. Behaviour on servers that omit optional limits,
  reject `Email/changes`, or close event sources early is unverified.
- No live test covers OAuth-scoped (read-only) Fastmail grants beyond the
  `isReadOnly` fixture path.
- Blob upload size limits (`maxSizeUpload`) are enforced server-side; the
  client sends whatever the composer built and surfaces the refusal.
