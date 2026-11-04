"""The registry serves every provider id and fails closed on the rest."""

import dataclasses

import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import (
    PROTOCOL_GRAPH,
    PROTOCOL_IMAP,
    PROTOCOL_JMAP,
    PROTOCOL_PROTON,
    PROTOCOL_TUTA,
    Account,
)
from postcard.core.models.mailbox import MailboxInfo
from postcard.core.net import MoveResult
from postcard.core.net.auth import Credential
from postcard.core.net.imap_session import FLAG_SEEN
from postcard.core.providers import (
    Capability,
    DuplicateProviderError,
    FetchRequest,
    PerIdOutcome,
    ProviderDescriptor,
    ProviderDriver,
    ProviderError,
    ProviderRegistry,
    SendOutcome,
    SyncCompleteness,
    SyncResult,
    UnknownProviderError,
)

CREDENTIAL = Credential("ada@example.com", "secret")


def account(protocol: str = PROTOCOL_IMAP, id: int = 71) -> Account:
    return Account(
        id=id,
        email="ada@example.com",
        display_name="Ada",
        protocol=protocol,
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=465,
    )


class RecordingDriver(ProviderDriver):
    """A driver double that records its requests and plays back canned results."""

    def __init__(
        self,
        id: str,
        capabilities: frozenset[Capability] = frozenset(Capability),
        fetch_result: SyncResult | None = None,
        flag_outcomes: tuple[PerIdOutcome, ...] = (),
    ) -> None:
        self._descriptor = ProviderDescriptor(
            id=id, display_name=id.title(), capabilities=capabilities
        )
        self._fetch_result = fetch_result if fetch_result is not None else SyncResult()
        self._flag_outcomes = flag_outcomes
        self.requests: list[object] = []

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def fetch_mailbox(self, account, credential, request):
        self.requests.append(request)
        return self._fetch_result

    def fetch_full_message(self, account, credential, request):
        self.requests.append(request)
        return b"message"

    def set_flags(self, account, credential, request):
        self.requests.append(request)
        return self._flag_outcomes

    def move(self, account, credential, request):
        self.requests.append(request)
        return MoveResult()

    def send(self, account, credential, request):
        self.requests.append(request)
        return SendOutcome(None, None, None)

    def sent_copy(self, account, credential, message_id):
        self.requests.append(message_id)
        return


def use_registry(monkeypatch, registry: ProviderRegistry) -> None:
    monkeypatch.setattr(mail_sync, "_provider_registry", registry)


def builtin_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    mail_sync.register_builtin_drivers(registry)
    return registry


def test_every_builtin_id_resolves_to_its_own_driver():
    registry = builtin_registry()

    for protocol in (
        PROTOCOL_IMAP,
        PROTOCOL_GRAPH,
        PROTOCOL_TUTA,
        PROTOCOL_PROTON,
        PROTOCOL_JMAP,
    ):
        assert registry.driver_for(protocol).descriptor.id == protocol


def test_an_unknown_provider_fails_closed():
    with pytest.raises(UnknownProviderError):
        builtin_registry().driver_for("carrier-pigeon")


def test_lookup_errors_share_one_base_for_catch_all_recovery():
    assert issubclass(UnknownProviderError, ProviderError)


def test_a_second_registration_for_an_id_loses():
    registry = ProviderRegistry()
    first = RecordingDriver("example")
    registry.register(first)

    with pytest.raises(DuplicateProviderError):
        registry.register(RecordingDriver("example"))

    assert registry.driver_for("example") is first


def test_a_driver_without_an_id_is_rejected():
    registry = ProviderRegistry()

    with pytest.raises(ValueError):
        registry.register(RecordingDriver(""))


def test_descriptor_for_is_none_when_no_driver_serves_the_id():
    registry = builtin_registry()

    assert registry.descriptor_for("carrier-pigeon") is None
    assert registry.descriptor_for(PROTOCOL_IMAP) is not None


