"""SMTP partial delivery: per-recipient refusal without resending the accepted.

smtplib reports per-recipient refusals by returning them, not by raising. A
refusal for some recipients raises SmtpRefusedError naming who was refused
and who already got it; the queue narrows to the refused recipients so the
retry sends to them only. A mid-send disconnect leaves every recipient's
state unknown, so that retry sends to all of them again.
"""

import email
import smtplib

import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import Account
from postcard.core.net.auth import Credential
from postcard.core.net.imap_session import MailboxInfo
from postcard.core.net.smtp_session import SmtpError, SmtpRefusedError, SmtpSession
from postcard.core.store.database import Database, OutboxState

CREDENTIAL = Credential("ada@example.com", "hunter2")
MESSAGE_ID = "<partial@example.com>"
YOU = "you@example.com"
GONE = "gone@example.com"


class FakeSmtpSession:
    """An SMTP server that refuses or drops on request, recording every send."""

    sends: list[tuple[str, list[str], bytes]] = []
    refused: dict = {}
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
        if type(self).refused:
            refused = dict(type(self).refused)
            accepted = tuple(a for a in recipients if a not in refused)
            raise SmtpRefusedError(tuple(refused), accepted)

    def quit(self):
        pass


class FakeImapSession:
    """An IMAP server with a Sent mailbox and a Message-ID index."""

    mailboxes: list[MailboxInfo] = []
    appends: list[tuple[str, bytes]] = []
    sent_uids: set[str] = set()
    sent_by_id: dict[str, set[str]] | None = None
    validity: int | None = None

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
        return 1

    def uidvalidity(self):
        return type(self).validity

    def search_message_id(self, message_id):
        by_id = type(self).sent_by_id
        if by_id is not None:
            return set(by_id.get(message_id, set()))
        return set(type(self).sent_uids)

    def has_capability(self, name):
        return False

    def append(self, mailbox, raw):
        type(self).appends.append((mailbox, raw))


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
    FakeSmtpSession.refused = {}
    FakeSmtpSession.fail_with = None
    FakeImapSession.mailboxes = [
        MailboxInfo("INBOX", "/", ""),
        MailboxInfo("Sent", "/", ""),
    ]
    FakeImapSession.appends = []
    FakeImapSession.sent_uids = set()
    FakeImapSession.sent_by_id = None
    FakeImapSession.validity = None
    monkeypatch.setattr(mail_sync, "SmtpSession", FakeSmtpSession)
    monkeypatch.setattr(mail_sync, "ImapSession", FakeImapSession)
    return FakeSmtpSession, FakeImapSession


@pytest.fixture
def stored():
    database = Database(":memory:")
    account = database.save_account(
        "ada@example.com", "Ada", "imap.example.com", 993, "smtp.example.com", 587
    )
    yield database, account
    database.close()


def _raw(message_id=MESSAGE_ID):
    return f"To: {YOU}\r\nMessage-ID: {message_id}\r\n\r\nbody".encode()


def _queued(database, account_id, envelope=None, raw=None):
    """An Outbox mail, queued and claimed, as the composer leaves it."""
    outbox = database.get_or_create_folder(account_id, mail_sync.OUTBOX_FOLDER)
    mail = database.save_email(outbox.id, "you@x", "Hi", "Hi", "now", False)
    database.save_raw_message(mail.id, raw if raw is not None else _raw())
    database.queue_outbox_email(mail.id, envelope if envelope is not None else [YOU])
    job = mail_sync.claim_queued_send(database, mail.id)
    assert job is not None
    return outbox, mail, job


def _raw_without_id():
    return f"To: {YOU}\r\n\r\nbody".encode()


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


def _retry(database, mail_id):
    """Fail recovery's round trip: release, recover, claim the next attempt."""
    mail_sync.release_queued_send(mail_id)
    mail_sync.recover_queued_sends(database)
    job = mail_sync.claim_queued_send(database, mail_id)
    assert job is not None
    return job


# --- the boundary itself -----------------------------------------------------


class StubSmtp:
    """A stdlib SMTP double answering sendmail as told."""

    refused: dict = {}
    fail_with: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    def starttls(self, context=None):
        pass

    def sendmail(self, from_addr, recipients, raw):
        if type(self).fail_with is not None:
            raise type(self).fail_with
        return dict(type(self).refused)

    def quit(self):
        pass


