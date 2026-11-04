import logging
import re
import threading
import time
import urllib.error
import urllib.request
import weakref
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from functools import lru_cache
from gettext import gettext as _
from urllib.parse import quote

from .core.compose import extract_recipients, rotate_message_id
from .core.lifecycle import Lifecycle
from .core.mime.message_parser import clock_format
from .core.models.account import (
    FASTMAIL_HOST,
    PROTOCOL_GRAPH,
    PROTOCOL_IMAP,
    PROTOCOL_JMAP,
    PROTOCOL_PROTON,
    PROTOCOL_TUTA,
    Account,
)
from .core.models.email import Email
from .core.models.folder import Folder, FolderRole

# Re-exported: callers reach MessageHeader through mail_sync, which is where it
# is built. It lives in core.models so core.store can accept one directly.
from .core.models.message_header import MessageHeader
from .core.net import (
    MoveResult,
    graph_folders,
    graph_messages,
    graph_send,
    jmap_folders,
    jmap_messages,
    tuta_messages,
)
from .core.net.auth import Credential
from .core.net.fastmail_oauth import FASTMAIL_JMAP_SESSION_URL
from .core.net.graph_session import GraphSession
from .core.net.imap_session import (
    ATTR_NOSELECT,
    GMAIL_CAPABILITY,
    IDLE_CAPABILITY,
    FetchedHeader,
    ImapError,
    ImapSession,
    MailboxInfo,
    UidValidityError,
    decode_mailbox_name,
)
from .core.net.jmap_session import (
    CODE_HTTP,
    CODE_NO_SUBMISSION,
    CODE_READ_ONLY,
    CODE_UNCERTAIN_SUBMISSION,
    JmapError,
    JmapSession,
    Session,
    discover,
    needs_full_sync,
)
from .core.net.proton_session import ProtonError
from .core.net.smtp_session import SmtpSession
from .core.net.tuta_session import TutaError, TutaSession

# Re-exported: SyncResult and SendOutcome are the drivers' UI-independent
# results, so they live in core.providers; callers keep reaching them here.
from .core.providers import (
    Capability,
    FetchRequest,
    FlagRequest,
    MessageRequest,
    MoveRequest,
    PerIdOutcome,
    ProviderDescriptor,
    ProviderDriver,
    ProviderError,
    ProviderRegistry,
    SendOutcome,
    SendRequest,
    SyncResult,
)
from .core.store.database import Database, OutboxState, QueuedSend
from .core.threader import NO_SUBJECT

logger = logging.getLogger(__name__)

# how many recent messages to pull per sync
RECENT_LIMIT = 50

# RFC 8058: the body is the whole request, and the server matches it verbatim.
ONE_CLICK_BODY = b"List-Unsubscribe=One-Click"

# Gmail nests its special folders under an unselectable "[Gmail]" container.
# It isn't a real mailbox, so it's hidden and its children sit at the top level.
NAMESPACE_ROOTS = ("[Gmail]", "[Google Mail]")


# How servers spell each role, in the order role_for_folder tries them: the
# first match wins, so "Sent/Drafts" is a sent mailbox rather than a drafts one.
# Word boundaries rather than a bare substring, so a mailbox the user named
# "Consent forms", "Presentations" or "Unsent Messages" isn't taken for Sent.
_ROLE_PATTERNS: tuple[tuple[re.Pattern[str], FolderRole], ...] = (
    (re.compile(r"^inbox$", re.IGNORECASE), FolderRole.INBOX),
    (re.compile(r"\bsent\b", re.IGNORECASE), FolderRole.SENT),
    (re.compile(r"\bdrafts?\b", re.IGNORECASE), FolderRole.DRAFTS),
    (re.compile(r"\b(trash|deleted|bin)\b", re.IGNORECASE), FolderRole.TRASH),
    (re.compile(r"\b(junk|spam)\b", re.IGNORECASE), FolderRole.JUNK),
    (re.compile(r"\b(archives?|all mail)\b", re.IGNORECASE), FolderRole.ARCHIVE),
    (re.compile(r"\b(starred|flagged)\b", re.IGNORECASE), FolderRole.STARRED),
)

# The canonical IMAP inbox name. Servers vary the casing, so this is the
# fallback rather than something to compare against -- see inbox_name.
INBOX_MAILBOX = "INBOX"

# Folders Postcard maintains itself rather than mirroring from the server.
# Outbox is local-only (prune_folders keeps it); Sent and Drafts are created on
# demand when the server has no folder of that role.
OUTBOX_FOLDER = "Outbox"
SENT_FOLDER = "Sent"
DRAFTS_FOLDER = "Drafts"


# How long a pooled connection may sit idle before it is probed with a NOOP.
# Back-to-back operations run on a socket the one before them just used, where
# the probe is pure latency; an idle connection may have been hung up on.
PROBE_IDLE_SECONDS = 10.0


@dataclass
class _Pooled:
    """One account's slot in the connection pool."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    session: ImapSession | None = None
    used_at: float = 0.0
    # Bumped by close_sessions, so an operation still running against a dropped
    # account logs its connection out instead of storing it in a stale slot.
    generation: int = 0


_pool: dict[int, _Pooled] = {}
_pool_lock = threading.Lock()


@dataclass
class _GraphState:
    """What a Graph account keeps between operations, in place of a connection.

    Graph is stateless HTTP, so there is nothing to pool -- but a well-known
    folder's id never changes once it exists, and a delta link turns the next
    UID snapshot of a folder into a request for what changed instead of a walk
    over all of it. Which folders exist can still change, so well_known is
    only kept for good once graph_folders.is_complete accepts it.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    well_known: dict[str, str] | None = None
    deltas: dict[str, graph_messages.DeltaState] = field(default_factory=dict)


# Guarded by _pool_lock, like the IMAP pool it sits beside.
_graph_states: dict[int, _GraphState] = {}


def _graph_state(account_id: int) -> _GraphState:
    with _pool_lock:
        return _graph_states.setdefault(account_id, _GraphState())


@dataclass(frozen=True)
class _JmapQuery:
    """One folder's id snapshot plus the query state that brings it up to
    date. Replaced wholesale, only after the fetch that produced it lands."""

    state: str
    ids: frozenset[str]


