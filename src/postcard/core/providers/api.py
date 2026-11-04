"""The versioned boundary every mail provider implements.

A provider is one backend Postcard can sync an account against: IMAP/SMTP,
Microsoft Graph, Tuta, Proton, JMAP, or an external add-on. Each one has a
stable id (which is what ``Account.protocol`` stores), a descriptor naming
what it can do, and a driver implementing the operations below. Callers
dispatch through the registry and never branch on the id themselves, so a new
provider means a new driver, not new conditionals in every caller.

Add-ons are trusted code running with the application's own mail and
credential privileges -- this boundary is about versioning and dispatch, not
an OS security sandbox. See ``docs/provider-checklist.md``.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from ..models.mailbox import MailboxInfo
from ..models.message_header import MessageHeader

# The driver interface version this build speaks. A driver declares the range
# it was written against on its descriptor; the registry loads it only when
# this version falls inside that range.
PROVIDER_API_VERSION = 1

# Installed add-ons advertise a driver under this importlib.metadata group,
# with the entry point's name as the provider id.
ENTRY_POINT_GROUP = "postcard.providers"

# Beside postcard.db: one trusted provider id per line. Builtins need no entry.
TRUST_FILE_NAME = "trusted_providers"


class Capability(StrEnum):
    """One thing a provider may or may not be able to do.

    Capabilities drive dispatch (IDLE_PUSH) and the UI (SEND disables the
    composer's Send button). A driver omits what it cannot do rather than
    raising only when called, except where no capability exists yet -- then it
    raises its backend's own ``unsupported_operation`` error.
    """

    FETCH = "fetch"
    FETCH_BODY = "fetch_body"
    SET_FLAGS = "set_flags"
    MOVE = "move"
    SEND = "send"
    SENT_COPY_LOOKUP = "sent_copy_lookup"
    IDLE_PUSH = "idle_push"


class SyncCompleteness(StrEnum):
    """Whether a fetch saw everything it set out to see.

    COMPLETE means the snapshot is authoritative: whatever is absent from
    ``all_uids`` is gone from the server and may be pruned locally. PARTIAL
    means something was skipped (a folder that would not answer, a page that
    failed) and the caller must merge without pruning what it did not verify.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """A provider's stable identity and what it can do.

    The id is the whole contract with stored accounts: it must never change
    once accounts exist under it, and a driver claiming another id is
    rejected. Capabilities are read by the UI without touching the network.
    """

    id: str
    display_name: str
    capabilities: frozenset[Capability]
    min_api_version: int = PROVIDER_API_VERSION
    max_api_version: int = PROVIDER_API_VERSION

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def is_compatible_with(self, api_version: int = PROVIDER_API_VERSION) -> bool:
        return self.min_api_version <= api_version <= self.max_api_version


# The requests below are the only arguments a driver operation takes beyond
# the account and credential. Frozen, so a worker thread's snapshot cannot be
# mutated from under it, and UI-independent: no widgets, no database rows.


@dataclass(frozen=True, slots=True)
class FetchRequest:
    folder: str | None
    limit: int
    offset: int
    should_count_unread: bool


@dataclass(frozen=True, slots=True)
class MessageRequest:
    folder_name: str
    uid: str
    uidvalidity: int | None


@dataclass(frozen=True, slots=True)
class FlagRequest:
    folder_name: str
    uids: tuple[str, ...]
    flag: str
    should_add: bool
    uidvalidity: int | None


@dataclass(frozen=True, slots=True)
class MoveRequest:
    folder_name: str
    uids: tuple[str, ...]
    destination: str
    uidvalidity: int | None


@dataclass(frozen=True, slots=True)
class SendRequest:
    from_addr: str
    recipients: tuple[str, ...]
    raw: bytes


@dataclass(frozen=True, slots=True)
class PerIdOutcome:
    """How one id in a batch operation fared.

    Batch operations report per id rather than failing the batch on the first
    miss: a move that fails part-way still moved its head, and the caller has
    to know where it stopped. ``error_code`` is the backend's own short code.
    """

    server_id: str
    succeeded: bool
    error_code: str | None = None


@dataclass
class SyncResult:
    folders: list[MailboxInfo] = field(default_factory=list)
    messages: list[MessageHeader] = field(default_factory=list)
    folder: str = "INBOX"
    exists: int = 0  # total messages in the selected mailbox
    offset: int = 0  # how far back from the newest this fetch reached
    all_uids: set[str] | None = None  # authoritative UID snapshot for newest page
    unread_counts: dict[str, int] = field(default_factory=dict)  # by mailbox name
    uidvalidity: int | None = None  # what the UIDs above belong to; None on Graph
    completeness: SyncCompleteness = SyncCompleteness.COMPLETE
    outcomes: tuple[PerIdOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """How a send went. The mail left; this says about its Sent copy.

    filed_uid is the UID the server filed it under (UIDPLUS), or None when the
    server files its own copy (Gmail, Exchange) or reports none. file_error is
    set only when the send succeeded but the Sent filing failed after it --
    the mail went out, so this must never send it again, only say so.
    """

    filed_uid: str | None
    uidvalidity: int | None
    file_error: Exception | None


class ProviderError(Exception):
    """A provider could not be reached through the registry."""


class UnknownProviderError(ProviderError):
    """No driver is registered for this id: nothing syncs, nothing falls back."""


class DisabledProviderError(ProviderError):
    """A driver exists for this id but is not trusted, so it stays unloaded."""


class IncompatibleProviderError(ProviderError):
    """A driver exists for this id but speaks another API version."""


class DuplicateProviderError(ProviderError):
    """Two drivers claim one id. The first registration wins, always."""
