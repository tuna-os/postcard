"""Reading and changing messages in a JMAP mailbox: Email/query paging plus
Email/get, per-type Email/changes, Email/set mutations, and EmailSubmission.

State tokens are opaque strings that only move forward: every delta returns
the new state beside its data, and the caller stores it after the cache
commit lands. A failure never advances a cursor and never means "empty".
"""

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ..models.message_header import MessageHeader
from ..threader import NO_SUBJECT
from .jmap_session import (
    CODE_HTTP,
    CODE_INVALID_ARGUMENTS,
    CODE_NO_SUBMISSION,
    JmapCall,
    JmapError,
    JmapSession,
    SetError,
    needs_full_sync,
    parse_set_errors,
    raise_if_cancelled,
    result_or_raise,
)

# How many ids one Email/query asks for: bounded whatever the mailbox holds.
_QUERY_PAGE = 500

# How many messages one Email/get asks for, before the server's own
# maxObjectsInGet narrows it further.
_GET_BATCH = 200

_SORT = [{"property": "receivedAt", "isAscending": False}]

_HEADER_PROPERTIES = [
    "id",
    "threadId",
    "from",
    "to",
    "cc",
    "subject",
    "sentAt",
    "receivedAt",
    "keywords",
    "preview",
    "header:Message-ID:asText",
    "header:In-Reply-To:asText",
    "header:References:asText",
]

# Properties that say where a message lives, for resolving an uncertain
# submission without fetching its headers.
_PLACEMENT_PROPERTIES = ["id", "mailboxIds"]


@dataclass(frozen=True, slots=True)
class QueryPage:
    ids: tuple[str, ...]
    total: int
    query_state: str


@dataclass(frozen=True, slots=True)
class QueryResult:
    """What a mailbox holds now: the ids, what left since the stored state
    (empty on a first walk), and the state a later sync resumes from."""

    ids: list[str]
    removed_ids: list[str]
    query_state: str


@dataclass(frozen=True, slots=True)
class MailboxSync:
    """An initial pass over one mailbox: headers, thread ids, and the query
    state a later incremental sync resumes from."""

    headers: list[MessageHeader]
    thread_ids: dict[str, str]
    removed_ids: list[str]
    not_found_ids: list[str]
    query_state: str


def message_header(item: dict) -> MessageHeader:
    """One message from an Email/get, in the form a sync stores."""
    sender_name, sender_address = next(
        iter(_email_addresses(item.get("from"))), ("", "")
    )
    recipients = _email_addresses(item.get("to"))
    copied = _email_addresses(item.get("cc"))
    recipient_name, recipient_address = next(iter(recipients), ("", ""))
    keywords = item.get("keywords")
    keywords = keywords if isinstance(keywords, dict) else {}
    return MessageHeader(
        uid=str(item.get("id") or ""),
        sender=sender_name or sender_address,
        sender_address=sender_address,
        recipient=recipient_name or recipient_address,
        recipient_address=recipient_address,
        subject=str(item.get("subject") or "") or NO_SUBJECT,
        date=_iso_date(str(item.get("sentAt") or item.get("receivedAt") or "")),
        is_unread="$seen" not in keywords,
        is_starred=bool(keywords.get("$flagged", False)),
        # The preview keeps the body's line breaks, which the list row would
        # otherwise show as ragged blank lines.
        preview=" ".join(str(item.get("preview") or "").split())[:200],
        message_id=str(item.get("header:Message-ID:asText") or ""),
        in_reply_to=str(item.get("header:In-Reply-To:asText") or ""),
        references=str(item.get("header:References:asText") or ""),
        addresses=[(sender_name, sender_address), *recipients, *copied],
    )


def thread_ids(items: list[dict]) -> dict[str, str]:
    """Each message id to its server-side thread. A message without one is
    left out, to be threaded locally from its headers instead."""
    threads = {}
    for item in items:
        email_id = item.get("id")
        thread_id = item.get("threadId")
        if isinstance(email_id, str) and isinstance(thread_id, str) and thread_id:
            threads[email_id] = thread_id
    return threads