@dataclass
class _JmapState:
    """What a JMAP account keeps between operations, in place of a connection.

    JMAP is stateless HTTP, so there is nothing to pool -- but discovery is
    one round trip saved by keeping the session, and a stored state token
    turns the next pass over a folder into a request for what changed instead
    of a walk over all of it. A restart drops all of this and the next sync
    walks everything over, which stays correct because every snapshot it
    builds is still authoritative.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    session: Session | None = None
    mailbox_state: str | None = None
    mailboxes: dict[str, jmap_folders.JmapMailbox] = field(default_factory=dict)
    queries: dict[str, _JmapQuery] = field(default_factory=dict)


# Guarded by _pool_lock, like the IMAP pool it sits beside.
_jmap_states: dict[int, _JmapState] = {}


def _jmap_state(account_id: int) -> _JmapState:
    with _pool_lock:
        return _jmap_states.setdefault(account_id, _JmapState())


# RFC 2177 lets a server drop an IDLE left running for 30 minutes, so it is
# renewed well before that. A renewal is two short lines.
IDLE_RENEW_SECONDS = 20 * 60

# How often an account with no push -- Graph, or a server without IDLE -- is
# asked for its inbox instead.
PUSHLESS_POLL_SECONDS = 60

# The wait before reconnecting a watcher whose connection failed. It doubles
# per failure up to the cap, and resets once a connection is watching again.
WATCH_RETRY_SECONDS = 15
WATCH_RETRY_MAX_SECONDS = 15 * 60


class _InboxWatcher:
    """Calls on_change(account id), from its own thread, when an inbox changes.

    An IMAP account gets a connection of its own held in IDLE on the inbox:
    IDLE blocks the connection it runs on, so it can't be the pooled one. A
    pushless account (Graph, Tuta, Proton), or a server without IDLE, offers
    no push a desktop client can receive, so it is polled instead. on_change
    also fires every time a connection starts watching, for whatever arrived
    while none was.

    The account is a snapshot and the thread touches neither the database nor
    GTK; credential_for is called on the thread, like every worker's.
    """

    def __init__(
        self,
        account: Account,
        credential_for: Callable[[Account], Credential | None],
        on_change: Callable[[int], object],
    ) -> None:
        self._account = account
        self._credential_for = credential_for
        self._on_change = on_change
        self._stop = threading.Event()
        self._session: ImapSession | None = None
        self._retry = WATCH_RETRY_SECONDS

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        """Returns at once: the thread notices and exits on its own."""
        self._stop.set()
        session = self._session
        if session is not None:
            session.interrupt()

    def _changed(self) -> None:
        if not self._stop.is_set():
            self._on_change(self._account.id)

    def _run(self) -> None:
        if self._should_idle():
            self._watch_idle()
        while not self._stop.wait(PUSHLESS_POLL_SECONDS):
            self._changed()

    def _should_idle(self) -> bool:
        """Whether this account's provider can hold an IDLE watch.

        Asked of the driver, not the protocol string: a provider the registry
        cannot serve -- unknown, untrusted, incompatible -- polls instead of
        opening an IMAP connection it was never meant to use. The sync itself
        still fails closed with the registry's error; the watcher just must
        not be the thing that falls back.
        """
        try:
            driver = driver_for(self._account.protocol)
        except ProviderError:
            logger.warning(
                "no mail provider serves %s (account %s); polling its inbox",
                self._account.protocol,
                self._account.email,
            )
            return False
        return driver.descriptor.supports(Capability.IDLE_PUSH)

    def _watch_idle(self) -> None:
        """Returns once stopped, or once the server turns out to have no IDLE."""
        while not self._stop.is_set():
            try:
                if not self._idle_on_inbox():
                    return
            except Exception:
                if self._stop.is_set():
                    return
                logger.warning(
                    "lost the IDLE connection to %s (account %s); retrying in %d s",
                    self._account.imap_host,
                    self._account.email,
                    self._retry,
                    exc_info=True,
                )
            self._stop.wait(self._retry)
            self._retry = min(self._retry * 2, WATCH_RETRY_MAX_SECONDS)

    def _idle_on_inbox(self) -> bool:
        """Watch over one connection until stopped. False: the server has no
        IDLE. Raises when the connection fails."""
        account = self._account
        credential = self._credential_for(account)
        if credential is None:
            raise ImapError(f"no credential for {account.email}")
        session = ImapSession(
            account.imap_host, account.imap_port, account.imap_security
        )
        self._session = session
        try:
            session.connect()
            # stop() may have come before there was a socket to interrupt.
            if self._stop.is_set():
                return True
            session.sign_in(credential)
            session.refresh_capabilities()
            if not session.has_capability(IDLE_CAPABILITY):
                logger.info(
                    "%s has no IDLE; polling the inbox of %s instead",
                    account.imap_host,
                    account.email,
                )
                return False
            session.select(inbox_name([box.name for box in session.list_folders()]))
            self._retry = WATCH_RETRY_SECONDS
            self._changed()
            while not self._stop.is_set():
                if session.idle(IDLE_RENEW_SECONDS):
                    self._changed()
        finally:
            self._session = None
            session.logout()
        return True


# One watcher per account, guarded by _pool_lock like the pool beside it.
_watchers: dict[int, _InboxWatcher] = {}


def watch_inboxes(
    accounts: Iterable[Account],
    credential_for: Callable[[Account], Credential | None],
    on_change: Callable[[int], object],
) -> None:
    """Keep one inbox watcher running per account in `accounts`, and no other.

    Idempotent, so it is safe to call whenever the accounts may have changed.
    on_change runs on a watcher thread with an account id; see _InboxWatcher.
    close_sessions stops watchers too, and the next call here restarts them.
    """
    wanted = {account.id: account for account in accounts}
    with _pool_lock:
        stale = [_watchers.pop(one) for one in list(_watchers) if one not in wanted]
        fresh = {
            one: _InboxWatcher(account, credential_for, on_change)
            for one, account in wanted.items()
            if one not in _watchers
        }
        _watchers.update(fresh)
    for watcher in stale:
        watcher.stop()
    for watcher in fresh.values():
        watcher.start()


def _signed_in(account: Account, credential: Credential) -> ImapSession:
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()
    try:
        session.sign_in(credential)
    except Exception:
        # Connected but not authenticated: without this the server keeps the
        # half-open session until its own idle timeout.
        session.logout()
        raise
    return session


@contextmanager
def _pooled_session(account: Account, credential: Credential) -> Iterator[ImapSession]:
    """This account's IMAP connection, opened and signed in if it has none.

    A handshake plus a LOGIN per operation was most of the latency of a sync
    and nearly all of opening one message. imaplib is not re-entrant, so an
    operation holds the connection for its whole run; one that arrives while
    it is busy opens its own rather than queueing behind a sync.
    """
    with _pool_lock:
        entry = _pool.setdefault(account.id, _Pooled())
        generation = entry.generation

    is_pooled = entry.lock.acquire(blocking=False)
    session = None
    if is_pooled:
        with _pool_lock:
            session, entry.session = entry.session, None
            idle_seconds = time.monotonic() - entry.used_at
        if session is not None and idle_seconds > PROBE_IDLE_SECONDS:
            # Dropped rather than logged out: the LOGOUT would go to the very
            # socket that just failed to answer and block for the timeout again.
            session = session if session.is_alive() else None

    try:
        if session is None:
            session = _signed_in(account, credential)
            logger.debug("opened an IMAP connection for %s", account.email)
        else:
            logger.debug("reusing the IMAP connection for %s", account.email)

        try:
            yield session
        except Exception:
            # An interrupted command can leave a reply half-read on the socket.
            session.logout()
            raise

        is_kept = False
        with _pool_lock:
            if is_pooled and entry.generation == generation:
                entry.session = session
                entry.used_at = time.monotonic()
                is_kept = True
        if not is_kept:
            # A spare opened for a busy account, or one whose account went
            # while it worked. Logged out here, outside the pool lock.
            session.logout()
    finally:
        if is_pooled:
            entry.lock.release()


def close_sessions(account_id: int | None = None) -> None:
    """Drop pooled connections and stop inbox watchers -- one account's, or
    every one.

    Dropped rather than logged out: the caller is usually the main thread,
    where a LOGOUT to a server that stopped answering blocks for the socket
    timeout. The slot itself stays, so an operation already holding this
    account's connection keeps its lock and cannot open a second one beside it.
    """
    with _pool_lock:
        ids = list(_pool) if account_id is None else [account_id]
        watcher_ids = list(_watchers) if account_id is None else [account_id]
        watchers = [_watchers.pop(one) for one in watcher_ids if one in _watchers]
        if account_id is None:
            _graph_states.clear()
            _jmap_states.clear()
        else:
            _graph_states.pop(account_id, None)
            _jmap_states.pop(account_id, None)
        for one in ids:
            _discard_pooled(one)
    for watcher in watchers:
        watcher.stop()


def _discard_pooled(account_id: int) -> None:
    """Forget an account's pooled connection. Caller holds _pool_lock.

    Only the pool: the account's inbox watcher runs on a connection of its own,
    which a failure on the pooled one says nothing about.
    """
    entry = _pool.get(account_id)
    if entry is not None:
        entry.session = None
        entry.generation += 1


def _jmap_session_url(account: Account) -> str:
    """Where this account's JMAP session resource lives."""
    if account.imap_host == FASTMAIL_HOST:
        return FASTMAIL_JMAP_SESSION_URL
    return f"https://{account.imap_host}/.well-known/jmap"


def _jmap_open(
    account: Account, credential: Credential, *, writable: bool = False
) -> JmapSession:
    """A JMAP session for one operation: discovery cached per account, the
    credential bound fresh each time."""
    state = _jmap_state(account.id)
    session = state.session
    if session is None:
        session = state.session = discover(_jmap_session_url(account), credential)
    return JmapSession(session, credential, writable=writable)


# Every window's job generations. Weak so a closed window stops hearing about
# removals once nothing else holds it; main thread only, like the windows.
_lifecycles: weakref.WeakSet[Lifecycle] = weakref.WeakSet()


def register_lifecycle(lifecycle: Lifecycle) -> None:
    """Hear about account removals on this window's generations."""
    _lifecycles.add(lifecycle)


def invalidate_account(account_id: int) -> None:
    """End every in-flight job for a removed or reconfigured account.

    On every window's generations at once: a composer holds its own, and its
    send must die with the account too. A re-added account reuses the rowid,
    so this runs before its rows go, while the tokens still name the old one.
    """
    for lifecycle in list(_lifecycles):
        lifecycle.invalidate_account(account_id)


# --- providers ---------------------------------------------------------------
# The builtin drivers behind the provider registry. Each one is a thin
# adapter: it unpacks the request and calls the existing per-backend function,
# which still owns the pooling, delta links, throttle handling and key
# contexts. The public operations above dispatch through driver_for and carry
# no per-protocol branches of their own.

_IMAP_CAPABILITIES = frozenset(
    {
        Capability.FETCH,
        Capability.FETCH_BODY,
        Capability.SET_FLAGS,
        Capability.MOVE,
        Capability.SEND,
        Capability.SENT_COPY_LOOKUP,
        Capability.IDLE_PUSH,
    }
)

