"""Bcc envelope preservation for queued mail and drafts.

A Bcc recipient is never written to the stored message, so the envelope kept
beside it is the only place Bcc survives a queue, a draft, a failure, or a
restart. Sending reads that envelope; a reopened draft restores Bcc from it;
Graph writes it into a Bcc header on the wire copy only.
"""

import base64
import email

import pytest

import postcard.mail_sync as mail_sync
from postcard.core import compose
from postcard.core.models.account import Account
from postcard.core.models.attachment import Attachment
from postcard.core.net.auth import Credential
from postcard.core.net.imap_session import MailboxInfo
from postcard.core.net.proton_session import ProtonError
from postcard.core.net.tuta_session import TutaError
from postcard.core.store.database import Database, OutboxState
from postcard.core.threader import NO_SUBJECT

CREDENTIAL = Credential("ada@example.com", "hunter2")


class FakeSmtpSession:
    """An SMTP server that records every send."""

    sends: list[tuple[str, list[str], bytes]] = []

    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def sign_in(self, credential):
        pass

    def send_raw(self, from_addr, recipients, raw):
        type(self).sends.append((from_addr, list(recipients), raw))

    def quit(self):
        pass


class FakeImapSession:
    """An IMAP server with a Sent mailbox and UIDPLUS."""

    mailboxes: list[MailboxInfo] = []
    appends: list[tuple[str, bytes]] = []
    appenduid: tuple[int, str] | None = None
    sent_uids: set[str] = set()
    validity: int | None = None

    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def sign_in(self, credential):
        pass

    def is_alive(self):
        return True

    def list_folders(self):
        return type(self).mailboxes

    def select(self, mailbox, is_readonly=True):
        return 1

    def uidvalidity(self):
        return type(self).validity

    def search_message_id(self, message_id):
        return set(type(self).sent_uids)

    def has_capability(self, name):
        return False

    def append(self, mailbox, raw):
        type(self).appends.append((mailbox, raw))
        return type(self).appenduid


class FakeGraphSession:
    """Exchange: records sends, answers the Sent-copy lookup."""

    requests: list[tuple[str, str, bytes]] = []
    sent_ids: list[str] = []

    def __init__(self, credential):
        pass

    def get(self, path, prefer=""):
        assert "internetMessageId" in path
        return {"value": [{"id": found} for found in type(self).sent_ids]}

    def request(self, method, path, body=b"", content_type=""):
        type(self).requests.append((method, path, bytes(body)))
        return {}


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
    FakeImapSession.mailboxes = [
        MailboxInfo("INBOX", "/", ""),
        MailboxInfo("Sent", "/", ""),
    ]
    FakeImapSession.appends = []
    FakeImapSession.appenduid = None
    FakeImapSession.sent_uids = set()
    FakeImapSession.validity = None
    FakeGraphSession.requests = []
    FakeGraphSession.sent_ids = []
    monkeypatch.setattr(mail_sync, "SmtpSession", FakeSmtpSession)
    monkeypatch.setattr(mail_sync, "ImapSession", FakeImapSession)
    monkeypatch.setattr(mail_sync, "GraphSession", FakeGraphSession)


@pytest.fixture
def stored():
    database = Database(":memory:")
    account = database.save_account(
        "ada@example.com", "Ada", "imap.example.com", 993, "smtp.example.com", 587
    )
    yield database, account
    database.close()


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


def _split(row: str) -> list[str]:
    """A composer recipient row as written into the MIME headers."""
    return [addr.strip() for addr in row.split(",") if addr.strip()]


