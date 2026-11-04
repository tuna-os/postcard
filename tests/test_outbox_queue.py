"""Queued sends: the Outbox as a state machine.

A mail is queued with its full envelope, claimed before its worker starts,
and completed or failed when the worker reports back. A crash between SMTP
acceptance and completion leaves a sending row; its retry looks for its own
copy in Sent before touching SMTP, so the mail goes out exactly once.
"""

import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import Account
from postcard.core.net.auth import Credential
from postcard.core.net.imap_session import ImapError, MailboxInfo
from postcard.core.net.proton_session import ProtonError
from postcard.core.net.smtp_session import SmtpError
from postcard.core.net.tuta_session import TutaError
from postcard.core.store.database import Database, OutboxState

CREDENTIAL = Credential("ada@example.com", "hunter2")
MESSAGE_ID = "<first@example.com>"


class FakeSmtpSession:
    """An SMTP server that records every send, failing on request."""

    sends: list[tuple[str, list[str], bytes]] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def sign_in(self, credential):
        pass

    def send_raw(self, from_addr, recipients, raw):
        if type(self).fail_with is not None:
            raise type(self).fail_with
        type(self).sends.append((from_addr, list(recipients), raw))

    def quit(self):
        pass


class FakeImapSession:
    """An IMAP server with a Sent mailbox, a Message-ID index, and UIDPLUS."""

    mailboxes: list[MailboxInfo] = []
    capabilities: tuple[str, ...] = ()
    appends: list[tuple[str, bytes]] = []
    appenduid: tuple[int, str] | None = None
    sent_uids: set[str] = set()
    validity: int | None = None
    searches: list[str] = []
    selected: list[str] = []
    fail_search_with: Exception | None = None
    fail_append_with: Exception | None = None

    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def sign_in(self, credential):
        pass

    def is_alive(self):
        return True

    def logout(self):
        pass

    def list_folders(self):
        return type(self).mailboxes

    def select(self, mailbox, is_readonly=True):
        type(self).selected.append(mailbox)
        return 1

    def uidvalidity(self):
        return type(self).validity

    def search_message_id(self, message_id):
        if type(self).fail_search_with is not None:
            raise type(self).fail_search_with
        type(self).searches.append(message_id)
        return set(type(self).sent_uids)

    def has_capability(self, name):
        return name in type(self).capabilities

    def append(self, mailbox, raw):
        if type(self).fail_append_with is not None:
            raise type(self).fail_append_with
        type(self).appends.append((mailbox, raw))
        return type(self).appenduid


class FakeGraphSession:
    """Exchange: records sends, answers the Sent-copy lookup."""

    sends: list[bytes] = []
    sent_ids: list[str] = []

    def __init__(self, credential):
        pass

    def get(self, path, prefer=""):
        assert "internetMessageId" in path
        return {"value": [{"id": found} for found in type(self).sent_ids]}


@pytest.fixture(autouse=True)
def clean_sync_state():
    mail_sync.close_sessions()
    mail_sync._live_claims.clear()
    yield
    mail_sync.close_sessions()
    mail_sync._live_claims.clear()


@pytest.fixture
def servers(monkeypatch):
    FakeSmtpSession.sends = []
    FakeSmtpSession.fail_with = None
    FakeImapSession.mailboxes = [
        MailboxInfo("INBOX", "/", ""),
        MailboxInfo("Sent", "/", ""),
    ]
    FakeImapSession.capabilities = ()
    FakeImapSession.appends = []
    FakeImapSession.appenduid = None
    FakeImapSession.sent_uids = set()
    FakeImapSession.validity = None
    FakeImapSession.searches = []
    FakeImapSession.selected = []
    FakeImapSession.fail_search_with = None
    FakeImapSession.fail_append_with = None
    FakeGraphSession.sends = []
    FakeGraphSession.sent_ids = []
    monkeypatch.setattr(mail_sync, "SmtpSession", FakeSmtpSession)
    monkeypatch.setattr(mail_sync, "ImapSession", FakeImapSession)
    monkeypatch.setattr(mail_sync, "GraphSession", FakeGraphSession)
    monkeypatch.setattr(mail_sync.graph_send, "send_mime", _record_graph_send)
    return FakeSmtpSession, FakeImapSession, FakeGraphSession