_GRAPH_CAPABILITIES = _IMAP_CAPABILITIES - {Capability.IDLE_PUSH}

_TUTA_CAPABILITIES = frozenset(
    {
        Capability.FETCH,
        Capability.FETCH_BODY,
        Capability.SET_FLAGS,
        Capability.MOVE,
        Capability.SEND,
    }
)

# Everything but push: watch_push stays a part-2 primitive until a watcher
# owns it, so JMAP accounts poll their inbox like Graph ones.
_JMAP_CAPABILITIES = _GRAPH_CAPABILITIES


def _succeeded(uids: Iterable[str]) -> tuple[PerIdOutcome, ...]:
    """Every id reported a success. The backends below apply a flag change
    atomically -- they raise rather than half-apply -- so reaching here means
    each id in the batch went through."""
    return tuple(PerIdOutcome(uid, True) for uid in uids)


class _ImapDriver(ProviderDriver):
    _descriptor = ProviderDescriptor(
        id=PROTOCOL_IMAP,
        display_name="IMAP/SMTP",
        capabilities=_IMAP_CAPABILITIES,
    )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def fetch_mailbox(
        self, account: Account, credential: Credential, request: FetchRequest
    ) -> SyncResult:
        return _fetch_imap_mailbox(
            account,
            credential,
            request.folder,
            request.limit,
            request.offset,
            request.should_count_unread,
        )

    def fetch_full_message(
        self, account: Account, credential: Credential, request: MessageRequest
    ) -> bytes:
        return _fetch_imap_message(
            account, credential, request.folder_name, request.uid, request.uidvalidity
        )

    def set_flags(
        self, account: Account, credential: Credential, request: FlagRequest
    ) -> tuple[PerIdOutcome, ...]:
        if not request.uids:
            return ()
        return _set_imap_flags(
            account,
            credential,
            request.folder_name,
            request.uids,
            request.flag,
            request.should_add,
            request.uidvalidity,
        )

    def move(
        self, account: Account, credential: Credential, request: MoveRequest
    ) -> MoveResult:
        return _move_imap_messages(
            account,
            credential,
            request.folder_name,
            request.uids,
            request.destination,
            request.uidvalidity,
        )

    def send(
        self, account: Account, credential: Credential, request: SendRequest
    ) -> SendOutcome:
        return _send_imap_message(
            account,
            credential,
            request.from_addr,
            list(request.recipients),
            request.raw,
        )

    def sent_copy(
        self, account: Account, credential: Credential, message_id: str
    ) -> tuple[str, int | None] | None:
        return _imap_sent_copy(account, credential, message_id)


class _GraphDriver(ProviderDriver):
    _descriptor = ProviderDescriptor(
        id=PROTOCOL_GRAPH,
        display_name="Microsoft 365",
        capabilities=_GRAPH_CAPABILITIES,
    )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def fetch_mailbox(
        self, account: Account, credential: Credential, request: FetchRequest
    ) -> SyncResult:
        return _fetch_graph_mailbox(
            account,
            credential,
            request.folder,
            request.limit,
            request.offset,
            request.should_count_unread,
        )

    def fetch_full_message(
        self, _account: Account, credential: Credential, request: MessageRequest
    ) -> bytes:
        return graph_messages.fetch_mime(GraphSession(credential), request.uid)

    def set_flags(
        self, _account: Account, credential: Credential, request: FlagRequest
    ) -> tuple[PerIdOutcome, ...]:
        graph_messages.set_flags(
            GraphSession(credential),
            list(request.uids),
            request.flag,
            request.should_add,
        )
        return _succeeded(request.uids)

    def move(
        self, _account: Account, credential: Credential, request: MoveRequest
    ) -> MoveResult:
        return graph_messages.move(
            GraphSession(credential), list(request.uids), request.destination
        )

    def send(
        self, _account: Account, credential: Credential, request: SendRequest
    ) -> SendOutcome:
        # Exchange files the Sent copy itself, so there is nothing to append.
        graph_send.send_mime(
            GraphSession(credential), request.raw, list(request.recipients)
        )
        return SendOutcome(None, None, None)

    def sent_copy(
        self, _account: Account, credential: Credential, message_id: str
    ) -> tuple[str, int | None] | None:
        return _graph_sent_copy(credential, message_id)


class _TutaDriver(ProviderDriver):
    _descriptor = ProviderDescriptor(
        id=PROTOCOL_TUTA,
        display_name="Tuta",
        capabilities=_TUTA_CAPABILITIES,
    )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def fetch_mailbox(
        self, _account: Account, credential: Credential, request: FetchRequest
    ) -> SyncResult:
        return _fetch_tuta_mailbox(
            credential, request.folder, request.limit, request.offset
        )

    def fetch_full_message(
        self, _account: Account, credential: Credential, request: MessageRequest
    ) -> bytes:
        return tuta_messages.fetch_mime(TutaSession(credential), request.uid)

    def set_flags(
        self, _account: Account, credential: Credential, request: FlagRequest
    ) -> tuple[PerIdOutcome, ...]:
        return tuta_messages.set_flags(
            TutaSession(credential),
            list(request.uids),
            request.flag,
            request.should_add,
        )

    def move(
        self, _account: Account, credential: Credential, request: MoveRequest
    ) -> MoveResult:
        return tuta_messages.move(
            TutaSession(credential), list(request.uids), request.destination
        )

    def send(
        self, account: Account, credential: Credential, request: SendRequest
    ) -> SendOutcome:
        """Send as ordinary Tuta mail; the server files the Sent copy itself.

        The composer's explicit delivery choice travels with the queued row:
        the composer only queues a Tuta send once ordinary delivery is
        chosen, so reaching here is the confirmation. Tuta submits
        atomically, so there is no partial refusal to narrow -- a failure is
        either definitive (nothing sent, safe to retry) or `send_uncertain`
        (possibly sent: the caller must terminal-fail it for a human, never
        auto-retry it). Like Graph, the server files Sent itself, so there is
        nothing to append -- but unlike Graph the server assigns its own
        Message-ID, which is why sent_copy below cannot match by ours.
        """
        sent = tuta_messages.send_mime(
            TutaSession(credential),
            request.from_addr,
            account.display_name,
            request.raw,
            list(request.recipients),
            ordinary_confirmed=True,
        )
        if (
            sent.internal_recipients is not None
            and sent.external_recipients is not None
        ):
            logger.info(
                "sent via Tuta (account %s): %d internal, %d external recipient(s)",
                account.email,
                sent.internal_recipients,
                sent.external_recipients,
            )
        return SendOutcome(sent.sent_mail, None, None)

    def sent_copy(
        self, _account: Account, _credential: Credential, _message_id: str
    ) -> tuple[str, int | None] | None:
        # The server assigns its own Message-ID at submit, so our id never
        # matches a server copy: None honestly reports "no copy under this
        # id". The success path still links up, through filed_uid: the next
        # sync merges the Sent copy onto the filed row by its compound server
        # id. The gap is the ambiguous retry, which can resend after a lost
        # reply -- that is why `send_uncertain` must terminal-fail instead of
        # retrying, and why SENT_COPY_LOOKUP stays off this driver.
        return None


class _ProtonDriver(ProviderDriver):
    _descriptor = ProviderDescriptor(
        id=PROTOCOL_PROTON, display_name="Proton", capabilities=frozenset()
    )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def fetch_mailbox(
        self, _account: Account, _credential: Credential, _request: FetchRequest
    ) -> SyncResult:
        raise ProtonError("unsupported_operation")

    def fetch_full_message(
        self, _account: Account, _credential: Credential, _request: MessageRequest
    ) -> bytes:
        raise ProtonError("unsupported_operation")

    def set_flags(
        self, _account: Account, _credential: Credential, _request: FlagRequest
    ) -> tuple[PerIdOutcome, ...]:
        raise ProtonError("unsupported_operation")

    def move(
        self, _account: Account, _credential: Credential, _request: MoveRequest
    ) -> MoveResult:
        raise ProtonError("unsupported_operation")

    def send(
        self, _account: Account, _credential: Credential, _request: SendRequest
    ) -> SendOutcome:
        raise ProtonError("unsupported_operation")

    def sent_copy(
        self, _account: Account, _credential: Credential, _message_id: str
    ) -> tuple[str, int | None] | None:
        return None