def _queued(database, account_id, to="", cc="", bcc=""):
    """An Outbox mail, queued and claimed, as the composer leaves it."""
    outbox = database.get_or_create_folder(account_id, mail_sync.OUTBOX_FOLDER)
    msg = compose.build_mime_message(
        "ada@example.com", _split(to), _split(cc), "Hi", "<div>Hi</div>", []
    )
    mail = database.save_email(
        outbox.id, to or "(no recipient)", "Hi", "Hi", "now", False
    )
    raw = msg.as_bytes()
    database.save_raw_message(mail.id, raw)
    envelope = compose.build_envelope(to, cc, bcc)
    database.queue_outbox_email(mail.id, compose.flatten_envelope(envelope))
    job = mail_sync.claim_queued_send(database, mail.id)
    assert job is not None
    return mail, job, raw


def _saved_draft(database, account_id, draft):
    """A drafts-folder mail, as the composer leaves it on close."""
    drafts = mail_sync.drafts_folder(database, account_id)
    msg = compose.build_mime_message(
        "ada@example.com",
        _split(draft.to),
        _split(draft.cc),
        draft.subject or NO_SUBJECT,
        draft.body_html,
        list(draft.attachments),
    )
    envelope = compose.build_envelope(draft.to, draft.cc, draft.bcc)
    mail = database.save_email(
        drafts.id,
        draft.to or "(no recipient)",
        draft.subject or NO_SUBJECT,
        "Hi",
        "now",
        False,
        envelope=envelope,
    )
    raw = msg.as_bytes()
    database.save_raw_message(mail.id, raw)
    return mail, raw, envelope


def _headers(raw: bytes):
    return email.message_from_bytes(raw)


# --- envelope helpers --------------------------------------------------------


def test_recipient_rows_normalize_to_bare_addresses():
    assert compose.normalize_addresses("Ada <ada@example.com>, bob@example.com") == [
        "ada@example.com",
        "bob@example.com",
    ]
    assert compose.normalize_addresses("  ada@example.com ,, ADA@example.com ") == [
        "ada@example.com",
        "ADA@example.com",
    ]
    assert compose.normalize_addresses("") == []


def test_repeats_fold_but_order_survives():
    assert compose.normalize_addresses(
        "b@example.com, a@example.com, b@example.com"
    ) == [
        "b@example.com",
        "a@example.com",
    ]


def test_a_flattened_envelope_orders_to_cc_bcc_without_repeats():
    envelope = compose.build_envelope("a@example.com", "a@example.com", "c@example.com")

    assert envelope == {
        "to": ["a@example.com"],
        "cc": ["a@example.com"],
        "bcc": ["c@example.com"],
    }
    assert compose.flatten_envelope(envelope) == ["a@example.com", "c@example.com"]


# --- sending -----------------------------------------------------------------


def test_a_bcc_only_mail_sends_without_naming_bcc_in_mime(stored, servers):
    database, account = stored
    mail, job, raw = _queued(database, account.id, bcc="hidden@example.com")

    assert _headers(raw)["Bcc"] is None

    mail_sync.send_queued(_account(), CREDENTIAL, job)

    [sent] = FakeSmtpSession.sends
    assert sent[1] == ["hidden@example.com"]
    assert _headers(sent[2])["Bcc"] is None
    assert database.get_raw_message(mail.id) == raw


def test_a_mixed_mail_sends_to_everyone_with_to_and_cc_in_mime(stored, servers):
    database, account = stored
    _mail, job, raw = _queued(
        database,
        account.id,
        to="Ada <ada@example.com>",
        cc="bob@example.com",
        bcc="hidden@example.com",
    )
    headers = _headers(raw)

    assert headers["To"] == "Ada <ada@example.com>"
    assert headers["Cc"] == "bob@example.com"
    assert headers["Bcc"] is None

    mail_sync.send_queued(_account(), CREDENTIAL, job)

    [sent] = FakeSmtpSession.sends
    assert sent[1] == ["ada@example.com", "bob@example.com", "hidden@example.com"]
    assert _headers(sent[2])["Bcc"] is None