def _record_graph_send(session, raw, recipients):
    FakeGraphSession.sends.append(raw)


@pytest.fixture
def stored():
    database = Database(":memory:")
    account = database.save_account(
        "ada@example.com", "Ada", "imap.example.com", 993, "smtp.example.com", 587
    )
    yield database, account
    database.close()


def _raw(message_id=MESSAGE_ID):
    return f"To: you@example.com\r\nMessage-ID: {message_id}\r\n\r\nbody".encode()


def _queued(database, account_id, envelope=None, raw=None, subject="Hi"):
    """An Outbox mail, queued and claimed, as the composer leaves it."""
    outbox = database.get_or_create_folder(account_id, mail_sync.OUTBOX_FOLDER)
    mail = database.save_email(outbox.id, "you@x", subject, subject, "now", False)
    database.save_raw_message(mail.id, raw if raw is not None else _raw())
    database.queue_outbox_email(
        mail.id, envelope if envelope is not None else ["you@example.com"]
    )
    job = mail_sync.claim_queued_send(database, mail.id)
    assert job is not None
    return outbox, mail, job


def _account(**overrides):
    fields = {
        "id": 1,
        "email": "ada@example.com",
        "display_name": "Ada",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
    }
    fields.update(overrides)
    return Account(**fields)


# --- claims ------------------------------------------------------------------


def test_a_second_claim_while_sending_holds_nothing(stored):
    database, account = stored
    _outbox, mail, _job = _queued(database, account.id)

    assert mail_sync.claim_queued_send(database, mail.id) is None


def test_recovery_spares_a_live_claim(stored):
    database, account = stored
    _outbox, mail, _job = _queued(database, account.id)

    assert mail_sync.recover_queued_sends(database) == 0
    assert database.queued_send(mail.id).state is OutboxState.SENDING

    mail_sync.release_queued_send(mail.id)

    assert mail_sync.recover_queued_sends(database) == 1
    assert database.queued_send(mail.id).state is OutboxState.READY


def test_failures_retry_until_the_cap_then_rest_failed(stored):
    database, account = stored
    _outbox, mail, _job = _queued(database, account.id)

    states = []
    for _ in range(mail_sync.MAX_SEND_ATTEMPTS):
        states.append(mail_sync.fail_queued_send(database, mail.id, "timed out"))
        mail_sync.release_queued_send(mail.id)
        mail_sync.recover_queued_sends(database)
        mail_sync.claim_queued_send(database, mail.id)

    assert states == [OutboxState.RETRY] * (mail_sync.MAX_SEND_ATTEMPTS - 1) + [
        OutboxState.FAILED
    ]
    assert database.queued_send(mail.id).last_error == "timed out"
    assert database.queued_send(mail.id).attempts == mail_sync.MAX_SEND_ATTEMPTS
    mail_sync.release_queued_send(mail.id)
    assert mail_sync.claim_queued_send(database, mail.id) is None


# --- sending -----------------------------------------------------------------


def test_a_first_attempt_sends_without_looking_in_sent(stored, servers):
    database, account = stored
    _smtp, imap, _graph = servers
    _outbox, _mail, job = _queued(
        database, account.id, envelope=["you@example.com", "bcc@example.com"]
    )

    outcome = mail_sync.send_queued(_account(), CREDENTIAL, job)

    assert outcome.file_error is None
    assert imap.searches == []
    [sent] = FakeSmtpSession.sends
    assert sent[1] == ["you@example.com", "bcc@example.com"]


def test_a_retry_without_a_sent_copy_resends(stored, servers):
    database, account = stored
    _outbox, mail, _job = _queued(database, account.id)
    mail_sync.fail_queued_send(database, mail.id, "timed out")
    mail_sync.release_queued_send(mail.id)
    mail_sync.recover_queued_sends(database)
    retry = mail_sync.claim_queued_send(database, mail.id)

    mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert len(FakeSmtpSession.sends) == 1


def test_a_retry_with_a_sent_copy_files_without_resending(stored, servers):
    database, account = stored
    _smtp, imap, _graph = servers
    imap.sent_uids = {"7"}
    imap.validity = 38505
    _outbox, mail, _job = _queued(database, account.id)
    mail_sync.fail_queued_send(database, mail.id, "timed out")
    mail_sync.release_queued_send(mail.id)
    mail_sync.recover_queued_sends(database)
    retry = mail_sync.claim_queued_send(database, mail.id)

    outcome = mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert FakeSmtpSession.sends == []
    assert imap.searches == [MESSAGE_ID]
    assert (outcome.filed_uid, outcome.uidvalidity) == ("7", 38505)


