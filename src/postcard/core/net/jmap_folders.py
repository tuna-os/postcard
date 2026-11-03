"""The mailbox tree of a JMAP account, read through Mailbox/get."""

from dataclasses import dataclass

from ..models.folder import FolderRole
from .jmap_session import (
    CODE_HTTP,
    JmapCall,
    JmapError,
    JmapSession,
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
            mailboxes.append(
                JmapMailbox(
                    id=mailbox_id,
                    name=str(item.get("name") or ""),
                    parent_id=path_parent,
                    role=ROLE_MAP.get(str(item.get("role") or ""), FolderRole.OTHER),
                    unread=_count(item.get("unreadEmails")),
                    total=_count(item.get("totalEmails")),
                )
            )
            visit(children.get(mailbox_id, []), mailbox_id)

    visit(children.get(None, []), "")
    for item in sorted(by_id.values(), key=key):
        if str(item.get("id")) not in seen:
            visit([item], "")
    return mailboxes


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0