def test_a_missing_provider_account_stays_recoverable(monkeypatch):
    """Every operation raises the lookup error, touches no backend, and the
    account itself is still describable -- nothing to delete it has to sync."""
    orphan = account("carrier-pigeon")

    def forbidden(*args, **kwargs):
        pytest.fail("A missing provider reached a backend session")

    monkeypatch.setattr(mail_sync, "ImapSession", forbidden)
    monkeypatch.setattr(mail_sync, "SmtpSession", forbidden)
    monkeypatch.setattr(mail_sync, "GraphSession", forbidden)
    monkeypatch.setattr(mail_sync, "TutaSession", forbidden)

    with pytest.raises(UnknownProviderError):
        mail_sync.fetch_mailbox(orphan, CREDENTIAL)
    with pytest.raises(UnknownProviderError):
        mail_sync.fetch_full_message(
            orphan, CREDENTIAL, "inbox", "message", uidvalidity=None
        )
    with pytest.raises(UnknownProviderError):
        mail_sync.set_flag(
            orphan, CREDENTIAL, "inbox", ["message"], FLAG_SEEN, True, uidvalidity=None
        )
    with pytest.raises(UnknownProviderError):
        mail_sync.move_messages(
            orphan, CREDENTIAL, "inbox", ["message"], "archive", uidvalidity=None
        )
    with pytest.raises(UnknownProviderError):
        mail_sync.send_message(
            orphan, CREDENTIAL, orphan.email, ["other@example.com"], b"message"
        )

    assert mail_sync.provider_descriptor(orphan.protocol) is None
    for capability in Capability:
        assert not mail_sync.provider_supports(orphan.protocol, capability)


def test_flagging_nothing_stays_a_noop_for_a_missing_provider():
    orphan = account("carrier-pigeon")

    assert (
        mail_sync.set_flag(
            orphan, CREDENTIAL, "inbox", [], FLAG_SEEN, True, uidvalidity=None
        )
        == ()
    )


def test_an_unknown_provider_watcher_polls_instead_of_opening_imap(monkeypatch):
    orphan = account("carrier-pigeon")

    def forbidden(*args, **kwargs):
        pytest.fail("A missing provider's watcher opened a backend session")

    monkeypatch.setattr(mail_sync, "ImapSession", forbidden)
    watcher = mail_sync._InboxWatcher(orphan, lambda _: CREDENTIAL, lambda _: None)
    watcher.stop()
    watcher._run()


def test_fetch_dispatch_hands_the_driver_an_immutable_request(monkeypatch):
    driver = RecordingDriver(PROTOCOL_IMAP)
    registry = ProviderRegistry()
    registry.register(driver)
    use_registry(monkeypatch, registry)

    result = mail_sync.fetch_mailbox(
        account(),
        CREDENTIAL,
        folder="INBOX",
        limit=10,
        offset=5,
        should_count_unread=False,
    )

    assert result is driver._fetch_result
    (request,) = driver.requests
    assert isinstance(request, FetchRequest)
    assert (request.folder, request.limit, request.offset) == ("INBOX", 10, 5)
    assert request.should_count_unread is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.limit = 99


def test_a_partial_result_reaches_the_caller_marked_partial(monkeypatch):
    driver = RecordingDriver(
        PROTOCOL_IMAP, fetch_result=SyncResult(completeness=SyncCompleteness.PARTIAL)
    )
    registry = ProviderRegistry()
    registry.register(driver)
    use_registry(monkeypatch, registry)

    result = mail_sync.fetch_mailbox(account(), CREDENTIAL)

    assert result.completeness is SyncCompleteness.PARTIAL


def test_set_flag_reports_each_id_outcome(monkeypatch):
    outcomes = (PerIdOutcome("4", True), PerIdOutcome("9", False, "gone"))
    driver = RecordingDriver(PROTOCOL_IMAP, flag_outcomes=outcomes)
    registry = ProviderRegistry()
    registry.register(driver)
    use_registry(monkeypatch, registry)

    assert (
        mail_sync.set_flag(
            account(), CREDENTIAL, "INBOX", ["4", "9"], FLAG_SEEN, True, uidvalidity=1
        )
        == outcomes
    )


class SilentImapSession:
    """An IMAP server holding one empty inbox."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def connect(self) -> None:
        pass

    def sign_in(self, credential) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def list_folders(self):
        return [MailboxInfo("INBOX", "/", "")]

    def select(self, mailbox) -> int:
        return 0

    def uidvalidity(self) -> int:
        return 1

    def search_all_uids(self):
        return set()

    def fetch_recent_headers(self, exists, limit, offset):
        return []


def test_a_builtin_fetch_reports_complete(monkeypatch):
    monkeypatch.setattr(mail_sync, "ImapSession", SilentImapSession)

    result = mail_sync.fetch_mailbox(account(id=4242), CREDENTIAL)

    assert result.completeness is SyncCompleteness.COMPLETE
    assert result.outcomes == ()
    assert [mailbox.name for mailbox in result.folders] == ["INBOX"]