@pytest.fixture
def stubbed_smtp(monkeypatch):
    StubSmtp.refused = {}
    StubSmtp.fail_with = None
    monkeypatch.setattr(smtplib, "SMTP", StubSmtp)
    return StubSmtp


def test_send_raw_returns_quietly_when_every_recipient_accepts(stubbed_smtp):
    session = SmtpSession("smtp.example.com", 587, "starttls")
    session.connect()

    assert session.send_raw("ada@example.com", [YOU, GONE], b"raw") is None


def test_send_raw_reports_a_partial_refusal_with_both_sets(stubbed_smtp):
    stubbed_smtp.refused = {GONE: (550, b"5.1.1 mailbox unavailable")}
    session = SmtpSession("smtp.example.com", 587, "starttls")
    session.connect()

    with pytest.raises(SmtpRefusedError) as caught:
        session.send_raw("ada@example.com", [YOU, GONE], b"raw")

    assert caught.value.accepted == (YOU,)
    assert caught.value.refused == (GONE,)
    assert caught.value.unknown == ()


def test_send_raw_reports_a_total_refusal_with_nobody_accepted(stubbed_smtp):
    stubbed_smtp.refused = {
        YOU: (550, b"rejected"),
        GONE: (550, b"rejected"),
    }
    session = SmtpSession("smtp.example.com", 587, "starttls")
    session.connect()

    with pytest.raises(SmtpRefusedError) as caught:
        session.send_raw("ada@example.com", [YOU, GONE], b"raw")

    assert caught.value.accepted == ()
    assert caught.value.refused == (YOU, GONE)


def test_a_refusal_names_addresses_but_not_the_server_verbatim(stubbed_smtp):
    stubbed_smtp.refused = {GONE: (550, b"5.1.1 internal secret-marker-xyz")}
    session = SmtpSession("smtp.example.com", 587, "starttls")
    session.connect()

    with pytest.raises(SmtpRefusedError) as caught:
        session.send_raw("ada@example.com", [YOU, GONE], b"raw")

    assert GONE in str(caught.value)
    assert "secret-marker-xyz" not in str(caught.value)


def test_a_disconnect_marks_every_recipient_unknown(stubbed_smtp):
    stubbed_smtp.fail_with = smtplib.SMTPServerDisconnected("connection lost")
    session = SmtpSession("smtp.example.com", 587, "starttls")
    session.connect()

    with pytest.raises(SmtpError) as caught:
        session.send_raw("ada@example.com", [YOU, GONE], b"raw")

    assert type(caught.value) is SmtpError
    assert caught.value.unknown == (YOU, GONE)


def test_a_refusal_with_nobody_refused_is_a_caller_bug():
    with pytest.raises(ValueError, match="at least one refused recipient"):
        SmtpRefusedError(())


# --- through the queue -------------------------------------------------------


def test_full_acceptance_sends_once_and_files(stored, servers):
    database, account = stored
    _smtp, imap = servers
    outbox, mail, job = _queued(database, account.id, envelope=[YOU, GONE])

    outcome = mail_sync.send_queued(_account(), CREDENTIAL, job)

    assert outcome.file_error is None
    assert [sent[1] for sent in FakeSmtpSession.sends] == [[YOU, GONE]]
    assert len(imap.appends) == 1
    sent, _changed = mail_sync.file_sent_email(
        database, _account(), job, outcome, ("You", YOU)
    )
    assert database.emails_in_folder(outbox.id) == []
    assert [mail.subject for mail in database.emails_in_folder(sent.id)] == ["Hi"]
    assert database.queued_send(mail.id) is None


def test_a_partial_refusal_files_nothing_to_sent(stored, servers):
    # A Sent copy would make the retry's redelivery check read the mail as
    # fully sent and drop the refused recipients.
    database, account = stored
    _smtp, imap = servers
    FakeSmtpSession.refused = {GONE: (550, b"mailbox unavailable")}
    _outbox, _mail, job = _queued(database, account.id, envelope=[YOU, GONE])

    with pytest.raises(SmtpRefusedError):
        mail_sync.send_queued(_account(), CREDENTIAL, job)

    assert imap.appends == []