def sync_mailbox(
    jmap: JmapSession,
    mailbox_id: str,
    *,
    since_state: str | None = None,
    cancelled: threading.Event | None = None,
) -> MailboxSync:
    """A pass over one mailbox: every id, then every header. When a stored
    query state has expired the ids are the full mailbox and removed is
    empty, so the caller reconciles instead of applying changes."""
    result = query_all_ids(
        jmap, mailbox_id, since_state=since_state, cancelled=cancelled
    )
    found, missing = fetch_emails(jmap, result.ids, cancelled)
    return MailboxSync(
        headers=[message_header(item) for item in found],
        thread_ids=thread_ids(found),
        removed_ids=result.removed_ids,
        not_found_ids=missing,
        query_state=result.query_state,
    )


def query_all_ids(
    jmap: JmapSession,
    mailbox_id: str,
    *,
    since_state: str | None = None,
    cancelled: threading.Event | None = None,
) -> QueryResult:
    """Every id in a mailbox, newest first: a full walk on a first sync, or
    only what changed since a stored query state."""
    if since_state is not None:
        try:
            return query_changes(jmap, mailbox_id, since_state, cancelled)
        except JmapError as error:
            if not needs_full_sync(error):
                raise
    return _walk(jmap, mailbox_id, cancelled)


def query_page(
    jmap: JmapSession,
    mailbox_id: str,
    position: int,
    limit: int,
    cancelled: threading.Event | None = None,
) -> QueryPage:
    """One page of a mailbox's ids, newest first."""
    raise_if_cancelled(cancelled)
    [answer] = jmap.request(
        [
            JmapCall(
                "Email/query",
                {
                    "accountId": jmap.account_id,
                    "filter": {"inMailbox": mailbox_id},
                    "sort": _SORT,
                    "position": position,
                    "limit": limit,
                },
            )
        ],
        cancelled,
    )
    payload = result_or_raise(answer)
    ids = payload.get("ids", [])
    if not isinstance(ids, list) or any(
        not isinstance(email_id, str) for email_id in ids
    ):
        raise JmapError(0, CODE_HTTP, "Email/query answered without ids")
    return QueryPage(
        ids=tuple(ids),
        total=_count(payload.get("total")),
        query_state=str(payload.get("queryState") or ""),
    )


def query_changes(
    jmap: JmapSession,
    mailbox_id: str,
    since_state: str,
    cancelled: threading.Event | None = None,
) -> QueryResult:
    """The ids that entered or left a mailbox since a stored query state."""
    added: list[str] = []
    removed: list[str] = []
    state = since_state
    while True:
        raise_if_cancelled(cancelled)
        [answer] = jmap.request(
            [
                JmapCall(
                    "Email/queryChanges",
                    {
                        "accountId": jmap.account_id,
                        "filter": {"inMailbox": mailbox_id},
                        "sort": _SORT,
                        "sinceQueryState": state,
                        "maxChanges": _QUERY_PAGE,
                        "collapseThreads": False,
                    },
                )
            ],
            cancelled,
        )
        payload = result_or_raise(answer)
        for entry in _response_list(payload, "added"):
            email_id = entry.get("emailId") if isinstance(entry, dict) else None
            if isinstance(email_id, str):
                added.append(email_id)
        removed.extend(
            gone for gone in _response_list(payload, "removed") if isinstance(gone, str)
        )
        state = str(payload.get("newQueryState") or state)
        if not payload.get("hasMoreChanges"):
            return QueryResult(ids=added, removed_ids=removed, query_state=state)


