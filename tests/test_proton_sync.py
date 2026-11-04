import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import PROTOCOL_PROTON, Account
from postcard.core.net.auth import Credential
from postcard.core.net.imap_session import FLAG_SEEN
from postcard.core.net.proton_session import MECHANISM_PROTON, ProtonError
from postcard.core.store.database import Database


@pytest.fixture
def proton(monkeypatch):
    account = Account(
        id=11,
        email="ada@proton.me",
        display_name="Ada",
        protocol=PROTOCOL_PROTON,
        imap_host="127.0.0.1",
        imap_port=1143,
        smtp_host="127.0.0.1",
        smtp_port=1025,
    )
    credential = Credential(account.email, "reserved", MECHANISM_PROTON)

    def forbidden(*args, **kwargs):
        pytest.fail("A Proton operation reached an IMAP/SMTP/Graph/Tuta session")

    monkeypatch.setattr(mail_sync, "ImapSession", forbidden)
    monkeypatch.setattr(mail_sync, "SmtpSession", forbidden)
    monkeypatch.setattr(mail_sync, "GraphSession", forbidden)
    monkeypatch.setattr(mail_sync, "TutaSession", forbidden)
    return account, credential


def test_every_operation_fails_closed_until_native_proton_lands(proton):
    account, credential = proton
    with pytest.raises(ProtonError):
        mail_sync.fetch_mailbox(account, credential)
    with pytest.raises(ProtonError):
        mail_sync.fetch_full_message(
            account, credential, "inbox", "message", uidvalidity=None
        )
    with pytest.raises(ProtonError):
        mail_sync.set_flag(
            account,
            credential,
            "inbox",
            ["message"],
            FLAG_SEEN,
            True,
            uidvalidity=None,
        )
    with pytest.raises(ProtonError):
        mail_sync.move_messages(
            account, credential, "inbox", ["message"], "archive", uidvalidity=None
        )
    with pytest.raises(ProtonError) as caught:
        mail_sync.send_message(
            account, credential, account.email, ["other@example.com"], b"message"
        )
    assert caught.value.code == "unsupported_operation"


def test_an_empty_flag_change_stays_a_noop(proton):
    account, credential = proton
    mail_sync.set_flag(
        account, credential, "inbox", [], FLAG_SEEN, True, uidvalidity=None
    )


def test_proton_watcher_polls_instead_of_opening_imap(proton):
    account, credential = proton
    watcher = mail_sync._InboxWatcher(account, lambda _: credential, lambda _: None)
    watcher.stop()
    watcher._run()


def test_database_round_trips_the_reserved_protocol():
    db = Database(":memory:")
    saved = db.save_account(
        "ada@proton.me",
        "Ada",
        "127.0.0.1",
        1143,
        "127.0.0.1",
        1025,
        protocol=PROTOCOL_PROTON,
    )
    assert saved.is_proton
    assert db.accounts()[0].is_proton
    db.close()