def test_a_partial_retry_sends_to_the_refused_only(stored, servers):
    database, account = stored
    outbox, mail, job = _queued(database, account.id, envelope=[YOU, GONE])
    FakeSmtpSession.refused = {GONE: (550, b"mailbox unavailable")}

    with pytest.raises(SmtpRefusedError) as caught:
        mail_sync.send_queued(_account(), CREDENTIAL, job)

    state = mail_sync.fail_refused_send(
        database, mail.id, caught.value.refused, str(caught.value)
    )
    assert state is OutboxState.RETRY
    narrowed = database.queued_send(mail.id)
    assert narrowed.envelope == [GONE]
    assert narrowed.attempts == 1

    FakeSmtpSession.refused = {}
    retry = _retry(database, mail.id)
    assert retry.envelope == [GONE]
    outcome = mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert [sent[1] for sent in FakeSmtpSession.sends] == [[YOU, GONE], [GONE]]
    sent, _changed = mail_sync.file_sent_email(
        database, _account(), retry, outcome, ("You", YOU)
    )
    assert database.emails_in_folder(outbox.id) == []
    assert database.queued_send(mail.id) is None
    assert [mail.subject for mail in database.emails_in_folder(sent.id)] == ["Hi"]


def test_a_total_refusal_retries_every_recipient(stored, servers):
    database, account = stored
    _outbox, mail, job = _queued(database, account.id, envelope=[YOU, GONE])
    FakeSmtpSession.refused = {
        YOU: (550, b"rejected"),
        GONE: (550, b"rejected"),
    }

    with pytest.raises(SmtpRefusedError) as caught:
        mail_sync.send_queued(_account(), CREDENTIAL, job)

    assert caught.value.accepted == ()
    state = mail_sync.fail_refused_send(
        database, mail.id, caught.value.refused, str(caught.value)
    )
    assert state is OutboxState.RETRY
    assert database.queued_send(mail.id).envelope == [YOU, GONE]

    FakeSmtpSession.refused = {}
    retry = _retry(database, mail.id)
    mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert [sent[1] for sent in FakeSmtpSession.sends] == [[YOU, GONE], [YOU, GONE]]


def test_a_disconnect_after_data_retries_every_recipient(stored, servers):
    # The link died mid-send: anyone may have gotten it, so no one can be
    # narrowed away, and the envelope stays whole for the retry.
    database, account = stored
    _outbox, mail, job = _queued(database, account.id, envelope=[YOU, GONE])
    FakeSmtpSession.fail_with = SmtpError(
        "could not send: connection lost", unknown=(YOU, GONE)
    )

    with pytest.raises(SmtpError) as caught:
        mail_sync.send_queued(_account(), CREDENTIAL, job)

    assert type(caught.value) is SmtpError
    assert caught.value.unknown == (YOU, GONE)
    state = mail_sync.fail_queued_send(database, mail.id, str(caught.value))
    assert state is OutboxState.RETRY
    assert database.queued_send(mail.id).envelope == [YOU, GONE]

    FakeSmtpSession.fail_with = None
    retry = _retry(database, mail.id)
    mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert [sent[1] for sent in FakeSmtpSession.sends] == [[YOU, GONE]]


def test_refusals_rest_failed_with_only_the_refused_left(stored, servers):
    database, account = stored
    FakeSmtpSession.refused = {GONE: (550, b"mailbox unavailable")}
    _outbox, mail, job = _queued(database, account.id, envelope=[YOU, GONE])

    states = []
    attempt = job
    for _ in range(mail_sync.MAX_SEND_ATTEMPTS):
        with pytest.raises(SmtpRefusedError) as caught:
            mail_sync.send_queued(_account(), CREDENTIAL, attempt)
        states.append(
            mail_sync.fail_refused_send(
                database, mail.id, caught.value.refused, str(caught.value)
            )
        )
        if states[-1] is OutboxState.FAILED:
            break
        attempt = _retry(database, mail.id)

    assert states == [OutboxState.RETRY] * (mail_sync.MAX_SEND_ATTEMPTS - 1) + [
        OutboxState.FAILED
    ]
    failed = database.queued_send(mail.id)
    assert failed.envelope == [GONE]
    assert failed.attempts == mail_sync.MAX_SEND_ATTEMPTS
    mail_sync.release_queued_send(mail.id)
    assert mail_sync.claim_queued_send(database, mail.id) is None