def test_a_crash_between_send_and_filing_sends_exactly_once(stored, servers):
    # Claim, SMTP-accept, then die before completing: the row stays sending.
    # The next drain recovers it, finds its own copy in Sent, and files the
    # local copy without ever touching SMTP again.
    database, account = stored
    _smtp, imap, _graph = servers
    imap.appenduid = (38505, "7")
    outbox, mail, job = _queued(database, account.id)

    outcome = mail_sync.send_queued(_account(), CREDENTIAL, job)
    assert outcome.file_error is None
    imap.sent_uids = {"7"}
    imap.validity = 38505
    # ... the process dies here: no complete, no release, a fresh boot ...
    mail_sync._live_claims.clear()

    assert mail_sync.recover_queued_sends(database) == 1
    retry = mail_sync.claim_queued_send(database, mail.id)
    assert retry.attempts == 2
    outcome = mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert len(FakeSmtpSession.sends) == 1
    sent, validity_changed = mail_sync.file_sent_email(
        database, _account(), retry, outcome, ("You", "you@example.com")
    )
    assert validity_changed is True
    assert database.queued_send(mail.id) is None
    assert database.emails_in_folder(outbox.id) == []
    [copy] = database.emails_in_folder(sent.id)
    assert (copy.subject, copy.server_id) == ("Hi", "7")


def test_a_failed_redelivery_check_fails_the_attempt_instead_of_sending(
    stored, servers
):
    # A check that errors says nothing about whether the mail left; sending on
    # could deliver it twice, while failing only retries it.
    database, account = stored
    _smtp, imap, _graph = servers
    imap.fail_search_with = ImapError("search failed: boom")
    _outbox, mail, _job = _queued(database, account.id)
    mail_sync.fail_queued_send(database, mail.id, "timed out")
    mail_sync.release_queued_send(mail.id)
    mail_sync.recover_queued_sends(database)
    retry = mail_sync.claim_queued_send(database, mail.id)

    with pytest.raises(ImapError, match="search failed"):
        mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert FakeSmtpSession.sends == []


def test_a_retry_without_a_message_id_cannot_check_so_it_resends(stored, servers):
    database, account = stored
    _outbox, mail, _job = _queued(database, account.id, raw=b"To: you@x\r\n\r\n")
    mail_sync.fail_queued_send(database, mail.id, "timed out")
    mail_sync.release_queued_send(mail.id)
    mail_sync.recover_queued_sends(database)
    retry = mail_sync.claim_queued_send(database, mail.id)
    assert retry.message_id == ""

    mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert len(FakeSmtpSession.sends) == 1


def test_legacy_mail_falls_back_to_header_recipients(stored, servers):
    # Mail queued before the queue table has no envelope row at all.
    database, account = stored
    outbox = database.get_or_create_folder(account.id, mail_sync.OUTBOX_FOLDER)
    mail = database.save_email(outbox.id, "you@x", "Hi", "Hi", "now", False)
    database.save_raw_message(mail.id, _raw())

    [legacy] = database.pending_outbox_sends(outbox.id)
    assert legacy.envelope is None

    mail_sync.send_queued(_account(), CREDENTIAL, legacy)

    [sent] = FakeSmtpSession.sends
    assert sent[1] == ["you@example.com"]


def test_a_partial_smtp_refusal_fails_the_attempt(stored, servers):
    database, account = stored
    _outbox, mail, job = _queued(
        database, account.id, envelope=["you@example.com", "gone@example.com"]
    )
    FakeSmtpSession.fail_with = SmtpError(
        "could not send to gone@example.com: gone@example.com: 550 gone"
    )

    with pytest.raises(SmtpError, match="could not send to gone@example.com"):
        mail_sync.send_queued(_account(), CREDENTIAL, job)

    state = mail_sync.fail_queued_send(database, mail.id, "could not send to ...")
    assert state is OutboxState.RETRY
    failed = database.queued_send(mail.id)
    assert failed.envelope == ["you@example.com", "gone@example.com"]
    assert failed.attempts == 1