class _JmapDriver(ProviderDriver):
    """JMAP accounts, synced natively over the part-2 JMAP backend.

    Like Graph, folders are addressed by opaque id and message ids are
    immutable, so there is no UIDVALIDITY to check; unlike Graph, the sync
    keeps per-folder query states and only walks what changed.
    """

    _descriptor = ProviderDescriptor(
        id=PROTOCOL_JMAP, display_name="JMAP", capabilities=_JMAP_CAPABILITIES
    )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def fetch_mailbox(
        self, account: Account, credential: Credential, request: FetchRequest
    ) -> SyncResult:
        return _fetch_jmap_mailbox(
            account,
            credential,
            request.folder,
            request.limit,
            request.offset,
            request.should_count_unread,
        )

    def fetch_full_message(
        self, account: Account, credential: Credential, request: MessageRequest
    ) -> bytes:
        return _fetch_jmap_message(account, credential, request.uid)

    def set_flags(
        self, account: Account, credential: Credential, request: FlagRequest
    ) -> tuple[PerIdOutcome, ...]:
        if not request.uids:
            return ()
        return _set_jmap_flags(
            account, credential, request.uids, request.flag, request.should_add
        )

    def move(
        self, account: Account, credential: Credential, request: MoveRequest
    ) -> MoveResult:
        return _move_jmap_messages(
            account, credential, request.folder_name, request.uids, request.destination
        )

    def send(
        self, account: Account, credential: Credential, request: SendRequest
    ) -> SendOutcome:
        return _send_jmap_message(
            account,
            credential,
            request.from_addr,
            list(request.recipients),
            request.raw,
        )

    def sent_copy(
        self, account: Account, credential: Credential, message_id: str
    ) -> tuple[str, int | None] | None:
        return _jmap_sent_copy(account, credential, message_id)


def register_builtin_drivers(registry: ProviderRegistry) -> None:
    """Register the drivers this build ships, first so no add-on can take one
    of their ids."""
    for driver in (
        _ImapDriver(),
        _GraphDriver(),
        _TutaDriver(),
        _ProtonDriver(),
        _JmapDriver(),
    ):
        registry.register(driver)


_provider_registry: ProviderRegistry | None = None


def provider_registry() -> ProviderRegistry:
    """The process-wide registry: builtins plus trusted add-ons.

    Built once, so trusting another provider takes a restart to take effect.
    Tests needing isolation build their own ProviderRegistry instead, or reset
    this one through monkeypatch.
    """
    global _provider_registry
    if _provider_registry is None:
        registry = ProviderRegistry()
        register_builtin_drivers(registry)
        registry.discover()
        _provider_registry = registry
    return _provider_registry


def driver_for(protocol: str) -> ProviderDriver:
    """The driver serving this provider id. Raises fail-closed -- unknown,
    disabled or incompatible -- and callers never fall back on seeing one."""
    return provider_registry().driver_for(protocol)


def provider_descriptor(protocol: str) -> ProviderDescriptor | None:
    """This provider's descriptor, or None when no driver serves it."""
    return provider_registry().descriptor_for(protocol)


def provider_supports(protocol: str, capability: Capability) -> bool:
    """Whether this provider can do this -- False for anything unserved."""
    descriptor = provider_descriptor(protocol)
    return descriptor is not None and descriptor.supports(capability)


def inbox_name(folders: list[str]) -> str:
    """The server's inbox mailbox. IMAP calls it INBOX but servers vary the
    casing (Yahoo lists it as "Inbox"), so match by role and fall back to the
    canonical name."""
    return mailbox_with_role(folders, FolderRole.INBOX) or INBOX_MAILBOX


def fetch_mailbox(
    account: Account,
    credential: Credential,
    folder: str | None = None,
    limit: int = RECENT_LIMIT,
    offset: int = 0,
    should_count_unread: bool = True,
) -> SyncResult:
    """Return the folder list + recent headers, over this account's connection.

    `folder` selects which mailbox to pull headers from; None means the inbox.
    `offset` pages backwards: 0 is the newest `limit`, `limit` is the page
    before that (used to load older mail on scroll).
    `should_count_unread` refreshes the badges of the folders this sync isn't
    fetching, which costs a STATUS round trip each -- see _unread_counts.

    Dispatches to this account's provider driver; a provider the registry
    cannot serve raises instead of falling back to another backend.
    """
    request = FetchRequest(
        folder=folder,
        limit=limit,
        offset=offset,
        should_count_unread=should_count_unread,
    )
    return driver_for(account.protocol).fetch_mailbox(account, credential, request)


def _fetch_imap_mailbox(
    account: Account,
    credential: Credential,
    folder: str | None,
    limit: int,
    offset: int,
    should_count_unread: bool,
) -> SyncResult:
    with _pooled_session(account, credential) as session:
        mailboxes = session.list_folders()
        target = folder or inbox_name([m.name for m in mailboxes])
        exists = session.select(target)
        uidvalidity = session.uidvalidity()
        all_uids = session.search_all_uids() if offset == 0 else None
        raw = session.fetch_recent_headers(exists, limit, offset)
        should_count = should_count_unread and offset == 0
        counts = _unread_counts(session, mailboxes, target) if should_count else {}

    messages = [_to_message_header(fetched) for fetched in raw]

    return SyncResult(
        folders=mailboxes,
        messages=messages,
        folder=target,
        exists=exists,
        offset=offset,
        all_uids=all_uids,
        unread_counts=counts,
        uidvalidity=uidvalidity,
    )


def _select(
    session: ImapSession, mailbox: str, uidvalidity: int | None, is_readonly: bool
) -> None:
    """Open a mailbox to act on UIDs that were cached under `uidvalidity`.

    If the server has renumbered it since, those UIDs now name other messages,
    so refuse rather than flag, move or download the wrong ones. None means
    they were cached before any value was recorded, which proves nothing, so
    that is refused too. The next sync records the value and clears the cache.
    Only a server that sends no UIDVALIDITY goes unchecked.
    """
    session.select(mailbox, is_readonly)
    current = session.uidvalidity()
    if current is not None and current != uidvalidity:
        raise UidValidityError(
            f"{mailbox} was renumbered (UIDVALIDITY {uidvalidity} -> {current})"
        )


def _unread_counts(
    session: ImapSession, mailboxes: list[MailboxInfo], target: str
) -> dict[str, int]:
    """Server unread counts for the role folders this sync didn't fetch.

    Only the target mailbox is fetched, so without this every other folder's
    badge stays at whatever the last visit left behind. A folder that won't
    answer is skipped rather than failing the sync -- the counts are a garnish.
    Only ImapError, though: a broken connection has to end the sync rather than
    be reported once per remaining folder and then handed back to the pool.
    """
    counts: dict[str, int] = {}
    for mailbox in mailboxes:
        if mailbox.name == target or ATTR_NOSELECT in mailbox.flags:
            continue
        if role_for_folder(mailbox.name) is FolderRole.OTHER:
            continue
        try:
            counts[mailbox.name] = session.unseen_count(mailbox.name)
        except ImapError:
            logger.warning(
                "could not read the unread count of %s", mailbox.name, exc_info=True
            )
    return counts


def _fetch_tuta_mailbox(
    credential: Credential,
    folder: str | None,
    limit: int,
    offset: int,
) -> SyncResult:
    result = TutaSession(credential).request(
        "sync", folder=folder, limit=limit, offset=offset
    )
    try:
        folders = tuta_messages.mailbox_infos(result["folders"])
        messages = [tuta_messages.message_header(row) for row in result["messages"]]
        folder_id = result["folder"]
        exists = result["exists"]
        reply_offset = result["offset"]
        all_ids = result["all_ids"]
    except (KeyError, TypeError):
        raise TutaError("invalid_response") from None
    if (
        not isinstance(folder_id, str)
        or isinstance(exists, bool)
        or not isinstance(exists, int)
        or isinstance(reply_offset, bool)
        or not isinstance(reply_offset, int)
    ):
        raise TutaError("invalid_response")
    # The id snapshot drives prune_stale_emails: a non-string member would
    # match nothing and wipe the folder's cache, so reject it instead.
    if all_ids is not None and (
        not isinstance(all_ids, list)
        or any(not isinstance(uid, str) for uid in all_ids)
    ):
        raise TutaError("invalid_response")
    return SyncResult(
        folders=folders,
        messages=messages,
        folder=folder_id,
        exists=exists,
        offset=reply_offset,
        all_uids=set(all_ids) if all_ids is not None else None,
    )