def fetch_emails(
    jmap: JmapSession,
    email_ids: list[str],
    cancelled: threading.Event | None = None,
    properties: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """The messages' headers in id order, plus the ids the server no longer
    has. Ids go over in bounded batches, never the whole mailbox at once."""
    batch = min(_GET_BATCH, jmap.max_objects_in_get)
    wanted = properties if properties else _HEADER_PROPERTIES
    found: dict[str, dict] = {}
    missing: list[str] = []
    for start in range(0, len(email_ids), batch):
        raise_if_cancelled(cancelled)
        [answer] = jmap.request(
            [
                JmapCall(
                    "Email/get",
                    {
                        "accountId": jmap.account_id,
                        "ids": email_ids[start : start + batch],
                        "properties": wanted,
                    },
                )
            ],
            cancelled,
        )
        payload = result_or_raise(answer)
        for item in _response_list(payload, "list"):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                found[item["id"]] = item
        for gone in _response_list(payload, "notFound"):
            if isinstance(gone, str) and gone not in found:
                missing.append(gone)
    ordered = [found[email_id] for email_id in email_ids if email_id in found]
    return ordered, missing


def _walk(
    jmap: JmapSession,
    mailbox_id: str,
    cancelled: threading.Event | None,
) -> QueryResult:
    ids: list[str] = []
    state = ""
    while True:
        page = query_page(jmap, mailbox_id, len(ids), _QUERY_PAGE, cancelled)
        ids.extend(page.ids)
        state = page.query_state
        if not page.ids or len(page.ids) < _QUERY_PAGE:
            return QueryResult(ids=ids, removed_ids=[], query_state=state)
        if page.total and len(ids) >= page.total:
            return QueryResult(ids=ids, removed_ids=[], query_state=state)


def _response_list(payload: dict, key: str) -> list:
    items = payload.get(key, [])
    if not isinstance(items, list):
        raise JmapError(0, CODE_HTTP, f"the JMAP answer has no {key} list")
    return items


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _email_addresses(value: object) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    return [_address(entry) for entry in value]


def _address(entry: object) -> tuple[str, str]:
    detail = entry if isinstance(entry, dict) else {}
    return str(detail.get("name") or ""), str(detail.get("email") or "").lower()


def _iso_date(value: str) -> str:
    # JMAP's "2026-09-12T08:30:00Z" is already a timestamp; fromisoformat
    # yields the offset-carrying form the database stores.
    try:
        return datetime.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return value


@dataclass(frozen=True, slots=True)
class EmailChanges:
    """What moved account-wide since a stored Email state: ids only, in no
    particular order. The caller fetches what it keeps and drops destroyed."""

    created: list[str]
    updated: list[str]
    destroyed: list[str]
    new_state: str


@dataclass(frozen=True, slots=True)
class EmailDelta:
    """One account-wide delta pass: changed headers plus destroyed ids, and
    the state the next pass resumes from -- stored only after the commit."""

    headers: list[MessageHeader]
    thread_ids: dict[str, str]
    destroyed_ids: list[str]
    not_found_ids: list[str]
    new_state: str


@dataclass(frozen=True, slots=True)
class MailboxDelta:
    """One mailbox pass against a stored query state. When is_full is set the
    stored state had expired, so ids are the whole mailbox with removed empty
    and the caller reconciles instead of applying changes."""

    headers: list[MessageHeader]
    thread_ids: dict[str, str]
    removed_ids: list[str]
    not_found_ids: list[str]
    new_state: str
    is_full: bool


def current_email_state(
    jmap: JmapSession, cancelled: threading.Event | None = None
) -> str:
    """The account's Email state now: an empty Email/get that answers with
    nothing but the state a later delta resumes from."""
    raise_if_cancelled(cancelled)
    [answer] = jmap.request(
        [
            JmapCall(
                "Email/get",
                {"accountId": jmap.account_id, "ids": [], "properties": ["id"]},
            )
        ],
        cancelled,
    )
    state = result_or_raise(answer).get("state")
    if not isinstance(state, str) or not state:
        raise JmapError(0, CODE_HTTP, "Email/get answered without a state")
    return state


def email_changes(
    jmap: JmapSession,
    since_state: str,
    cancelled: threading.Event | None = None,
) -> EmailChanges:
    """The account-wide Email created/updated/destroyed since a stored state,
    following hasMoreChanges to the end. An expired state raises
    cannotCalculateChanges for the caller to answer with a full walk."""
    created: list[str] = []
    updated: list[str] = []
    destroyed: list[str] = []
    state = since_state
    while True:
        raise_if_cancelled(cancelled)
        [answer] = jmap.request(
            [
                JmapCall(
                    "Email/changes",
                    {
                        "accountId": jmap.account_id,
                        "sinceState": state,
                        "maxChanges": _QUERY_PAGE,
                    },
                )
            ],
            cancelled,
        )
        payload = result_or_raise(answer)
        created.extend(_changed_ids(payload, "created"))
        updated.extend(_changed_ids(payload, "updated"))
        destroyed.extend(_changed_ids(payload, "destroyed"))
        state = str(payload.get("newState") or state)
        if not payload.get("hasMoreChanges"):
            return EmailChanges(
                created=created,
                updated=updated,
                destroyed=destroyed,
                new_state=state,
            )


def sync_email_delta(
    jmap: JmapSession,
    since_state: str,
    cancelled: threading.Event | None = None,
) -> EmailDelta:
    """One account-wide delta pass: the changed headers plus destroyed ids.
    An expired state raises through needs_full_sync -- the caller re-walks
    each mailbox, commits, and only then reads current_email_state."""
    changes = email_changes(jmap, since_state, cancelled)
    found, missing = fetch_emails(jmap, [*changes.created, *changes.updated], cancelled)
    return EmailDelta(
        headers=[message_header(item) for item in found],
        thread_ids=thread_ids(found),
        destroyed_ids=changes.destroyed,
        not_found_ids=missing,
        new_state=changes.new_state,
    )


def sync_mailbox_delta(
    jmap: JmapSession,
    mailbox_id: str,
    since_state: str,
    cancelled: threading.Event | None = None,
) -> MailboxDelta:
    """One mailbox pass against a stored query state: what entered or left,
    plus the headers to store. An expired state walks the mailbox over and
    sets is_full instead of failing; any other error raises with the stored
    state untouched, so the next sync resumes where this one started."""
    try:
        result = query_changes(jmap, mailbox_id, since_state, cancelled)
        is_full = False
    except JmapError as error:
        if not needs_full_sync(error):
            raise
        result = _walk(jmap, mailbox_id, cancelled)
        is_full = True
    found, missing = fetch_emails(jmap, result.ids, cancelled)
    return MailboxDelta(
        headers=[message_header(item) for item in found],
        thread_ids=thread_ids(found),
        removed_ids=result.removed_ids,
        not_found_ids=missing,
        new_state=result.query_state,
        is_full=is_full,
    )


@dataclass(frozen=True, slots=True)
class EmailSetResult:
    """What an Email/set applied: creation ids to their server echo, updated
    and destroyed ids, and per-object refusals keyed by id or creation id.
    A partial failure still applies its siblings, so errors are returned."""

    created: dict[str, dict]
    updated: list[str]
    destroyed: list[str]
    errors: dict[str, SetError] = field(default_factory=dict)


def set_emails(
    jmap: JmapSession,
    update: Mapping[str, Mapping[str, object]] | None = None,
    destroy: Sequence[str] | None = None,
    if_in_state: str | None = None,
    cancelled: threading.Event | None = None,
) -> EmailSetResult:
    """Patch and/or destroy emails in bounded /set calls: flags, mailbox
    membership, deletes. A stateMismatch raises for the caller to re-read and
    rebuild the change; per-object refusals come back in errors."""
    updates = dict(update or {})
    doomed = list(destroy or [])
    if not updates and not doomed:
        raise JmapError(0, CODE_INVALID_ARGUMENTS, "Email/set needs an update")
    batch = max(1, min(_GET_BATCH, jmap.max_objects_in_set))
    merged = EmailSetResult(created={}, updated=[], destroyed=[], errors={})
    for start in range(0, len(updates), batch):
        chunk = dict(list(updates.items())[start : start + batch])
        _apply_set(jmap, {"update": chunk}, if_in_state, cancelled, merged)
    for start in range(0, len(doomed), batch):
        _apply_set(
            jmap,
            {"destroy": doomed[start : start + batch]},
            if_in_state,
            cancelled,
            merged,
        )
    return merged


def create_emails(
    jmap: JmapSession,
    create: Mapping[str, Mapping[str, object]],
    cancelled: threading.Event | None = None,
) -> EmailSetResult:
    """Create emails from client creation ids (a draft from an uploaded blob):
    the server echo per creation id, or per-object refusals in errors."""
    if not create:
        raise JmapError(0, CODE_INVALID_ARGUMENTS, "Email/set needs a create")
    batch = max(1, min(_GET_BATCH, jmap.max_objects_in_set))
    merged = EmailSetResult(created={}, updated=[], destroyed=[], errors={})
    entries = list(create.items())
    for start in range(0, len(entries), batch):
        _apply_set(
            jmap,
            {"create": dict(entries[start : start + batch])},
            None,
            cancelled,
            merged,
        )
    return merged


def set_keywords(
    jmap: JmapSession,
    email_ids: Sequence[str],
    add: Sequence[str] = (),
    remove: Sequence[str] = (),
    cancelled: threading.Event | None = None,
) -> EmailSetResult:
    """Add and/or remove keywords ($seen, $flagged, ...) on emails. No ids is
    a no-op without network traffic; no keywords is a caller bug."""
    ids = list(email_ids)
    if not ids:
        return EmailSetResult(created={}, updated=[], destroyed=[], errors={})
    if not add and not remove:
        raise JmapError(0, CODE_INVALID_ARGUMENTS, "set_keywords needs a keyword")
    patch: dict[str, bool | None] = {f"keywords/{keyword}": True for keyword in add}
    patch.update({f"keywords/{keyword}": None for keyword in remove})
    return set_emails(
        jmap, {email_id: dict(patch) for email_id in ids}, None, None, cancelled
    )


def mark_read(
    jmap: JmapSession,
    email_ids: Sequence[str],
    read: bool,
    cancelled: threading.Event | None = None,
) -> EmailSetResult:
    """Set $seen on emails (or clear it to mark unread)."""
    if read:
        return set_keywords(jmap, email_ids, ("$seen",), (), cancelled)
    return set_keywords(jmap, email_ids, (), ("$seen",), cancelled)


def mark_starred(
    jmap: JmapSession,
    email_ids: Sequence[str],
    starred: bool,
    cancelled: threading.Event | None = None,
) -> EmailSetResult:
    """Set $flagged on emails (or clear it to unstar)."""
    if starred:
        return set_keywords(jmap, email_ids, ("$flagged",), (), cancelled)
    return set_keywords(jmap, email_ids, (), ("$flagged",), cancelled)


def move_emails(
    jmap: JmapSession,
    email_ids: Sequence[str],
    add_mailbox_ids: Sequence[str],
    remove_mailbox_ids: Sequence[str],
    cancelled: threading.Event | None = None,
) -> EmailSetResult:
    """Change mailbox membership: archive and move are the same call with
    different ids. Ids are opaque server strings, never names or numbers, and
    patch segments are escaped so any id survives the trip."""
    ids = list(email_ids)
    if not add_mailbox_ids and not remove_mailbox_ids:
        raise JmapError(0, CODE_INVALID_ARGUMENTS, "move_emails needs a mailbox")
    if not ids:
        return EmailSetResult(created={}, updated=[], destroyed=[], errors={})
    patch: dict[str, bool | None] = {
        f"mailboxIds/{_escape_segment(mailbox_id)}": True
        for mailbox_id in add_mailbox_ids
    }
    patch.update(
        {
            f"mailboxIds/{_escape_segment(mailbox_id)}": None
            for mailbox_id in remove_mailbox_ids
        }
    )
    return set_emails(
        jmap, {email_id: dict(patch) for email_id in ids}, None, None, cancelled
    )


@dataclass(frozen=True, slots=True)
class SubmitRequest:
    """One message to hand to the server's outbox: the draft's id, and
    optionally whose identity sends it and what a success updates."""

    email_id: str
    identity_id: str | None = None
    envelope: Mapping[str, object] | None = None
    on_success_update: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """What a submission attempt found out. Exactly one of submission_id and
    error is set on a decided attempt; is_uncertain means the request went out
    but no answer came back, so the message may or may not be sent -- resolve
    it with resolve_uncertain_submission, never by resubmitting blindly."""

    email_id: str
    submission_id: str | None = None
    error: SetError | None = None
    is_uncertain: bool = False
    cause: str = ""


def submit_email(
    jmap: JmapSession,
    draft: SubmitRequest,
    cancelled: threading.Event | None = None,
) -> SubmissionResult:
    """Submit a draft for sending. The submission id is the durable handle:
    store it before confirming, and confirm with confirm_submission. A refused
    submission comes back in error; a lost answer comes back is_uncertain."""
    if not jmap.submission_supported:
        raise JmapError(0, CODE_NO_SUBMISSION, "the server offers no EmailSubmission")
    creation = _submission_creation(draft)
    try:
        [answer] = jmap.request(
            [
                JmapCall(
                    "EmailSubmission/set",
                    {"accountId": jmap.account_id, "create": {"s1": creation}},
                )
            ],
            cancelled,
        )
    except OSError as error:
        # No answer at all: the server may have queued the message before the
        # connection died. Report, don't retry -- a retry may send it twice.
        return SubmissionResult(draft.email_id, is_uncertain=True, cause=str(error))
    except JmapError as error:
        if error.status >= 500:
            return SubmissionResult(draft.email_id, is_uncertain=True, cause=str(error))
        raise
    return _submission_result(draft.email_id, result_or_raise(answer))


def _submission_creation(draft: SubmitRequest) -> dict[str, object]:
    """The EmailSubmission/set create object: the draft plus whatever sending
    options the caller set."""
    creation: dict[str, object] = {"emailId": draft.email_id}
    if draft.identity_id is not None:
        creation["identityId"] = draft.identity_id
    if draft.envelope is not None:
        creation["envelope"] = dict(draft.envelope)
    if draft.on_success_update is not None:
        creation["onSuccessUpdateEmail"] = dict(draft.on_success_update)
    return creation


def _submission_result(email_id: str, payload: dict) -> SubmissionResult:
    """The decided outcome of a submission: its durable handle, or the refusal
    that stopped it."""
    created = payload.get("created", {})
    if isinstance(created, dict) and isinstance(created.get("s1"), dict):
        submission_id = created["s1"].get("id")
        if isinstance(submission_id, str) and submission_id:
            return SubmissionResult(email_id, submission_id=submission_id)
    errors = parse_set_errors(payload, "notCreated")
    if "s1" in errors:
        return SubmissionResult(email_id, error=errors["s1"])
    raise JmapError(0, CODE_HTTP, "EmailSubmission/set answered without a result")


def confirm_submission(
    jmap: JmapSession,
    submission_id: str,
    cancelled: threading.Event | None = None,
) -> dict | None:
    """The stored submission object (sendAt, undoStatus, ...), or None when
    the server no longer has it."""
    raise_if_cancelled(cancelled)
    [answer] = jmap.request(
        [
            JmapCall(
                "EmailSubmission/get",
                {
                    "accountId": jmap.account_id,
                    "ids": [submission_id],
                    "properties": ["id", "emailId", "sendAt", "undoStatus"],
                },
            )
        ],
        cancelled,
    )
    payload = result_or_raise(answer)
    for item in _response_list(payload, "list"):
        if isinstance(item, dict) and item.get("id") == submission_id:
            return item
    return None


def was_submitted(email_item: dict, sent_mailbox_id: str) -> bool:
    """Whether a fetched email sits in the sent mailbox -- the server files it
    there (or destroys the draft) once a submission goes through."""
    mailboxes = email_item.get("mailboxIds")
    return isinstance(mailboxes, dict) and sent_mailbox_id in mailboxes


def resolve_uncertain_submission(
    jmap: JmapSession,
    email_id: str,
    sent_mailbox_id: str,
    cancelled: threading.Event | None = None,
) -> bool | None:
    """Settle an uncertain submission without sending anything: True when the
    message reached the sent mailbox, False when it is still only a draft
    (safe to retry on the user's say-so), None when the draft is gone and
    nobody can tell -- surface that, don't resubmit."""
    found, _missing = fetch_emails(
        jmap, [email_id], cancelled, properties=_PLACEMENT_PROPERTIES
    )
    if not found:
        return None
    return was_submitted(found[0], sent_mailbox_id)


def _changed_ids(payload: dict, key: str) -> list[str]:
    """The bare ids under a /changes created/updated/destroyed key."""
    return [
        email_id
        for email_id in _response_list(payload, key)
        if isinstance(email_id, str)
    ]


def _apply_set(
    jmap: JmapSession,
    changes: dict,
    if_in_state: str | None,
    cancelled: threading.Event | None,
    merged: EmailSetResult,
) -> None:
    """One Email/set call, merging what it applied into the running result.
    The merged lists mutate in place across chunks of one logical change."""
    raise_if_cancelled(cancelled)
    arguments = {"accountId": jmap.account_id, **changes}
    if if_in_state is not None:
        arguments["ifInState"] = if_in_state
    [answer] = jmap.request([JmapCall("Email/set", arguments)], cancelled)
    payload = result_or_raise(answer)
    created = payload.get("created", {})
    if isinstance(created, dict):
        merged.created.update(
            {key: value for key, value in created.items() if isinstance(value, dict)}
        )
    for key, collected in (
        ("updated", merged.updated),
        ("destroyed", merged.destroyed),
    ):
        collected.extend(_changed_ids(payload, key) if key in payload else [])
    for key in ("notCreated", "notUpdated", "notDestroyed"):
        if key in payload:
            merged.errors.update(parse_set_errors(payload, key))


def _escape_segment(value: str) -> str:
    """Escape one JSON-pointer segment (RFC 6901): mailbox ids are opaque and
    may carry characters the patch syntax would otherwise read."""
    return value.replace("~", "~0").replace("/", "~1")