def test_tuta_and_proton_raise_without_a_sent_check(stored, servers):
    database, account = stored
    _smtp, imap, _graph = servers
    _outbox, _mail, job = _queued(database, account.id)

    with pytest.raises(TutaError):
        mail_sync.send_queued(_account(protocol="tuta"), CREDENTIAL, job)
    with pytest.raises(ProtonError):
        mail_sync.send_queued(_account(protocol="proton"), CREDENTIAL, job)

    assert imap.searches == []
    assert imap.selected == []


def test_a_graph_retry_with_a_sent_copy_skips_the_send(stored, servers):
    database, account = stored
    _smtp, _imap, graph = servers
    graph.sent_ids = ["gid-1"]
    _outbox, mail, _job = _queued(database, account.id)
    mail_sync.fail_queued_send(database, mail.id, "timed out")
    mail_sync.release_queued_send(mail.id)
    mail_sync.recover_queued_sends(database)
    retry = mail_sync.claim_queued_send(database, mail.id)

    outcome = mail_sync.send_queued(_account(protocol="graph"), CREDENTIAL, retry)

    assert FakeGraphSession.sends == []
    assert (outcome.filed_uid, outcome.uidvalidity) == ("gid-1", None)


def test_a_graph_retry_without_a_copy_sends(stored, servers):
    database, account = stored
    _outbox, mail, _job = _queued(database, account.id)
    mail_sync.fail_queued_send(database, mail.id, "timed out")
    mail_sync.release_queued_send(mail.id)
    mail_sync.recover_queued_sends(database)
    retry = mail_sync.claim_queued_send(database, mail.id)

    mail_sync.send_queued(_account(protocol="graph"), CREDENTIAL, retry)

    assert len(FakeGraphSession.sends) == 1


# --- filing ------------------------------------------------------------------


def test_a_failed_append_reports_instead_of_raising(servers):
    _smtp, imap, _graph = servers
    imap.fail_append_with = ImapError("could not append to Sent: over quota")

    outcome = mail_sync.send_message(
        _account(), CREDENTIAL, "ada@example.com", ["you@example.com"], b"raw"
    )

    assert len(FakeSmtpSession.sends) == 1
    assert outcome.filed_uid is None
    assert isinstance(outcome.file_error, ImapError)


def test_uidplus_links_the_local_sent_copy(stored, servers):
    database, account = stored
    _smtp, imap, _graph = servers
    imap.appenduid = (38505, "3955")
    outbox, mail, job = _queued(database, account.id)

    outcome = mail_sync.send_queued(_account(), CREDENTIAL, job)
    sent, validity_changed = mail_sync.file_sent_email(
        database, _account(), job, outcome, ("You", "you@example.com")
    )

    assert validity_changed is True
    stored_sent = database.get_folder_by_name(account.id, sent.name)
    assert stored_sent is not None
    assert stored_sent.uidvalidity == 38505
    [copy] = database.emails_in_folder(sent.id)
    assert copy.server_id == "3955"
    assert copy.message_id == MESSAGE_ID
    assert database.emails_in_folder(outbox.id) == []
    assert database.queued_send(mail.id) is None


def test_a_second_completion_single_flights_the_validity_bump(stored, servers):
    database, account = stored
    _outbox, _mail, job = _queued(database, account.id)
    outcome = mail_sync.SendOutcome("7", 38505, None)

    _sent, first = mail_sync.file_sent_email(
        database, _account(), job, outcome, ("You", "you@example.com")
    )
    _sent, second = mail_sync.file_sent_email(
        database, _account(), job, outcome, ("You", "you@example.com")
    )

    assert (first, second) == (True, False)


def test_filing_twice_keeps_a_single_sent_copy(stored, servers):
    # A crash between filing and Outbox cleanup files the same mail again on
    # retry; the second insert must replace, not collide with, the first.
    database, account = stored
    _outbox, _mail, job = _queued(database, account.id)
    outcome = mail_sync.SendOutcome("7", 38505, None)

    mail_sync.file_sent_email(
        database, _account(), job, outcome, ("You", "you@example.com")
    )
    sent, _changed = mail_sync.file_sent_email(
        database, _account(), job, outcome, ("You", "you@example.com")
    )

    assert [mail.subject for mail in database.emails_in_folder(sent.id)] == ["Hi"]