def _fetch_graph_mailbox(
    account: Account,
    credential: Credential,
    folder: str | None,
    limit: int,
    offset: int,
    should_count_unread: bool,
) -> SyncResult:
    """fetch_mailbox for a Graph account, where a folder's name is its id."""
    session = GraphSession(credential)
    state = _graph_state(account.id)
    well_known = state.well_known
    if well_known is None or not graph_folders.is_complete(well_known):
        well_known = state.well_known = graph_folders.well_known_ids(session)
    folders = graph_folders.list_folders(session, well_known)

    target = folder or well_known.get("inbox") or ""
    exists = next((f.total for f in folders if f.id == target), 0)
    messages = graph_messages.fetch_headers(session, target, limit, offset)

    all_uids = None
    if offset == 0:
        # Held across the requests: two syncs of one folder racing would each
        # apply the same changes to a stale copy and lose the other's link.
        with state.lock:
            delta = graph_messages.folder_ids(session, target, state.deltas.get(target))
            state.deltas[target] = delta
        all_uids = set(delta.ids)

    # Free here, unlike IMAP: every folder's count came with the listing.
    counts = {
        f.id: f.unread
        for f in folders
        if should_count_unread
        and offset == 0
        and f.id != target
        and f.role is not FolderRole.OTHER
    }
    return SyncResult(
        folders=[
            MailboxInfo(f.id, "/", "", label=f.label, role=f.role, parent=f.parent_id)
            for f in folders
        ],
        messages=messages,
        folder=target,
        exists=exists,
        offset=offset,
        all_uids=all_uids,
        unread_counts=counts,
    )


def _fetch_jmap_mailbox(
    account: Account,
    credential: Credential,
    folder: str | None,
    limit: int,
    offset: int,
    should_count_unread: bool,
) -> SyncResult:
    """fetch_mailbox for a JMAP account, where a folder's name is its id."""
    jmap = _jmap_open(account, credential)
    state = _jmap_state(account.id)
    # Held across the requests: two syncs of one folder racing would each
    # apply the same changes to a stale copy and lose the other's cursor.
    with state.lock:
        mailboxes = _jmap_mailboxes(jmap, state)
        target = folder or next(
            (box.id for box in mailboxes if box.role is FolderRole.INBOX), ""
        )
        exists = next((box.total for box in mailboxes if box.id == target), 0)
        if offset == 0:
            messages, all_uids = _jmap_newest(jmap, state, target, limit)
        else:
            page = jmap_messages.query_page(jmap, target, offset, limit)
            found, _missing = jmap_messages.fetch_emails(jmap, list(page.ids))
            messages = [jmap_messages.message_header(item) for item in found]
            all_uids = None

    # Free here, unlike IMAP: every mailbox's count came with the listing.
    counts = {
        box.id: box.unread
        for box in mailboxes
        if should_count_unread
        and offset == 0
        and box.id != target
        and box.role is not FolderRole.OTHER
    }
    return SyncResult(
        folders=[
            MailboxInfo(
                box.id, "", "", label=box.name, role=box.role, parent=box.parent_id
            )
            for box in mailboxes
        ],
        messages=messages,
        folder=target,
        exists=exists,
        offset=offset,
        all_uids=all_uids,
        unread_counts=counts,
    )


def _apply_mailbox_delta(
    merged: dict[str, jmap_folders.JmapMailbox], delta: jmap_folders.MailboxDelta
) -> None:
    for gone in delta.destroyed_ids:
        merged.pop(gone, None)
    for box in delta.changed:
        merged[box.id] = box


def _jmap_mailboxes(
    jmap: JmapSession, state: _JmapState
) -> list[jmap_folders.JmapMailbox]:
    """Every mailbox, refreshed through the stored Mailbox state when there is
    one. Caller holds the state lock; the cursor moves only on success."""
    if state.mailbox_state is not None:
        try:
            delta = jmap_folders.mailbox_changes(jmap, state.mailbox_state)
        except JmapError as error:
            if not needs_full_sync(error):
                raise
        else:
            _apply_mailbox_delta(state.mailboxes, delta)
            state.mailbox_state = delta.new_state
            return list(state.mailboxes.values())
    # State first, then the listing, then one delta to catch the window
    # between them: a mailbox created after the list but before the cursor
    # would otherwise miss both and stay invisible until the state expires.
    since = jmap_folders.current_mailbox_state(jmap)
    merged = {box.id: box for box in jmap_folders.list_mailboxes(jmap)}
    try:
        delta = jmap_folders.mailbox_changes(jmap, since)
    except JmapError as error:
        if not needs_full_sync(error):
            raise
        state.mailboxes = merged
        state.mailbox_state = jmap_folders.current_mailbox_state(jmap)
        return list(state.mailboxes.values())
    _apply_mailbox_delta(merged, delta)
    state.mailboxes = merged
    state.mailbox_state = delta.new_state
    return list(state.mailboxes.values())


def _jmap_newest(
    jmap: JmapSession, state: _JmapState, target: str, limit: int
) -> tuple[list[MessageHeader], set[str]]:
    """The newest page plus whatever entered since, and the authoritative id
    set behind it. Caller holds the state lock; the cursor moves only after
    every fetch below lands."""
    cursor = state.queries.get(target)
    if cursor is not None:
        try:
            changed = jmap_messages.query_changes(jmap, target, cursor.state)
        except JmapError as error:
            if not needs_full_sync(error):
                raise
        else:
            # A delta only names what entered or left, so the newest page is
            # re-read beside it: that is what picks up a flag flipped on
            # another client, the way the IMAP and Graph syncs do.
            page = jmap_messages.query_page(jmap, target, 0, limit)
            wanted = list(dict.fromkeys([*page.ids, *changed.ids]))
            found, missing = jmap_messages.fetch_emails(jmap, wanted)
            kept = (set(cursor.ids) - set(changed.removed_ids) - set(missing)) | set(
                changed.ids
            )
            state.queries[target] = _JmapQuery(changed.query_state, frozenset(kept))
            return [jmap_messages.message_header(item) for item in found], kept
    walked = jmap_messages.query_all_ids(jmap, target)
    found, _missing = jmap_messages.fetch_emails(jmap, walked.ids[:limit])
    full = set(walked.ids)
    query_state = walked.query_state
    # A long walk spans states: one delta from where it ended catches mail
    # that arrived mid-walk, which the pages may never have covered.
    try:
        changed = jmap_messages.query_changes(jmap, target, walked.query_state)
    except JmapError as error:
        if not needs_full_sync(error):
            raise
    else:
        full = (full - set(changed.removed_ids)) | set(changed.ids)
        query_state = changed.query_state
    state.queries[target] = _JmapQuery(query_state, frozenset(full))
    return [jmap_messages.message_header(item) for item in found], full


def _to_message_header(fetched: FetchedHeader) -> MessageHeader:
    """Turn raw wire headers into the display-ready form.

    This is where the two shapes differ: the sender becomes a display name,
    the date is normalized to a timestamp, and the server's \\Seen flag is
    inverted into `is_unread`, which is how the rest of the app thinks about it.
    """
    recipient_name, recipient_address = first_recipient(fetched.to_header)
    return MessageHeader(
        uid=fetched.uid,
        sender=_clean_sender(fetched.from_header),
        sender_address=_sender_address(fetched.from_header),
        recipient=recipient_name,
        recipient_address=recipient_address,
        subject=fetched.subject or NO_SUBJECT,
        date=_iso_date(fetched.date),
        is_unread=not fetched.seen,
        is_starred=fetched.flagged,
        message_id=fetched.message_id,
        in_reply_to=fetched.in_reply_to,
        references=fetched.references,
        addresses=getaddresses(
            [fetched.from_header, fetched.to_header, fetched.cc_header]
        ),
    )


def fetch_full_message(
    account: Account,
    credential: Credential,
    folder_name: str,
    uid: str,
    *,
    uidvalidity: int | None,
) -> bytes:
    """Open one folder and download a single full message."""
    request = MessageRequest(folder_name=folder_name, uid=uid, uidvalidity=uidvalidity)
    return driver_for(account.protocol).fetch_full_message(account, credential, request)


def _fetch_imap_message(
    account: Account,
    credential: Credential,
    folder_name: str,
    uid: str,
    uidvalidity: int | None,
) -> bytes:
    with _pooled_session(account, credential) as session:
        _select(session, folder_name, uidvalidity, is_readonly=True)
        return session.fetch_message(uid)


def _fetch_jmap_message(account: Account, credential: Credential, uid: str) -> bytes:
    """One message's bytes. JMAP ids are account-wide and immutable, so there
    is no folder to open and no UIDVALIDITY to check."""
    return jmap_messages.fetch_mime(_jmap_open(account, credential), uid)


def server_uids(mails: Iterable[Email]) -> list[str]:
    """The IMAP UIDs of these messages, skipping any without one.

    A locally saved copy (a Sent message, before the next sync confirms it)
    has no UID, and imaplib silently drops a None argument -- which would send
    a UID-less "UID STORE +FLAGS (...)" and get a BAD back. There is nothing
    on the server to act on yet, so leave those out.
    """
    return [mail.server_id for mail in mails if mail.server_id]


