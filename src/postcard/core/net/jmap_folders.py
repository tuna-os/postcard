"""The mailbox tree of a JMAP account, read through Mailbox/get and kept fresh
through Mailbox/changes against a stored state token."""

import threading
from dataclasses import dataclass

from ..models.folder import FolderRole
from .jmap_session import (
    CODE_HTTP,
    JmapCall,
    JmapError,
    JmapSession,
    raise_if_cancelled,
    result_or_raise,
)

# The server states each mailbox's purpose outright, which is the only way to
# tell a localized "Gesendete Objekte" is the sent folder. Anything else the
# server sends (snoozed, important, ...) reads as OTHER.
ROLE_MAP: dict[str, FolderRole] = {
    "inbox": FolderRole.INBOX,
    "sent": FolderRole.SENT,
    "drafts": FolderRole.DRAFTS,
    "trash": FolderRole.TRASH,
    "junk": FolderRole.JUNK,
    "archive": FolderRole.ARCHIVE,
}

# The sidebar order of the role mailboxes; everything else follows by name.
_ROLE_ORDER = tuple(ROLE_MAP.values())

# Mailboxes are dozens, not thousands, so one changes page holds them all;
# hasMoreChanges still loops for the pathological account.
_CHANGES_PAGE = 200

_PROPERTIES = [
    "id",
    "name",
    "parentId",
    "role",
    "unreadEmails",
    "totalEmails",
    "sortOrder",
]


@dataclass(frozen=True, slots=True)
class JmapMailbox:
    id: str
    name: str
    parent_id: str  # "" at the top level
    role: FolderRole
    unread: int
    total: int


def list_mailboxes(jmap: JmapSession) -> list[JmapMailbox]:
    """Every mailbox, parents before their children. One Mailbox/get without
    ids returns them all; mailboxes are dozens, not thousands."""
    [answer] = jmap.request(
        [
            JmapCall(
                "Mailbox/get",
                {
                    "accountId": jmap.account_id,
                    "properties": _PROPERTIES,
                },
            )
        ]
    )
    items = result_or_raise(answer).get("list", [])
    if not isinstance(items, list):
        raise JmapError(0, CODE_HTTP, "Mailbox/get answered without a mailbox list")
    return build_tree(items)


