import json

import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import PROTOCOL_TUTA, Account
from postcard.core.net.auth import Credential
from postcard.core.net.imap_session import FLAG_SEEN
from postcard.core.net.tuta_session import MECHANISM_TUTA, TutaError
from postcard.core.store.database import Database


@pytest.fixture
def tuta(monkeypatch):
    account = Account(
        id=9,
        email="ada@tuta.com",
        display_name="Ada",
        protocol=PROTOCOL_TUTA,
        imap_host="app.tuta.com",
        imap_port=443,
        smtp_host="app.tuta.com",
        smtp_port=443,
    )
    credential = Credential(
        account.email, json.dumps({"login": account.email}), MECHANISM_TUTA
    )
    requests = []

    class Session:
        def __init__(self, credential):
            pass

        def request(self, operation, **kwargs):
            requests.append((operation, kwargs))
            if operation == "sync":
                return {
                    "folders": [
                        {
                            "id": "folders/inbox",
                            "label": "Inbox",
                            "role": "inbox",
                            "parent": "",
                        }
                    ],
                    "messages": [],
                    "folder": "folders/inbox",
                    "exists": 0,
                    "all_ids": [] if kwargs["offset"] == 0 else None,
                    "offset": kwargs["offset"],
                }
            return {}

    def forbidden(*args, **kwargs):
        pytest.fail("A Tuta operation reached an IMAP/SMTP/Graph session")

    monkeypatch.setattr(mail_sync, "TutaSession", Session)
    monkeypatch.setattr(mail_sync, "ImapSession", forbidden)
    monkeypatch.setattr(mail_sync, "SmtpSession", forbidden)
    monkeypatch.setattr(mail_sync, "GraphSession", forbidden)
    return account, credential, requests


def test_tuta_sync_routes_without_imap_and_distinguishes_full_snapshot(tuta):
    account, credential, requests = tuta
    result = mail_sync.fetch_mailbox(account, credential)
    assert result.folder == "folders/inbox"
    assert result.folders[0].role == "inbox"
    assert result.all_uids == set()
    assert mail_sync.fetch_mailbox(account, credential, offset=50).all_uids is None
    assert [op for op, _ in requests] == ["sync", "sync"]


def test_native_read_and_move_never_fall_back(tuta, monkeypatch):
    account, credential, requests = tuta
    monkeypatch.setattr(
        mail_sync.tuta_messages, "fetch_mime", lambda session, uid: b"mime"
    )
    assert (
        mail_sync.fetch_full_message(
            account, credential, "folders/inbox", "list/mail", uidvalidity=None
        )
        == b"mime"
    )
    mail_sync.set_flag(
        account,
        credential,
        "folders/inbox",
        ["list/mail"],
        FLAG_SEEN,
        True,
        uidvalidity=None,
    )
    moved = mail_sync.move_messages(
        account,
        credential,
        "folders/inbox",
        ["list/mail"],
        "folders/archive",
        uidvalidity=None,
    )
    assert moved.failed_index is None
    assert [op for op, _ in requests] == ["mark_read", "move"]


def test_send_refuses_instead_of_downgrading_encryption(tuta):
    account, credential, requests = tuta
    with pytest.raises(TutaError) as caught:
        mail_sync.send_message(
            account, credential, account.email, ["other@example.com"], b"message"
        )
    assert caught.value.code == "unsupported_operation"
    assert requests == []


def test_tuta_watcher_uses_polling_instead_of_opening_imap(tuta):
    account, credential, _ = tuta
    watcher = mail_sync._InboxWatcher(account, lambda _: credential, lambda _: None)
    watcher.stop()
    watcher._run()


def test_database_round_trips_native_protocol():
    db = Database(":memory:")
    saved = db.save_account(
        "ada@tuta.com",
        "Ada",
        "app.tuta.com",
        443,
        "app.tuta.com",
        443,
        protocol=PROTOCOL_TUTA,
    )
    assert saved.is_tuta
    assert db.accounts()[0].is_tuta
    assert not saved.is_graph
    db.close()
