# Tuta ordinary send and its crash semantics (issues #4, #77)

How a Tuta mail goes out, what "uncertain" means at every layer, and why a
crash mid-send fails closed instead of retrying.

## The submit model

Sending is draft-create then submit in one helper invocation
(`send` in `native/tuta/src/main.rs`): the helper stages a draft through
`DraftService`, resolves attachment keys, and posts it to
`SendDraftService`. Ordinary (unencrypted) delivery only, and only with the
composer's explicit confirmation -- without `ordinary_confirmed` the helper
refuses.

Three facts shape everything below:

- The server assigns its own Message-ID at submit. Our id is never
  submitted and never matches a server copy, so `sent_copy` on the Tuta
  driver honestly returns None and `SENT_COPY_LOOKUP` stays off.
- The server files the Sent copy itself. The success path links up through
  `filed_uid` (the sent mail's compound id), which the next sync merges on.
- Failures split two ways. Definitive ones (`send_failed` and the
  validation codes) sent nothing and are safe to retry. `send_uncertain`
  means the mail may have gone out -- timeouts, lost replies, server-side
  failures after accept. The caller must terminal-fail it for a human to
  check Sent, never auto-retry it. See `submit_error` in `main.rs` for the
  mapping, and `send_mime` in `src/postcard/core/net/tuta_messages.py`.

## Live ambiguity: send_uncertain fails closed

When the helper reports `send_uncertain`, both send paths record terminal
`failed` with the fixed `send_uncertain` wording and keep the mail visibly
in the Outbox: the composer in `_on_send_failed`
(`src/postcard/composer_window.py`), the drain in `_record_drained_error`
(`src/postcard/window.py`). No automatic retry, on either path.

## Crash ambiguity: recovery fails closed too (issue #77)

A process crash between server accept and local completion leaves a
`sending` outbox row with no witness: the mail may or may not have left.
Every other driver settles this by looking for its own copy in Sent before
resending -- the retry guard in `send_queued`. Tuta has no such guard: its
`sent_copy` cannot match, so a blind retry resends, a duplicate with no
detection. A narrow window (~100ms), the same class SMTP had before #10.

Mitigations investigated, in order:

1. **Server-side idempotency: unavailable.** The pinned `SendDraftData`
   carries no idempotency key or client request id, and the Tuta API is
   undocumented with no duplicate suppression. There is nothing to send
   that the server would ded duplicate on.
2. **Crash-atomic claim plus fail-closed recovery: implemented.** The
   `sending` claim is the crash marker, and it commits durably
   (`synchronous = FULL` in `claim_outbox_email`) on the main thread
   before the submit worker spawns -- so a crash at any point after it,
   process or power, finds `sending`, never a retryable state. Recovery
   (`recover_queued_sends`) then fails Tuta `sending` rows closed to
   terminal `failed` with the same `send_uncertain` state and wording the
   live path records, while other protocols' rows ready again for their
   guarded retry. No new column: the marker reuses `OutboxState`, and the
   message reuses `last_error`. A future SEND-capable driver without
   `SENT_COPY_LOOKUP` must join the ambiguous set in `recover_queued_sends`,
   or its crash retries resend too.
3. **Post-recovery Sent-scan heuristic: rejected.** Matching by
   subject/recipients/date is fuzzy by construction: a miss still
   duplicates, a false match drops a mail the user believes sent. It adds
   network and fuzz for no safety gain over failing closed.

## Residual risk, honestly

- **False "check Sent" prompts.** Recovery cannot tell a crash before the
  submit from one after it, so every crash mid-send -- including before the
  worker runs, or during its slowest pre-submit step (credential fetch) --
  terminal-fails a mail that may never have left. Fail-closed means the
  user checks Sent for nothing, never that a duplicate goes out silently.
- **Orphan server drafts.** A crash between draft-create and submit leaves
  the staged draft in the server's Drafts folder (the helper's
  `cleanup_draft` only runs when the helper survives to report). Clutter,
  not a duplicate: the recovery never retries, so no second draft submits.
- **No toast at recovery.** The stuck Outbox row and its `last_error` are
  the surface, as in the live path, but no toast fires when recovery fails
  the row -- recovery runs inside the drain before any result exists to
  report. The event is logged (`recovered %d interrupted send(s)`).
- **Backup rollback can still duplicate.** Restoring a database backup from
  before the claim resurrects a retryable row for a mail already sent, and
  the drain will send it again. This is outside the crash model -- no
  client-side queue survives its own rollback -- but SMTP's Sent guard
  covers even that, and Tuta's cannot.

What is closed: process crashes and power loss at any point from the claim
on -- before submit, between accept and completion, between filing steps --
can neither resend a Tuta mail nor lose track of one silently. The
ambiguous case always lands terminal-failed with the `send_uncertain`
message, awaiting a human.