def set_flag(
    account: Account,
    credential: Credential,
    folder_name: str,
    uids: Sequence[str],
    flag: str,
    should_add: bool,
    *,
    uidvalidity: int | None,
) -> tuple[PerIdOutcome, ...]:
    """Add or remove an IMAP flag on every message in a conversation.

    One STORE for the whole UID set rather than one per message, which made
    reading a long thread a round trip per message in it. An empty set is not
    an empty command but a malformed one, and a conversation can be entirely
    locally saved copies that have no UID yet -- see server_uids.

    Returns each id's outcome; a provider the registry cannot serve raises.
    """
    if not uids:
        return ()
    request = FlagRequest(
        folder_name=folder_name,
        uids=tuple(uids),
        flag=flag,
        should_add=should_add,
        uidvalidity=uidvalidity,
    )
    return driver_for(account.protocol).set_flags(account, credential, request)


def _set_imap_flags(
    account: Account,
    credential: Credential,
    folder_name: str,
    uids: Sequence[str],
    flag: str,
    should_add: bool,
    uidvalidity: int | None,
) -> tuple[PerIdOutcome, ...]:
    with _pooled_session(account, credential) as session:
        _select(session, folder_name, uidvalidity, is_readonly=False)
        session.store_flags(",".join(uids), flag, should_add)
    return tuple(PerIdOutcome(uid, True) for uid in uids)


def _set_jmap_flags(
    account: Account,
    credential: Credential,
    uids: Sequence[str],
    flag: str,
    should_add: bool,
) -> tuple[PerIdOutcome, ...]:
    """Add or remove one flag on every id. A refusal raises rather than
    reporting a quiet failure the caller would never show."""
    result = jmap_messages.set_flag(
        _jmap_open(account, credential, writable=True), list(uids), flag, should_add
    )
    for uid in uids:
        if uid in result.errors:
            refusal = result.errors[uid]
            raise JmapError(
                0,
                refusal.code,
                f"could not set {flag} on {uid}: {refusal.message}",
            )
    return _succeeded(uids)


def move_messages(
    account: Account,
    credential: Credential,
    folder_name: str,
    uids: list[str],
    destination: str,
    *,
    uidvalidity: int | None,
) -> MoveResult:
    """Move every message in a conversation to another mailbox."""
    request = MoveRequest(
        folder_name=folder_name,
        uids=tuple(uids),
        destination=destination,
        uidvalidity=uidvalidity,
    )
    return driver_for(account.protocol).move(account, credential, request)


def _move_imap_messages(
    account: Account,
    credential: Credential,
    folder_name: str,
    uids: Sequence[str],
    destination: str,
    uidvalidity: int | None,
) -> MoveResult:
    with _pooled_session(account, credential) as session:
        _select(session, folder_name, uidvalidity, is_readonly=False)
        destination_uids = []
        for index, uid in enumerate(uids):
            try:
                destination_uids.append(session.move(uid, destination))
            except Exception as error:
                # Swallowing it here hides the failure from the pool, which
                # would then hand on a connection that may have a half-read
                # reply on the socket. Drop it explicitly instead -- and only
                # it, so the inbox watcher keeps pushing new mail.
                with _pool_lock:
                    _discard_pooled(account.id)
                return MoveResult(destination_uids, index, error)
    return MoveResult(destination_uids)


def _move_jmap_messages(
    account: Account,
    credential: Credential,
    folder_name: str,
    uids: Sequence[str],
    destination: str,
) -> MoveResult:
    """Move every id to another mailbox. A move keeps the id -- only the
    mailbox membership changes -- and the result stops at the first id the
    server refused, which is what the window restores from."""
    result = jmap_messages.move_emails(
        _jmap_open(account, credential, writable=True),
        list(uids),
        [destination],
        [folder_name],
    )
    applied = set(result.updated)
    moved = MoveResult()
    for index, uid in enumerate(uids):
        refusal = result.errors.get(uid)
        if refusal is not None:
            moved.failed_index = index
            moved.error = JmapError(
                0, refusal.code, f"could not move {uid}: {refusal.message}"
            )
            break
        if uid not in applied:
            moved.failed_index = index
            moved.error = JmapError(0, CODE_HTTP, f"Email/set answered without {uid}")
            break
        moved.destination_uids.append(uid)
    return moved


def send_message(
    account: Account,
    credential: Credential,
    from_addr: str,
    recipients: list[str],
    raw: bytes,
) -> SendOutcome:
    """Connect, log in, and hand a fully-built message to the server.

    Raises when the mail did not go out. A Sent filing that fails after a
    successful send comes back as file_error instead of raising: raising there
    would leave the mail in the Outbox to be sent a second time. A refusal
    for some recipients raises SmtpRefusedError naming who was refused and
    who already got it -- and files nothing to Sent, since a Sent copy would
    make the next attempt's redelivery check read the mail as fully sent and
    drop the refused recipients.

    Dispatches to this account's provider driver; a provider the registry
    cannot serve raises instead of falling back to another backend.
    """
    request = SendRequest(from_addr=from_addr, recipients=tuple(recipients), raw=raw)
    return driver_for(account.protocol).send(account, credential, request)


def _send_imap_message(
    account: Account,
    credential: Credential,
    from_addr: str,
    recipients: list[str],
    raw: bytes,
) -> SendOutcome:
    session = SmtpSession(account.smtp_host, account.smtp_port, account.smtp_security)
    session.connect()

    try:
        session.sign_in(credential)
        session.send_raw(from_addr, recipients, raw)
    finally:
        session.quit()

    # SMTP only hands the message to the recipient's server; nothing puts a copy
    # in our own Sent mailbox, so without this the mail is missing from every
    # other client. Never fatal: it has already gone out, and failing here would
    # leave it in the Outbox to be sent a second time.
    try:
        filed = _append_to_sent(account, credential, raw)
    except Exception as error:
        logger.warning(
            "could not save a copy of the sent message to Sent on %s (account %s)",
            account.imap_host,
            account.email,
            exc_info=True,
        )
        return SendOutcome(None, None, error)
    if filed is None:
        return SendOutcome(None, None, None)
    uidvalidity, uid = filed
    return SendOutcome(uid, uidvalidity, None)


def _send_jmap_message(
    account: Account,
    credential: Credential,
    from_addr: str,
    recipients: list[str],
    raw: bytes,
) -> SendOutcome:
    """Upload the finished message, save it as a draft, and submit it. The
    server files the Sent copy itself, so there is nothing to append.

    An uncertain submission is settled by looking at where the draft ended up
    -- never by submitting again, which could send it twice. When that cannot
    prove the mail went out, the attempt fails and the Outbox's own Sent check
    guards the retry.
    """
    jmap = _jmap_open(account, credential, writable=True)
    if not jmap.submission_supported:
        raise JmapError(0, CODE_NO_SUBMISSION, "the server offers no EmailSubmission")
    if not jmap.is_writable:
        raise JmapError(0, CODE_READ_ONLY, "this account is read-only")
    boxes = jmap_folders.list_mailboxes(jmap)
    drafts = next((box.id for box in boxes if box.role is FolderRole.DRAFTS), None)
    if drafts is None:
        raise JmapError(0, CODE_HTTP, "the account has no drafts mailbox")
    sent = next((box.id for box in boxes if box.role is FolderRole.SENT), None)
    blob = jmap.upload_blob(raw, "message/rfc822")
    created = jmap_messages.create_emails(
        jmap, {"postcard": {"blobId": blob, "mailboxIds": {drafts: True}}}
    )
    refusal = created.errors.get("postcard")
    if refusal is not None:
        raise JmapError(0, refusal.code, f"could not save the draft: {refusal.message}")
    echo = created.created.get("postcard", {})
    email_id = echo.get("id")
    if not isinstance(email_id, str) or not email_id:
        raise JmapError(0, CODE_HTTP, "Email/set answered without a draft id")
    submitted = jmap_messages.submit_email(
        jmap,
        jmap_messages.SubmitRequest(
            email_id,
            envelope={
                "mailFrom": {"email": from_addr},
                "rcptTo": [
                    {"email": address} for address in _envelope_addresses(recipients)
                ],
            },
        ),
    )
    if submitted.error is not None:
        raise JmapError(
            0,
            submitted.error.code,
            f"the server refused the message: {submitted.error.message}",
        )
    if submitted.is_uncertain:
        settled = sent is not None and jmap_messages.resolve_uncertain_submission(
            jmap, email_id, sent
        )
        if settled is True:
            return SendOutcome(None, None, None)
        raise JmapError(
            0,
            CODE_UNCERTAIN_SUBMISSION,
            "could not confirm the message was sent",
        )
    return SendOutcome(None, None, None)


