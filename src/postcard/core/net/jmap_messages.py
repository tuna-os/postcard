"""Reading messages from a JMAP mailbox: Email/query paging plus Email/get."""

import threading
from dataclasses import dataclass
from datetime import datetime

from ..models.message_header import MessageHeader
from ..threader import NO_SUBJECT
from .jmap_session import (
    CODE_HTTP,
    JmapCall,
    JmapError,
    JmapSession,
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

# A stored query state the server no longer honours: the only way on is to
# walk the mailbox over instead of asking what changed.
_EXPIRED_STATE_ERRORS = frozenset({"cannotCalculateChanges"})


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
            if error.code not in _EXPIRED_STATE_ERRORS:
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
) -> tuple[list[dict], list[str]]:
    """The messages' headers in id order, plus the ids the server no longer
    has. Ids go over in bounded batches, never the whole mailbox at once."""
    batch = min(_GET_BATCH, jmap.max_objects_in_get)
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
                        "properties": _HEADER_PROPERTIES,
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