def test_a_bcc_refusal_retries_without_naming_bcc_in_mime(stored, servers):
    # The refused Bcc address travels in the envelope only: the retry sends
    # to it, while the stored message still names nobody hidden.
    database, account = stored
    hidden = "hidden@example.com"
    raw = _raw()
    assert hidden.encode() not in raw
    _outbox, mail, job = _queued(database, account.id, envelope=[YOU, hidden])
    FakeSmtpSession.refused = {hidden: (550, b"mailbox unavailable")}

    with pytest.raises(SmtpRefusedError) as caught:
        mail_sync.send_queued(_account(), CREDENTIAL, job)

    assert caught.value.refused == (hidden,)
    assert caught.value.accepted == (YOU,)
    mail_sync.fail_refused_send(
        database, mail.id, caught.value.refused, str(caught.value)
    )
    assert database.queued_send(mail.id).envelope == [hidden]

    FakeSmtpSession.refused = {}
    retry = _retry(database, mail.id)
    mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert [sent[1] for sent in FakeSmtpSession.sends] == [[YOU, hidden], [hidden]]
    assert hidden.encode() not in retry.raw


def test_a_partial_refusal_rotates_the_message_id(stored, servers):
    database, account = stored
    _outbox, mail, job = _queued(database, account.id, envelope=[YOU, GONE])
    FakeSmtpSession.refused = {GONE: (550, b"mailbox unavailable")}

    with pytest.raises(SmtpRefusedError) as caught:
        mail_sync.send_queued(_account(), CREDENTIAL, job)
    mail_sync.fail_refused_send(
        database, mail.id, caught.value.refused, str(caught.value)
    )

    rotated = database.queued_send(mail.id)
    assert rotated.message_id != MESSAGE_ID
    assert rotated.message_id.startswith("<") and rotated.message_id.endswith(">")
    headers = email.message_from_bytes(rotated.raw)
    assert headers["Message-ID"] == rotated.message_id
    assert headers["To"] == YOU
    assert rotated.raw.endswith(b"body")


def test_a_retry_ignores_a_sent_copy_filed_under_the_old_id(stored, servers):
    # A server that auto-files SMTP mail to Sent kept the first attempt under
    # its id; the refused-only retry goes out under a fresh one, so the
    # pre-flight finds nothing and the refused recipient is not dropped.
    database, account = stored
    _smtp, imap, *_rest = servers
    _outbox, mail, job = _queued(database, account.id, envelope=[YOU, GONE])
    FakeSmtpSession.refused = {GONE: (550, b"mailbox unavailable")}

    with pytest.raises(SmtpRefusedError) as caught:
        mail_sync.send_queued(_account(), CREDENTIAL, job)
    mail_sync.fail_refused_send(
        database, mail.id, caught.value.refused, str(caught.value)
    )
    # The server took the first attempt for YOU and filed it under its id.
    imap.sent_by_id = {MESSAGE_ID: {"7"}}

    FakeSmtpSession.refused = {}
    retry = _retry(database, mail.id)
    assert retry.message_id != MESSAGE_ID
    mail_sync.send_queued(_account(), CREDENTIAL, retry)

    assert [sent[1] for sent in FakeSmtpSession.sends] == [[YOU, GONE], [GONE]]


def test_mail_without_an_id_keeps_none_after_a_refusal(stored, servers):
    database, account = stored
    _outbox, mail, _job = _queued(
        database, account.id, envelope=[YOU, GONE], raw=_raw_without_id()
    )
    FakeSmtpSession.refused = {GONE: (550, b"mailbox unavailable")}
    claimed = database.queued_send(mail.id)
    assert claimed.message_id == ""

    with pytest.raises(SmtpRefusedError) as caught:
        mail_sync.send_queued(_account(), CREDENTIAL, claimed)
    mail_sync.fail_refused_send(
        database, mail.id, caught.value.refused, str(caught.value)
    )

    assert database.queued_send(mail.id).message_id == ""