def test_the_exact_envelope_survives_failure_recovery_and_restart(stored, servers):
    database, account = stored
    _mail, _job = _queued(
        database, account.id, to="a@example.com", bcc="hidden@example.com"
    )[:2]
    mail_id = _job.email_id
    mail_sync.fail_queued_send(database, mail_id, "timed out")
    mail_sync.release_queued_send(mail_id)
    # ... the process restarts: claims are memory, the envelope is on disk ...
    mail_sync._live_claims.clear()
    mail_sync.recover_queued_sends(database)
    retry = mail_sync.claim_queued_send(database, mail_id)
    assert retry is not None

    assert retry.attempts == 2
    assert retry.envelope == ["a@example.com", "hidden@example.com"]

    mail_sync.send_queued(_account(), CREDENTIAL, retry)

    [sent] = FakeSmtpSession.sends
    assert sent[1] == ["a@example.com", "hidden@example.com"]


def test_a_queued_envelope_survives_a_database_reopen(tmp_path):
    # A restart is a new process on the same cache file, not just cleared
    # in-memory claims: the envelope has to be on disk to survive it.
    path = str(tmp_path / "postcard.db")
    key = b"0" * 32
    database = Database(path, key=key)
    account = database.save_account(
        "ada@example.com", "Ada", "imap.example.com", 993, "smtp.example.com", 587
    )
    mail, _job, _raw = _queued(
        database, account.id, to="a@example.com", bcc="hidden@example.com"
    )
    database.close()

    reopened = Database(path, key=key)
    try:
        stored = reopened.queued_send(mail.id)
        assert stored is not None
        assert stored.envelope == ["a@example.com", "hidden@example.com"]
    finally:
        reopened.close()


def test_unsupported_providers_keep_the_envelope_on_failure(stored, servers):
    database, account = stored
    for protocol, error in (("tuta", TutaError), ("proton", ProtonError)):
        mail, job, _raw = _queued(
            database, account.id, to="a@example.com", bcc="hidden@example.com"
        )
        with pytest.raises(error):
            mail_sync.send_queued(_account(protocol=protocol), CREDENTIAL, job)

        state = mail_sync.fail_queued_send(database, mail.id, "unsupported")

        assert state is OutboxState.RETRY
        assert database.queued_send(mail.id).envelope == [
            "a@example.com",
            "hidden@example.com",
        ]
        mail_sync.release_queued_send(mail.id)


# --- drafts ------------------------------------------------------------------


def test_a_draft_round_trip_restores_every_field_including_bcc(stored):
    database, account = stored
    attachment = Attachment(
        filename="notes.txt", mime_type="text/plain", content=b"hello"
    )
    mail, raw, envelope = _saved_draft(
        database,
        account.id,
        compose.ReopenedDraft(
            to="Ada <ada@example.com>",
            cc="bob@example.com",
            bcc="hidden@example.com",
            subject="Hi",
            body_html="<div>Hi <b>Ada</b></div>",
            attachments=(attachment,),
        ),
    )

    assert _headers(raw)["Bcc"] is None
    assert database.draft_envelope(mail.id) == envelope

    restored = compose.reopen_draft(raw, database.draft_envelope(mail.id))

    assert (restored.to, restored.cc, restored.bcc) == (
        "Ada <ada@example.com>",
        "bob@example.com",
        "hidden@example.com",
    )
    assert restored.subject == "Hi"
    assert restored.body_html.strip() == "<div>Hi <b>Ada</b></div>"
    [(filename, mime_type, content)] = [
        (part.filename, part.mime_type, part.content) for part in restored.attachments
    ]
    assert (filename, mime_type, content) == ("notes.txt", "text/plain", b"hello")


def test_a_draft_without_a_subject_reopens_with_a_blank_one(stored):
    database, account = stored
    _mail, raw, _envelope = _saved_draft(database, account.id, compose.ReopenedDraft())

    assert _headers(raw)["Subject"] == NO_SUBJECT

    restored = compose.reopen_draft(raw, database.draft_envelope(_mail.id))

    assert restored.subject == ""


