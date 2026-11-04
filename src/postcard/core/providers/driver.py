"""The driver interface: what every mail provider implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..net import MoveResult
from .api import (
    FetchRequest,
    FlagRequest,
    MessageRequest,
    MoveRequest,
    PerIdOutcome,
    ProviderDescriptor,
    SendOutcome,
    SendRequest,
    SyncResult,
)

if TYPE_CHECKING:
    from ..models.account import Account
    from ..net.auth import Credential


class ProviderDriver(ABC):
    """One backend's implementation of the provider operations.

    Threading follows the application's worker model: methods run on a worker
    thread, do network only, and must never touch the database or GTK. The
    account is a snapshot resolved on the main thread; the credential arrives
    resolved too, since only the caller knows which store holds it.

    Errors fail closed. A driver raises its backend's own errors (or one of
    the registry's) and never falls back to another backend's protocol --
    least of all IMAP/SMTP. A sync it cannot complete raises rather than
    returning a partial result marked complete.
    """

    @property
    @abstractmethod
    def descriptor(self) -> ProviderDescriptor:
        """This driver's stable identity, capabilities and API range."""

    @abstractmethod
    def fetch_mailbox(
        self, account: Account, credential: Credential, request: FetchRequest
    ) -> SyncResult:
        """The folder list plus one page of headers from ``request.folder``.

        ``request.folder`` names the mailbox to read, or None for the inbox.
        ``all_uids`` is set only for the newest page (offset 0), where it is
        the authoritative snapshot pruning is judged against -- unless the
        result is marked partial, in which case the caller merges without
        pruning what was not verified.
        """

    @abstractmethod
    def fetch_full_message(
        self, account: Account, credential: Credential, request: MessageRequest
    ) -> bytes:
        """The raw bytes of one message. UIDVALIDITY failures raise rather
        than risk returning another message under a reused UID."""

    @abstractmethod
    def set_flags(
        self, account: Account, credential: Credential, request: FlagRequest
    ) -> tuple[PerIdOutcome, ...]:
        """Add or remove one flag on every id, reporting each id's outcome."""

    @abstractmethod
    def move(
        self, account: Account, credential: Credential, request: MoveRequest
    ) -> MoveResult:
        """Move every id to another mailbox. A failure part-way is reported
        in the result -- the head did move -- not raised."""

    @abstractmethod
    def send(
        self, account: Account, credential: Credential, request: SendRequest
    ) -> SendOutcome:
        """Hand a fully-built message to the server. Raises when the mail did
        not go out; a Sent filing that fails after a successful send comes
        back as ``file_error`` instead, which must never send it again."""

    @abstractmethod
    def sent_copy(
        self, account: Account, credential: Credential, message_id: str
    ) -> tuple[str, int | None] | None:
        """The (UID, UIDVALIDITY) of this account's Sent copy of a message.

        Delivery confirmation for the Outbox: a retry whose last try sent
        before dying must find its own copy and not re-send. None means the
        copy is absent -- or the provider files no copy to look for, which is
        what the SENT_COPY_LOOKUP capability distinguishes. A check that
        fails raises instead of reading as absent: failing the attempt only
        retries it, while sending on could send a second copy.
        """