def build_tree(items: list[dict]) -> list[JmapMailbox]:
    """Flatten mailboxes depth first. A mailbox whose parent is missing
    becomes a root rather than vanishing, and so does anything a parent cycle
    would otherwise strand."""
    by_id = {
        item["id"]: item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    children: dict[str | None, list[dict]] = {}
    for item in by_id.values():
        parent = item.get("parentId")
        slot = parent if isinstance(parent, str) and parent in by_id else None
        children.setdefault(slot, []).append(item)

    def key(item: dict) -> tuple[int, int, str]:
        role = ROLE_MAP.get(str(item.get("role") or ""))
        rank = _ROLE_ORDER.index(role) if role is not None else len(_ROLE_ORDER)
        order = item.get("sortOrder")
        return (
            rank,
            order if isinstance(order, int) else 0,
            str(item.get("name") or "").casefold(),
        )

    mailboxes: list[JmapMailbox] = []
    seen: set[str] = set()

    def visit(siblings: list[dict], path_parent: str) -> None:
        for item in sorted(siblings, key=key):
            mailbox_id = str(item.get("id"))
            if mailbox_id in seen:
                continue
            seen.add(mailbox_id)
            mailboxes.append(_mailbox(item, path_parent))
            visit(children.get(mailbox_id, []), mailbox_id)

    visit(children.get(None, []), "")
    for item in sorted(by_id.values(), key=key):
        if str(item.get("id")) not in seen:
            visit([item], "")
    return mailboxes


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _mailbox(item: dict, parent_id: str) -> JmapMailbox:
    """One Mailbox/get object as the sync keeps it: the server's role, not the
    (possibly localized) name, says what the mailbox is for."""
    return JmapMailbox(
        id=str(item.get("id")),
        name=str(item.get("name") or ""),
        parent_id=parent_id,
        role=ROLE_MAP.get(str(item.get("role") or ""), FolderRole.OTHER),
        unread=_count(item.get("unreadEmails")),
        total=_count(item.get("totalEmails")),
    )


@dataclass(frozen=True, slots=True)
class MailboxDelta:
    """What moved in the mailbox tree since a stored Mailbox state: changed
    mailboxes (created and updated alike) plus destroyed ids, and the state
    the next pass resumes from -- stored only after the commit."""

    changed: list[JmapMailbox]
    destroyed_ids: list[str]
    new_state: str


def current_mailbox_state(
    jmap: JmapSession, cancelled: threading.Event | None = None
) -> str:
    """The account's Mailbox state now: an empty Mailbox/get that answers with
    nothing but the state a later delta resumes from."""
    raise_if_cancelled(cancelled)
    [answer] = jmap.request(
        [JmapCall("Mailbox/get", {"accountId": jmap.account_id, "ids": []})],
        cancelled,
    )
    state = result_or_raise(answer).get("state")
    if not isinstance(state, str) or not state:
        raise JmapError(0, CODE_HTTP, "Mailbox/get answered without a state")
    return state


def mailbox_changes(
    jmap: JmapSession,
    since_state: str,
    cancelled: threading.Event | None = None,
) -> MailboxDelta:
    """The mailbox tree's created/updated/destroyed since a stored state,
    following hasMoreChanges to the end. An expired state raises
    cannotCalculateChanges for the caller to answer with list_mailboxes plus
    current_mailbox_state."""
    changed: list[JmapMailbox] = []
    destroyed: list[str] = []
    state = since_state
    while True:
        raise_if_cancelled(cancelled)
        [answer] = jmap.request(
            [
                JmapCall(
                    "Mailbox/changes",
                    {
                        "accountId": jmap.account_id,
                        "sinceState": state,
                        "maxChanges": _CHANGES_PAGE,
                    },
                )
            ],
            cancelled,
        )
        payload = result_or_raise(answer)
        refreshed = [
            *_changed_ids(payload, "created"),
            *_changed_ids(payload, "updated"),
        ]
        if refreshed:
            changed.extend(_fetch_mailboxes(jmap, refreshed, cancelled))
        destroyed.extend(_changed_ids(payload, "destroyed"))
        state = str(payload.get("newState") or state)
        if not payload.get("hasMoreChanges"):
            return MailboxDelta(
                changed=changed, destroyed_ids=destroyed, new_state=state
            )


def _fetch_mailboxes(
    jmap: JmapSession, mailbox_ids: list[str], cancelled: threading.Event | None
) -> list[JmapMailbox]:
    """The changed mailboxes by id. The parent comes from each object's own
    parentId -- without the full tree there is nothing to fix it against, so
    the caller reconciles orphans when it merges."""
    raise_if_cancelled(cancelled)
    [answer] = jmap.request(
        [
            JmapCall(
                "Mailbox/get",
                {
                    "accountId": jmap.account_id,
                    "ids": mailbox_ids,
                    "properties": _PROPERTIES,
                },
            )
        ],
        cancelled,
    )
    items = result_or_raise(answer).get("list", [])
    if not isinstance(items, list):
        raise JmapError(0, CODE_HTTP, "Mailbox/get answered without a mailbox list")
    mailboxes = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        parent = item.get("parentId")
        mailboxes.append(_mailbox(item, parent if isinstance(parent, str) else ""))
    return mailboxes


def _changed_ids(payload: dict, key: str) -> list[str]:
    """The bare ids under a /changes created/updated/destroyed key."""
    items = payload.get(key, [])
    if not isinstance(items, list):
        raise JmapError(0, CODE_HTTP, f"the JMAP answer has no {key} list")
    return [item for item in items if isinstance(item, str)]