def test_a_legacy_draft_without_an_envelope_reopens_without_bcc(stored):
    database, account = stored
    drafts = mail_sync.drafts_folder(database, account.id)
    msg = compose.build_mime_message(
        "ada@example.com", ["a@example.com"], [], "Hi", "<div>Hi</div>", []
    )
    mail = database.save_email(drafts.id, "a@example.com", "Hi", "Hi", "now", False)
    database.save_raw_message(mail.id, msg.as_bytes())

    assert database.draft_envelope(mail.id) is None

    restored = compose.reopen_draft(msg.as_bytes(), database.draft_envelope(mail.id))

    assert (restored.to, restored.bcc) == ("a@example.com", "")


def test_a_draft_envelope_tolerates_missing_recipient_kinds(stored):
    database, account = stored
    drafts = mail_sync.drafts_folder(database, account.id)
    mail = database.save_email(
        drafts.id,
        "a@example.com",
        "Hi",
        "Hi",
        "now",
        False,
        envelope={"to": ["a@example.com"]},
    )

    assert database.draft_envelope(mail.id) == {
        "to": ["a@example.com"],
        "cc": [],
        "bcc": [],
    }


# --- provider send paths -----------------------------------------------------


def test_graph_puts_bcc_on_the_wire_copy_only(stored, servers):
    database, account = stored
    _mail, job, raw = _queued(
        database,
        account.id,
        to="you@example.com",
        bcc="hidden@example.com",
    )

    outcome = mail_sync.send_queued(_account(protocol="graph"), CREDENTIAL, job)

    [(method, path, body)] = FakeGraphSession.requests
    assert (method, path) == ("POST", "/me/sendMail")
    outgoing = base64.b64decode(body)
    [bcc_line] = outgoing.split(b"\n", 1)[:1]
    assert bcc_line == b"Bcc: hidden@example.com"
    assert outcome.file_error is None
    assert _headers(raw)["Bcc"] is None

    sent, _changed = mail_sync.file_sent_email(
        database, _account(protocol="graph"), job, outcome, ("You", "you@example.com")
    )
    [copy] = database.emails_in_folder(sent.id)
    assert _headers(database.get_raw_message(copy.id))["Bcc"] is None


def test_a_graph_retry_with_a_sent_copy_files_without_resending(stored, servers):
    database, account = stored
    FakeGraphSession.sent_ids = ["gid-1"]
    _mail, job, _raw = _queued(
        database, account.id, to="a@example.com", bcc="hidden@example.com"
    )

    outcome = mail_sync.send_queued(_account(protocol="graph"), CREDENTIAL, job)

    assert FakeGraphSession.requests == []
    assert (outcome.filed_uid, outcome.uidvalidity) == ("gid-1", None)


# --- filing ------------------------------------------------------------------


def test_a_filed_copy_falls_back_to_the_envelope_when_to_is_empty(stored, servers):
    database, account = stored
    _mail, job, _raw = _queued(database, account.id, bcc="hidden@example.com")
    outcome = mail_sync.SendOutcome(None, None, None)

    sent, _changed = mail_sync.file_sent_email(
        database, _account(), job, outcome, ("", "")
    )

    [copy] = database.emails_in_folder(sent.id)
    assert (copy.recipient, copy.recipient_address) == (
        "hidden@example.com",
        "hidden@example.com",
    )


def test_a_filed_copy_keeps_a_to_recipient_over_the_envelope(stored, servers):
    database, account = stored
    _mail, job, _raw = _queued(
        database, account.id, to="you@example.com", bcc="hidden@example.com"
    )
    outcome = mail_sync.SendOutcome(None, None, None)

    sent, _changed = mail_sync.file_sent_email(
        database, _account(), job, outcome, ("You", "you@example.com")
    )

    [copy] = database.emails_in_folder(sent.id)
    assert (copy.recipient, copy.recipient_address) == ("You", "you@example.com")