def _envelope_addresses(recipients: list[str]) -> list[str]:
    """Bare addresses for the submission envelope: the composer hands over
    display names with them, while rcptTo wants addresses alone."""
    return [address for _name, address in getaddresses(recipients) if address]


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses a redirect that would leave https.

    The token in an unsubscribe URL is the only thing authenticating the
    request, and a list is free to redirect us at any host it likes -- so a
    plain-http hop would put that token on the wire in the clear.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https:"):
            raise urllib.error.HTTPError(newurl, code, msg, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Names the app rather than mimicking a browser: bot filters reject the default
# urllib agent, not an honest one. No version -- it would have to be threaded
# down from main() for nothing, since no list branches on it.
UNSUBSCRIBE_USER_AGENT = "Postcard (+https://postcard.gxanshu.in)"
UNSUBSCRIBE_TIMEOUT_SECONDS = 15

_unsubscribe_opener = urllib.request.build_opener(_HttpsOnlyRedirect())


def post_unsubscribe(url: str) -> None:
    """Send an RFC 8058 one-click unsubscribe. Raises unless the list accepts it.

    open() raises HTTPError for any 4xx/5xx, so a return means the list took the
    request. Nothing authenticates this beyond the token already in the URL: no
    cookies and no credentials -- but a User-Agent is not optional. Cloudflare
    403s urllib's default "Python-urllib/x.y" at the edge, so a plain request
    never reaches the list at all (Substack and mailersite.com both do this).
    """
    request = urllib.request.Request(
        url,
        data=ONE_CLICK_BODY,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UNSUBSCRIBE_USER_AGENT,
        },
    )
    _unsubscribe_opener.open(request, timeout=UNSUBSCRIBE_TIMEOUT_SECONDS).close()


def _append_to_sent(
    account: Account, credential: Credential, raw: bytes
) -> tuple[int, str] | None:
    """File a sent message in the server's Sent mailbox. Returns the
    (UIDVALIDITY, UID) UIDPLUS assigned it, or None when there is nothing to
    link back to: Gmail files its own copy, some servers have no Sent mailbox,
    and some report no APPENDUID."""
    with _pooled_session(account, credential) as session:
        if session.has_capability(GMAIL_CAPABILITY):
            return None
        sent = mailbox_with_role(
            (mailbox.name for mailbox in session.list_folders()), FolderRole.SENT
        )
        if sent is None:
            logger.warning(
                "no Sent mailbox on %s (account %s)", account.imap_host, account.email
            )
            return None
        return session.append(sent, raw)


# --- queued sends ----------------------------------------------------------
# The Outbox as a state machine, worked from the main thread: claim a row
# before its worker starts, complete or fail it when the worker reports back.
# A crash between SMTP acceptance and completion leaves a sending row whose
# mail may or may not have left, so every attempt looks for its own copy in
# Sent before touching SMTP -- the only proof either way.

# How many attempts a queued mail gets before it rests as terminal failed,
# staying visible in the Outbox until the user deletes it.
MAX_SEND_ATTEMPTS = 5

# Email ids a send in this process claimed and has not finished. Recovery
# skips these: a row a live worker is sending looks exactly like a crashed
# one, and recovering it would send it twice. Main thread only.
_live_claims: set[int] = set()


def claim_queued_send(db: Database, email_id: int) -> QueuedSend | None:
    """Claim a queued mail for a send starting now. None when another send
    already holds it, or it reached terminal failed."""
    claimed = db.claim_outbox_email(email_id)
    if claimed is not None:
        _live_claims.add(email_id)
    return claimed


def release_queued_send(email_id: int) -> None:
    """Let go of a claim without completing it, for a send whose result was
    dropped. Its row stays to be recovered and retried by the next drain."""
    _live_claims.discard(email_id)


def recover_queued_sends(db: Database) -> int:
    """Ready every send a crash interrupted, except claims a live send holds."""
    return db.recover_interrupted_sends(set(_live_claims))


def fail_queued_send(db: Database, email_id: int, error: str) -> OutboxState:
    """Record a failed attempt, terminal past the attempt cap. Returns the
    state the row landed on. error is diagnostics for the next drain to show,
    so it is classified before it ever reaches the user."""
    send = db.queued_send(email_id)
    if send is None:
        return OutboxState.FAILED
    state = (
        OutboxState.FAILED if send.attempts >= MAX_SEND_ATTEMPTS else OutboxState.RETRY
    )
    db.fail_outbox_email(email_id, error, state)
    return state


def fail_refused_send(
    db: Database, email_id: int, refused: tuple[str, ...], error: str
) -> OutboxState:
    """Record a refused-recipient attempt, terminal past the attempt cap.

    Returns the state the row landed on. Only the refused recipients stay in
    the envelope: the retry sends to them alone, never again to accepted ones.
    error is diagnostics for the next drain to show, so it is classified
    before it ever reaches the user.
    """
    send = db.queued_send(email_id)
    if send is None:
        return OutboxState.FAILED
    state = (
        OutboxState.FAILED if send.attempts >= MAX_SEND_ATTEMPTS else OutboxState.RETRY
    )
    rotated = None
    if send.message_id:
        # A refused-only retry is a new send under a fresh id: the attempt's
        # id may already sit in Sent, auto-filed by the server that took it
        # for the accepted recipients, and matching it would drop the refused
        # without sending. Mail with no id keeps none.
        rotated = rotate_message_id(send.raw)
    db.fail_refused_outbox_email(email_id, list(refused), error, state, rotated)
    return state


def send_queued(
    account: Account, credential: Credential, job: QueuedSend
) -> SendOutcome:
    """Send one claimed Outbox mail, skipping SMTP when it already went out.

    Runs on the worker thread: network only, like every other send. Every
    attempt looks for its own copy in Sent first -- a retry whose last try
    sent before dying must not re-send, and only a delivery already on record
    proves the mail reached the outside. The check costs one Sent lookup per
    send, and when Sent itself is unreachable the attempt fails rather than
    sending mail the next retry could not recognize. A partial refusal
    propagates as SmtpRefusedError: the caller narrows the envelope to the
    refused recipients with fail_refused_send, so the retry sends to them
    only.
    """
    if job.message_id:
        found = _sent_copy(account, credential, job.message_id)
        if found is not None:
            uid, uidvalidity = found
            logger.info(
                "not resending %r via %s (account %s): already in Sent",
                job.subject,
                account.smtp_host,
                account.email,
            )
            return SendOutcome(uid, uidvalidity, None)
    recipients = (
        job.envelope if job.envelope is not None else extract_recipients(job.raw)
    )
    return send_message(account, credential, account.email, recipients, job.raw)


def _sent_copy(
    account: Account, credential: Credential, message_id: str
) -> tuple[str, int | None] | None:
    """The (UID, UIDVALIDITY) of this account's Sent copy of message_id, if any.

    None means the copy is absent -- or the account files no copy to look for.
    A check that fails raises instead of reading as absent: sending on after a
    failed check could send a second copy of mail that already went out, while
    failing the attempt only retries it. Graph ids are immutable, so those
    carry no validity.
    """
    return driver_for(account.protocol).sent_copy(account, credential, message_id)


def _imap_sent_copy(
    account: Account, credential: Credential, message_id: str
) -> tuple[str, int | None] | None:
    with _pooled_session(account, credential) as session:
        sent = mailbox_with_role(
            (mailbox.name for mailbox in session.list_folders()),
            FolderRole.SENT,
        )
        if sent is None:
            return None
        session.select(sent)
        uids = session.search_message_id(message_id)
        if not uids:
            return None
        return min(uids, key=int), session.uidvalidity()


def _graph_sent_copy(
    credential: Credential, message_id: str
) -> tuple[str, None] | None:
    escaped = message_id.replace("'", "''")
    filtr = quote(f"internetMessageId eq '{escaped}'", safe="")
    page = GraphSession(credential).get(
        f"/me/messages?$filter={filtr}&$select=id&$top=1"
    )
    values = page.get("value", [])
    if not values:
        return None
    return values[0]["id"], None


def _jmap_sent_copy(
    account: Account, credential: Credential, message_id: str
) -> tuple[str, None] | None:
    found = jmap_messages.find_by_message_id(
        _jmap_open(account, credential), message_id
    )
    return (found, None) if found is not None else None


def file_sent_email(
    db: Database,
    account: Account,
    job: QueuedSend,
    outcome: SendOutcome,
    recipient: tuple[str, str],
) -> tuple[Folder, bool]:
    """File the local Sent copy of a mail known sent, then forget the queue.

    server_id links the row to the server copy UIDPLUS reported; without one
    the next sync still merges it by Message-ID. Returns the Sent folder and
    whether its UIDVALIDITY changed -- and only then must the caller end the
    jobs on its old UIDs, which single-flights the generation bump across
    however many completions land together.
    """
    sent = sent_folder(db, account.id)
    validity_changed = outcome.uidvalidity is not None and db.set_folder_uidvalidity(
        sent.id, outcome.uidvalidity
    )
    if job.message_id:
        db.delete_emails_by_message_id(sent.id, job.message_id)
    name, address = recipient
    if not address and job.envelope:
        # A Bcc-only mail names nobody in To, so the caller has no recipient
        # to show -- the first envelope address stands in, naming Bcc nowhere
        # but the sender's own copy.
        _shown, address = next(iter(getaddresses(job.envelope)), ("", ""))
        name = name or address
    row = db.save_email(
        sent.id,
        sender=account.email,
        sender_address=account.email,
        recipient=name,
        recipient_address=address,
        subject=job.subject,
        preview=job.subject,
        date=datetime.now().astimezone().isoformat(),
        is_unread=False,
        server_id=outcome.filed_uid,
    )
    db.save_raw_message(row.id, job.raw)
    db.complete_outbox_email(job.email_id)
    if row.id != job.email_id:
        # A re-filing lands on the deleted Outbox row's id when the table is
        # otherwise empty; deleting it then would take the copy just saved.
        db.delete_email(job.email_id)
    return sent, validity_changed


# Cached because the sidebar classifies every folder of every account several
# times per reload (badges, the unified inbox, the tray count), and a folder
# name is a stable key.
@lru_cache(maxsize=512)
def role_for_folder(name: str) -> FolderRole:
    """Classify a mailbox by name.

    Matched by regex and tolerant of casing, because servers name these
    differently ("Sent Items", "[Gmail]/Sent Mail", "Deleted Items").
    """
    for pattern, role in _ROLE_PATTERNS:
        if pattern.search(name):
            return role
    return FolderRole.OTHER


def folder_role(folder: Folder) -> FolderRole:
    """A folder's role: the one its server stated, else inferred from its name."""
    try:
        return FolderRole(folder.role)
    except ValueError:
        return role_for_folder(folder.name)


def folder_label(folder: Folder) -> str:
    """What the sidebar and menus call a folder."""
    return folder.label or display_name_for_folder(
        folder.name, folder.display_delimiter
    )


def is_outgoing(folder: Folder) -> bool:
    """Whether a folder holds mail this account sent.

    The sender of everything in one is the account itself, so the list shows
    the recipient instead. Outbox is local-only and role_for_folder, which
    classifies what the server offers, doesn't know it.
    """
    return folder.name == OUTBOX_FOLDER or folder_role(folder) in (
        FolderRole.SENT,
        FolderRole.DRAFTS,
    )


def first_recipient(to_header: str) -> tuple[str, str]:
    """A To header as (display name, address), both empty if it names nobody.

    parseaddr, which the sender helpers use, returns empty strings for a header
    listing more than one address -- and a sent message usually does.
    """
    name, address = next(iter(getaddresses([to_header])), ("", ""))
    address = address.strip().lower()
    return name or address, address


def mailbox_with_role(names: Iterable[str], role: FolderRole) -> str | None:
    """The mailbox this server uses for a role, or None when it lists none.

    Every provider spells these its own way -- sent mail lives in "Sent",
    "Sent Items", "Sent Messages", "[Gmail]/Sent Mail" or "INBOX.Sent" -- so
    the name has to come from the server's own list, never from a constant.
    """
    return next((name for name in names if role_for_folder(name) is role), None)


def sent_folder(db: Database, account_id: int) -> Folder:
    """The local folder that mirrors this account's sent mailbox on the server.

    Reads and writes the database, so main thread only. Saving the copy under a
    folder literally named "Sent" files it beside the server's own sent mailbox
    instead of in it, and the next sync's prune_folders then drops that folder
    and the copy with it -- the mail vanishes from the app. Falls back to
    creating "Sent" for an account whose folder list hasn't synced yet.
    """
    return _folder_for_role(db, account_id, FolderRole.SENT, SENT_FOLDER)


def drafts_folder(db: Database, account_id: int) -> Folder:
    """The local folder that mirrors this account's drafts mailbox.

    Same reasoning as sent_folder: a draft saved under a folder named "Drafts"
    beside the server's "Entwürfe" is pruned, with the draft, by the next sync.
    Main thread only.
    """
    return _folder_for_role(db, account_id, FolderRole.DRAFTS, DRAFTS_FOLDER)


def _folder_for_role(
    db: Database, account_id: int, role: FolderRole, fallback_name: str
) -> Folder:
    folders = db.folders_for_account(account_id)
    match = next((folder for folder in folders if folder_role(folder) is role), None)
    if match is not None:
        return match
    return db.get_or_create_folder(
        account_id, fallback_name, icon_for_folder(fallback_name)
    )


def parent_mailbox_name(name: str, delimiter: str) -> str:
    """The mailbox enclosing name, or "" when it sits at the top level."""
    parent = name.rpartition(delimiter)[0] if delimiter else ""
    return "" if parent in NAMESPACE_ROOTS else parent


def parent_of(mailbox: MailboxInfo) -> str:
    """The name of the mailbox enclosing this one, "" at the top level."""
    if mailbox.parent is not None:
        return mailbox.parent
    return parent_mailbox_name(mailbox.name, mailbox.delimiter)


def creation_order(mailboxes: list[MailboxInfo]) -> list[MailboxInfo]:
    """The order to first store mailboxes in, which is their sidebar order.

    IMAP lists in whatever order the server likes, so shortest name first
    keeps a parent above its children. Graph mailboxes arrive already ordered,
    role folders first.
    """
    if any(mailbox.parent is not None for mailbox in mailboxes):
        return mailboxes
    return sorted(mailboxes, key=lambda mailbox: len(mailbox.name))


def icon_for_mailbox(mailbox: MailboxInfo) -> str:
    if ATTR_NOSELECT in mailbox.flags:
        return "folder-symbolic"
    if mailbox.role:
        return icon_for_role(FolderRole(mailbox.role))
    return icon_for_folder(mailbox.name)


def display_name_for_folder(name: str, delimiter: str | None = None) -> str:
    name = decode_mailbox_name(name)
    for root in NAMESPACE_ROOTS:
        if name.startswith(root + "/"):
            name = name[len(root) + 1 :]
            break
    if delimiter:
        name = name.rsplit(delimiter, 1)[-1]
    return name


def icon_for_folder(name: str) -> str:
    """Pick a symbolic icon name for a mailbox (used in the sidebar)."""
    return icon_for_role(role_for_folder(name))


def icon_for_role(role: FolderRole) -> str:
    return {
        FolderRole.INBOX: "mail-inbox-symbolic",
        FolderRole.SENT: "mail-send-symbolic",
        FolderRole.DRAFTS: "document-edit-symbolic",
        FolderRole.ARCHIVE: "mail-archive-symbolic",
        FolderRole.TRASH: "user-trash-symbolic",
        FolderRole.JUNK: "mail-mark-junk-symbolic",
        FolderRole.STARRED: "starred-symbolic",
    }.get(role, "folder-symbolic")


def _clean_sender(value: str) -> str:
    # "Ada Lovelace <ada@example.com>" -> "Ada Lovelace"; a bare address stays.
    name, addr = parseaddr(value)
    return name or addr or value


def _sender_address(value: str) -> str:
    # "Ada Lovelace <ada@example.com>" -> "ada@example.com", "" if unparseable.
    return parseaddr(value)[1].strip().lower()


def _iso_date(value: str) -> str:
    # "Wed, 16 Jul 2026 10:00:00 +0000" -> "2026-07-16T10:00:00+00:00". Stored
    # as a timestamp rather than a label because format_date is relative to
    # today, and the column is written once at sync time and never revisited.
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def format_date(value: str, is_12_hour_clock: bool = False) -> str:
    """Render a stored timestamp as the short label the list and reader show.

    "Today 10:00" for today, "Yesterday", a weekday name within the last week,
    and "Jul 16" beyond it. Anything unparseable is passed through untouched,
    which covers both a message whose Date header we couldn't read and the
    pre-formatted dates rows carried before this column held a timestamp.
    """
    try:
        moment = datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return value

    days = (date.today() - moment.date()).days
    if days == 0:
        time = moment.strftime(clock_format(is_12_hour_clock)).strip()
        return _("Today {time}").format(time=time)
    if days == 1:
        return _("Yesterday")
    if 1 < days < 7:
        return moment.strftime("%a")
    return moment.strftime("%b %d")
