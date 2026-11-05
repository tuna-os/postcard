import os
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import postcard.core.store.database as database_module
from postcard.core.models.email import Email
from postcard.core.models.message_header import MessageHeader
from postcard.core.store.database import (
    MIGRATIONS,
    CacheError,
    Database,
    OutboxState,
    _arrival_key,
    _fts_query,
    _sent_key,
    restrict_cache_permissions,
)

needs_sqlcipher = pytest.mark.skipif(
    database_module._sqlcipher is None, reason="sqlcipher3 is not installed"
)

_CACHE_KEY = bytes(range(32))
_OTHER_KEY = bytes(reversed(range(32)))


@pytest.fixture
def db():
    # An explicit path keeps the constructor off XDG_DATA_HOME entirely.
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def folder(db):
    account = db.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    return db.get_or_create_folder(account.id, "INBOX")


def _email(*, server_id: str | None = None, date: str = "") -> Email:
    """A bare Email, for the pure helpers that only read one field off it."""
    return Email(
        id=1,
        folder_id=1,
        server_id=server_id,
        sender="a@x",
        subject="s",
        preview="",
        date=date,
        is_unread=False,
    )


def incoming(db, folder_id, uid, subject="Lunch", **kwargs):
    kwargs.setdefault("sender", "Ada <ada@example.com>")
    kwargs.setdefault("sender_address", "")
    kwargs.setdefault("recipient", "")
    kwargs.setdefault("recipient_address", "")
    kwargs.setdefault("preview", "see you at one")
    kwargs.setdefault("date", "Jul 16")
    kwargs.setdefault("is_unread", True)
    header = MessageHeader(uid=uid, subject=subject, **kwargs)
    return db.save_incoming_email(folder_id, header)


# --- accounts ---------------------------------------------------------------


def test_save_and_list_accounts(db):
    saved = db.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    (listed,) = db.accounts()
    assert (listed.id, listed.email, listed.display_name) == (
        saved.id,
        saved.email,
        "Me",
    )
    assert (listed.imap_host, listed.imap_port) == ("imap.example.com", 993)
    assert (listed.smtp_host, listed.smtp_port) == ("smtp.example.com", 587)


def test_an_account_remembers_the_online_account_it_came_from(db):
    # goa_id is what tells credential lookup to ask GNOME Online Accounts
    # instead of the keyring, so it has to survive the round trip.
    db.save_account("a@x", "A", "imap.x", 993, "smtp.x", 587, goa_id="account_1700_0")
    typed_in = db.save_account("b@x", "B", "imap.x", 993, "smtp.x", 587)

    imported, listed_typed_in = db.accounts()
    assert imported.goa_id == "account_1700_0"
    assert (listed_typed_in.id, listed_typed_in.goa_id) == (typed_in.id, "")


def test_renaming_an_account_keeps_the_rest_of_it(db):
    saved = db.save_account("a@x", "a", "imap.x", 993, "smtp.x", 587, goa_id="g1")

    db.rename_account(saved.id, "Home Email")

    (listed,) = db.accounts()
    assert (listed.display_name, listed.email, listed.goa_id) == (
        "Home Email",
        "a@x",
        "g1",
    )


def test_an_account_remembers_the_username_it_signs_in_with(db):
    db.save_account("a@x", "A", "imap.x", 993, "smtp.x", 587, username="a.short")
    db.save_account("b@x", "B", "imap.x", 993, "smtp.x", 587)

    named, unnamed = db.accounts()
    assert (named.username, named.login_name) == ("a.short", "a.short")
    # Left blank, the address is the login -- which is what every account
    # written before the username column has, so they sign in unchanged.
    assert (unnamed.username, unnamed.login_name) == ("", "b@x")


def test_deleting_an_account_takes_its_folders_and_emails(db, folder):
    incoming(db, folder.id, "1")
    account_id = db.accounts()[0].id

    db.delete_account(account_id)

    assert db.accounts() == []
    assert db.folders_for_account(account_id) == []
    assert db.emails_in_folder(folder.id) == []


# --- folders ----------------------------------------------------------------


def test_get_or_create_folder_is_idempotent(db):
    account = db.save_account("a@x", "A", "imap.x", 993, "smtp.x", 587)
    first = db.get_or_create_folder(account.id, "Sent")
    second = db.get_or_create_folder(account.id, "Sent")
    assert first.id == second.id
    assert len(db.folders_for_account(account.id)) == 1


def test_get_folder_by_name_when_absent(db):
    account = db.save_account("a@x", "A", "imap.x", 993, "smtp.x", 587)
    assert db.get_folder_by_name(account.id, "Nope") is None


def test_prune_folders_deletes_a_whole_subtree_and_its_mail(db, folder):
    account_id = folder.account_id
    parent = db.get_or_create_folder(account_id, "Work")
    child = db.get_or_create_folder(account_id, "Work/2026")
    db.set_folder_parent(child.id, parent.id, "/")
    incoming(db, child.id, "1")

    db.prune_folders(account_id, {"INBOX"})

    assert [f.name for f in db.folders_for_account(account_id)] == ["INBOX"]
    assert db.emails_in_folder(child.id) == []


def test_prune_folders_keeps_what_the_server_still_lists(db, folder):
    account_id = folder.account_id
    db.get_or_create_folder(account_id, "Sent")
    db.get_or_create_folder(account_id, "Gone")

    db.prune_folders(account_id, {"INBOX", "Sent"})

    assert sorted(f.name for f in db.folders_for_account(account_id)) == [
        "INBOX",
        "Sent",
    ]


# --- emails -----------------------------------------------------------------


def test_save_incoming_email_reports_whether_it_was_new(db, folder):
    assert incoming(db, folder.id, "1") is True
    assert incoming(db, folder.id, "1") is False
    assert len(db.emails_in_folder(folder.id)) == 1


def test_resyncing_a_message_takes_the_servers_flags(db, folder):
    incoming(db, folder.id, "1", is_unread=True, is_starred=False)
    incoming(db, folder.id, "1", is_unread=False, is_starred=True)

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.is_unread, mail.is_starred) == (False, True)


def test_resyncing_a_message_leaves_the_rest_of_it_alone(db, folder):
    incoming(db, folder.id, "1", subject="Lunch", preview="see you at one")
    incoming(db, folder.id, "1", subject="", preview="", sender="")

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.subject, mail.preview) == ("Lunch", "see you at one")


def test_a_different_message_at_the_same_uid_replaces_the_stored_one(db, folder):
    # Otherwise the old row keeps its headers over the new message's body.
    incoming(db, folder.id, "1", subject="Hiiiii", message_id="<a@x>")
    (stale,) = db.emails_in_folder(folder.id)
    db.save_raw_message(stale.id, b"From: abhay@x\n\nold body")

    assert incoming(db, folder.id, "1", subject="New sign-in", message_id="<b@x>")

    (mail,) = db.emails_in_folder(folder.id)
    assert mail.subject == "New sign-in"
    assert db.get_raw_message(mail.id) is None


def test_a_renumbered_mailbox_drops_its_cached_mail_but_not_local_copies(db, folder):
    # Covers what the Message-ID check can't: mail with no Message-ID, and
    # cached rows the sync never re-fetches.
    assert db.set_folder_uidvalidity(folder.id, 7) is True
    incoming(db, folder.id, "1", message_id="")
    incoming(db, folder.id, "500", message_id="<old@x>")
    db.save_email(folder.id, "me@x", "Sent copy", "", "Jul 16", False)

    assert db.set_folder_uidvalidity(folder.id, 7) is False
    assert len(db.emails_in_folder(folder.id)) == 3

    assert db.set_folder_uidvalidity(folder.id, 8) is True
    assert [mail.subject for mail in db.emails_in_folder(folder.id)] == ["Sent copy"]
    assert db.folders_for_account(folder.account_id)[0].uidvalidity == 8


def test_the_first_uidvalidity_drops_mail_cached_before_it(db, folder):
    # Cached by a version that never checked, so it may predate a renumber.
    incoming(db, folder.id, "1")

    assert db.set_folder_uidvalidity(folder.id, 7) is True
    assert db.emails_in_folder(folder.id) == []


def test_a_missing_uidvalidity_drops_nothing(db, folder):
    incoming(db, folder.id, "1")

    assert db.set_folder_uidvalidity(folder.id, None) is False
    assert len(db.emails_in_folder(folder.id)) == 1


def test_resyncing_the_same_message_keeps_its_cached_body(db, folder):
    incoming(db, folder.id, "1", message_id="<a@x>")
    (mail,) = db.emails_in_folder(folder.id)
    db.save_raw_message(mail.id, b"From: ada@x\n\nbody")

    assert incoming(db, folder.id, "1", message_id="<a@x>") is False
    assert db.get_raw_message(mail.id) == b"From: ada@x\n\nbody"


def test_the_sender_address_round_trips_separately_from_the_display_name(db, folder):
    incoming(db, folder.id, "1", sender="Ada", sender_address="ada@example.com")
    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.sender, mail.sender_address) == ("Ada", "ada@example.com")


def test_mail_saved_without_a_sender_address_reads_back_as_empty(db, folder):
    # Rows predating the column, and locally-saved copies, have no address.
    incoming(db, folder.id, "1")
    (mail,) = db.emails_in_folder(folder.id)
    assert mail.sender_address == ""


def test_the_same_uid_in_another_folder_is_a_different_message(db, folder):
    other = db.get_or_create_folder(folder.account_id, "Archive")
    assert incoming(db, folder.id, "1") is True
    assert incoming(db, other.id, "1") is True


def test_locally_saved_emails_without_a_uid_are_never_deduped(db, folder):
    # SQLite treats NULLs as distinct, so the unique index doesn't collapse these.
    db.save_email(folder.id, "me@x", "Draft", "", "Jul 16", False)
    db.save_email(folder.id, "me@x", "Draft", "", "Jul 16", False)
    assert len(db.emails_in_folder(folder.id)) == 2


@pytest.mark.parametrize(
    ("active_server_uids", "local_server_id", "deleted"),
    [({"active"}, None, 1), (set(), "", 2)],
)
def test_prune_stale_emails_keeps_active_and_uid_less_rows(
    db, folder, active_server_uids, local_server_id, deleted
):
    incoming(db, folder.id, "active", subject="Active")
    incoming(db, folder.id, "stale", subject="Stale")
    db.save_email(folder.id, "me@x", "Draft", "", "Jul 16", False, local_server_id)

    assert db.prune_stale_emails(folder.id, active_server_uids) == deleted

    assert {mail.server_id for mail in db.emails_in_folder(folder.id)} == (
        active_server_uids | {local_server_id}
    )


def test_prune_stale_emails_accepts_an_authoritative_set_over_sqlite_variable_limit(
    db, folder
):
    incoming(db, folder.id, "active", subject="Active")
    incoming(db, folder.id, "stale", subject="Stale")
    active_server_uids = {f"uid-{uid}" for uid in range(250_000)} | {"active"}

    assert db.prune_stale_emails(folder.id, active_server_uids) == 1

    assert [mail.server_id for mail in db.emails_in_folder(folder.id)] == ["active"]


def test_the_recipient_of_a_synced_message_round_trips(db, folder):
    incoming(db, folder.id, "1", recipient="Bob", recipient_address="bob@example.com")

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.recipient, mail.recipient_address) == ("Bob", "bob@example.com")


def test_re_syncing_fills_in_a_recipient_the_stored_row_never_had(db, folder):
    incoming(db, folder.id, "1", recipient="", recipient_address="")

    incoming(db, folder.id, "1", recipient="Bob", recipient_address="bob@example.com")

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.recipient, mail.recipient_address) == ("Bob", "bob@example.com")


def test_a_locally_saved_message_keeps_its_recipient(db, folder):
    db.save_email(
        folder.id,
        sender="me@example.com",
        recipient="Bob",
        recipient_address="bob@example.com",
        subject="Lunch",
        preview="",
        date="",
        is_unread=False,
    )

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.recipient, mail.recipient_address) == ("Bob", "bob@example.com")


def test_the_python_flags_map_onto_the_original_column_names(db, folder):
    # The Email/MessageHeader fields are is_unread/is_starred but the columns
    # are still `unread`/`starred`, so _email_from_row bridges the two. A rename
    # leaking into the SQL would only show up at runtime.
    incoming(db, folder.id, "1", is_unread=True, is_starred=True)

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.is_unread, mail.is_starred) == (True, True)

    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(emails)")}
    assert {"unread", "starred"} <= columns
    assert not {"is_unread", "is_starred"} & columns


def test_read_and_unread(db, folder):
    incoming(db, folder.id, "1", is_unread=True)
    (email,) = db.emails_in_folder(folder.id)
    assert db.unread_count_in_folder(folder.id) == 1

    db.set_email_unread(email.id, False)
    assert db.unread_count_in_folder(folder.id) == 0

    db.set_email_unread(email.id, True)
    assert db.unread_count_in_folder(folder.id) == 1


def test_starring(db, folder):
    incoming(db, folder.id, "1")
    (email,) = db.emails_in_folder(folder.id)
    assert email.is_starred is False

    db.set_email_starred(email.id, True)
    assert db.emails_in_folder(folder.id)[0].is_starred is True


def test_moving_an_email_between_folders(db, folder):
    trash = db.get_or_create_folder(folder.account_id, "Trash")
    incoming(db, folder.id, "1")
    (email,) = db.emails_in_folder(folder.id)

    db.move_emails([email.id], trash.id)

    assert db.emails_in_folder(folder.id) == []
    assert [e.id for e in db.emails_in_folder(trash.id)] == [email.id]


# Undo and commit are the same reconciliation pointed at different mailboxes, so
# both halves of it are pinned here: the undo path and the commit path.


def test_undoing_a_move_puts_the_email_back_with_its_original_uid(db, folder):
    trash = db.get_or_create_folder(folder.account_id, "Trash")
    incoming(db, folder.id, "7")
    (email,) = db.emails_in_folder(folder.id)
    db.move_emails([email.id], trash.id)

    db.reconcile_moved_emails([(email.id, folder.id, "7")])

    (restored,) = db.emails_in_folder(folder.id)
    assert (restored.id, restored.server_id) == (email.id, "7")
    assert db.emails_in_folder(trash.id) == []


def test_undoing_a_move_drops_the_placeholder_when_a_sync_beat_it_back(db, folder):
    trash = db.get_or_create_folder(folder.account_id, "Trash")
    incoming(db, folder.id, "7")
    (email,) = db.emails_in_folder(folder.id)
    db.move_emails([email.id], trash.id)
    # A sync reinserted the source UID while the move was pending.
    incoming(db, folder.id, "7")

    db.reconcile_moved_emails([(email.id, folder.id, "7")])

    (kept,) = db.emails_in_folder(folder.id)
    assert kept.id != email.id  # the authoritative row survived, not ours
    assert db.emails_in_folder(trash.id) == []


def test_a_completed_move_takes_the_destination_uid(db, folder):
    trash = db.get_or_create_folder(folder.account_id, "Trash")
    incoming(db, folder.id, "7")
    (email,) = db.emails_in_folder(folder.id)
    db.move_emails([email.id], trash.id)

    db.reconcile_moved_emails([(email.id, trash.id, "42")])

    (moved,) = db.emails_in_folder(trash.id)
    assert (moved.id, moved.server_id) == (email.id, "42")


def test_a_completed_move_without_a_uid_drops_the_placeholder(db, folder):
    trash = db.get_or_create_folder(folder.account_id, "Trash")
    incoming(db, folder.id, "7")
    (email,) = db.emails_in_folder(folder.id)
    db.move_emails([email.id], trash.id)

    db.reconcile_moved_emails([(email.id, trash.id, None)])

    assert db.emails_in_folder(trash.id) == []
    assert db.emails_in_folder(folder.id) == []


def test_the_servers_copy_of_a_sent_message_replaces_the_local_one(db, folder):
    # The local copy has no UID, so left behind it would sort as newest forever.
    local = db.save_email(folder.id, "me", "Hi", "Hi", "2026-10-23", False)
    db.save_raw_message(local.id, b"Message-ID: <a@x>\r\nSubject: Hi\r\n\r\nbody")

    incoming(db, folder.id, "89", subject="Hi", message_id="<a@x>")

    (kept,) = db.emails_in_folder(folder.id)
    assert kept.server_id == "89"


def test_deleting_an_email(db, folder):
    incoming(db, folder.id, "1")
    (email,) = db.emails_in_folder(folder.id)
    db.delete_email(email.id)
    assert db.emails_in_folder(folder.id) == []


def test_raw_message_round_trip(db, folder):
    incoming(db, folder.id, "1")
    (email,) = db.emails_in_folder(folder.id)
    assert db.get_raw_message(email.id) is None

    db.save_raw_message(email.id, b"From: a@x\n\nbody")
    assert db.get_raw_message(email.id) == b"From: a@x\n\nbody"


# --- ordering & threading ---------------------------------------------------


def test_arrival_key_uses_the_imap_uid():
    assert _arrival_key(_email(server_id="42")) == 42


def test_arrival_key_sorts_uid_less_messages_newest():
    # A Sent copy saved locally has no UID until the next sync confirms it.
    # IMAP UIDs are 32-bit unsigned, so the largest one must still sort older.
    largest_uid = _arrival_key(_email(server_id=str(2**32 - 1)))
    assert _arrival_key(_email(server_id=None)) > largest_uid
    assert _arrival_key(_email(server_id="")) > largest_uid


def test_a_zone_less_date_is_read_as_local_time():
    # "-0000" means "zone unknown", and parses naive. format_date shows it
    # as local time, so the ordering has to agree with the label.
    local = datetime(2026, 8, 1, 9, 0).astimezone().timestamp()
    assert _sent_key(_email(date="2026-08-01T09:00:00")) == local


def test_conversations_are_newest_first_by_uid_not_by_local_id(db, folder):
    # Load-on-scroll backfill gives *older* mail a *newer* local id.
    incoming(db, folder.id, "9", subject="Newer")
    incoming(db, folder.id, "2", subject="Older")

    assert [c.subject for c in db.conversations_in_folders([folder.id])] == [
        "Newer",
        "Older",
    ]


def test_conversations_from_several_folders_are_ordered_by_date_not_uid(db, folder):
    # The second account's inbox is at UID 3 while the first is past 1000, so
    # UID order would put the older message first.
    other = db.save_account("b@x", "B", "imap.x", 993, "smtp.x", 587)
    other_inbox = db.get_or_create_folder(other.id, "INBOX")
    incoming(db, folder.id, "1001", subject="Older", date="2026-08-01T09:00:00+00:00")
    incoming(db, other_inbox.id, "3", subject="Newer", date="2026-08-02T09:00:00+00:00")

    merged = db.conversations_in_folders([folder.id, other_inbox.id])

    assert [c.subject for c in merged] == ["Newer", "Older"]


def test_an_unreadable_date_sorts_last(db, folder):
    # The Date header is the sender's to write, so an unreadable one must not
    # buy a permanent place at the top of the merged list.
    other = db.save_account("b@x", "B", "imap.x", 993, "smtp.x", 587)
    other_inbox = db.get_or_create_folder(other.id, "INBOX")
    incoming(db, folder.id, "1", subject="Dated", date="2026-08-01T09:00:00+00:00")
    incoming(db, other_inbox.id, "1", subject="Undated", date="not a date")

    merged = db.conversations_in_folders([folder.id, other_inbox.id])

    assert [c.subject for c in merged] == ["Dated", "Undated"]


def test_both_loaders_return_nothing_for_no_folders(db, folder):
    incoming(db, folder.id, "1")

    assert db.conversations_in_folders([]) == []
    assert db.search_conversations([], "lunch") == []


def test_reassign_conversations_groups_a_reply_with_its_parent(db, folder):
    incoming(db, folder.id, "1", subject="Lunch", message_id="<a>")
    incoming(
        db, folder.id, "2", subject="Re: Lunch", message_id="<b>", in_reply_to="<a>"
    )

    db.reassign_conversations(folder.id)

    (conversation,) = db.conversations_in_folders([folder.id])
    assert conversation.count == 2
    # Sorted oldest first, so .latest is genuinely the newest message.
    assert conversation.latest.subject == "Re: Lunch"


def test_unthreaded_emails_stay_separate_conversations(db, folder):
    incoming(db, folder.id, "1", subject="Lunch", message_id="<a>")
    incoming(db, folder.id, "2", subject="Invoice", message_id="<b>")

    db.reassign_conversations(folder.id)

    assert len(db.conversations_in_folders([folder.id])) == 2


# --- search -----------------------------------------------------------------


def test_fts_query_prefix_matches_every_word():
    assert _fts_query("hello world") == '"hello"* "world"*'


def test_fts_query_doubles_embedded_quotes():
    assert _fts_query('say "hi"') == '"say"* """hi"""*'


def test_fts_query_of_blank_text():
    assert _fts_query("   ") == ""


def test_search_matches_a_subject_prefix(db, folder):
    incoming(db, folder.id, "1", subject="Lunch plans")
    incoming(db, folder.id, "2", subject="Invoice")

    results = db.search_conversations([folder.id], "lun")

    assert [c.subject for c in results] == ["Lunch plans"]


def test_search_requires_every_word_to_match(db, folder):
    incoming(db, folder.id, "1", subject="Lunch plans")

    assert db.search_conversations([folder.id], "lunch plans") != []
    assert db.search_conversations([folder.id], "lunch invoice") == []


def test_search_matches_the_sender_and_preview(db, folder):
    incoming(db, folder.id, "1", subject="Lunch")
    assert db.search_conversations([folder.id], "ada") != []
    assert db.search_conversations([folder.id], "one") != []


def test_an_empty_search_returns_everything(db, folder):
    incoming(db, folder.id, "1")
    incoming(db, folder.id, "2", subject="Invoice")

    assert len(db.search_conversations([folder.id], "")) == 2
    assert len(db.search_conversations([folder.id], "   ")) == 2


def test_a_quoted_search_term_does_not_break_fts(db, folder):
    incoming(db, folder.id, "1", subject="Lunch")
    assert db.search_conversations([folder.id], 'say "hi"') == []


def test_search_across_folders_matches_only_inside_them(db, folder):
    work = db.get_or_create_folder(folder.account_id, "Work")
    archive = db.get_or_create_folder(folder.account_id, "Archive")
    incoming(db, folder.id, "1", subject="Lunch plans")
    incoming(db, work.id, "1", subject="Lunch menu")
    incoming(db, archive.id, "1", subject="Lunch elsewhere")

    results = db.search_conversations([folder.id, work.id], "lunch")

    assert sorted(c.subject for c in results) == ["Lunch menu", "Lunch plans"]


def test_matching_one_email_returns_its_whole_conversation(db, folder):
    incoming(db, folder.id, "1", subject="Lunch", message_id="<a>")
    incoming(
        db,
        folder.id,
        "2",
        subject="Re: Lunch",
        message_id="<b>",
        in_reply_to="<a>",
        preview="bringing dessert",
    )
    db.reassign_conversations(folder.id)

    (conversation,) = db.search_conversations([folder.id], "dessert")

    assert conversation.count == 2


# --- contacts ---------------------------------------------------------------


def test_save_contacts_lowercases_and_formats(db):
    db.save_contacts([("Ada L", "Ada@Example.COM"), ("", "bob@example.com")])
    assert db.contact_addresses() == ["bob@example.com", "Ada L <ada@example.com>"]


def test_a_later_anonymous_sighting_never_wipes_a_known_name(db):
    db.save_contacts([("Ada L", "ada@example.com")])
    db.save_contacts([("", "ada@example.com")])
    assert db.contact_addresses() == ["Ada L <ada@example.com>"]


def test_a_later_sighting_fills_in_a_missing_name(db):
    db.save_contacts([("", "ada@example.com")])
    db.save_contacts([("Ada L", "ada@example.com")])
    assert db.contact_addresses() == ["Ada L <ada@example.com>"]


def test_save_contacts_skips_entries_with_no_address(db):
    db.save_contacts([("Nobody", "")])
    assert db.contact_addresses() == []


# --- Microsoft Graph accounts -------------------------------------------------


def test_an_account_remembers_its_protocol(db):
    db.save_account(
        "a@x",
        "A",
        "graph.microsoft.com",
        443,
        "graph.microsoft.com",
        443,
        goa_id="account_2",
        protocol="graph",
    )
    db.save_account("b@x", "B", "imap.x", 993, "smtp.x", 587)

    graph, imap = db.accounts()

    assert (graph.protocol, graph.is_graph) == ("graph", True)
    assert (imap.protocol, imap.is_graph) == ("imap", False)


def test_a_folder_keeps_the_role_and_label_its_server_stated(db, folder):
    db.set_folder_identity(folder.id, "sent", "Gesendete Elemente")

    [stored] = db.folders_for_account(folder.account_id)

    assert (stored.role, stored.label) == ("sent", "Gesendete Elemente")


def test_a_new_folder_infers_its_role_and_label(folder):
    assert (folder.role, folder.label) == ("", "")


def test_arrival_key_orders_opaque_graph_ids_by_date():
    older = _email(server_id="AAMkA=", date="2026-09-01T08:00:00+00:00")
    newer = _email(server_id="AAMkB=", date="2026-09-02T08:00:00+00:00")

    assert _arrival_key(older) < _arrival_key(newer)


# --- cache permissions ------------------------------------------------------


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@needs_sqlcipher
def test_opening_a_database_leaves_it_owner_only(tmp_path):
    path = str(tmp_path / "postcard.db")
    os.chmod(tmp_path, 0o755)

    database = Database(path, key=_CACHE_KEY)
    database.close()

    assert mode_of(tmp_path) == 0o700
    assert mode_of(path) == 0o600


@needs_sqlcipher
def test_a_pre_existing_world_readable_cache_is_tightened(tmp_path):
    path = str(tmp_path / "postcard.db")
    Database(path, key=_CACHE_KEY).close()
    os.chmod(tmp_path, 0o755)
    os.chmod(path, 0o644)

    Database(path, key=_CACHE_KEY).close()

    assert mode_of(tmp_path) == 0o700
    assert mode_of(path) == 0o600


def test_sqlite_sidecars_are_tightened_too(tmp_path):
    path = tmp_path / "postcard.db"
    path.write_bytes(b"")
    for suffix in ("-wal", "-shm", "-journal"):
        (tmp_path / f"postcard.db{suffix}").write_bytes(b"")
    os.chmod(tmp_path, 0o755)
    os.chmod(path, 0o644)
    for suffix in ("-wal", "-shm", "-journal"):
        os.chmod(tmp_path / f"postcard.db{suffix}", 0o644)

    restrict_cache_permissions(str(path))

    assert mode_of(tmp_path) == 0o700
    assert mode_of(path) == 0o600
    for suffix in ("-wal", "-shm", "-journal"):
        assert mode_of(tmp_path / f"postcard.db{suffix}") == 0o600


def test_missing_sidecars_are_normal_not_an_error(tmp_path, caplog):
    restrict_cache_permissions(str(tmp_path / "postcard.db"))

    assert "could not restrict" not in caplog.text


def test_restriction_failures_warn_without_failing(tmp_path, caplog, monkeypatch):
    def _deny(path, mode):
        raise OSError("denied")

    monkeypatch.setattr(os, "chmod", _deny)
    restrict_cache_permissions(str(tmp_path / "postcard.db"))

    assert caplog.text.count("could not restrict") == 5


# --- encryption at rest -----------------------------------------------------


def test_a_file_database_without_a_key_fails_closed(tmp_path):
    with pytest.raises(CacheError) as caught:
        Database(str(tmp_path / "postcard.db"))
    assert caught.value.code == "key_missing"


def test_a_missing_binding_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, "_sqlcipher", None)
    with pytest.raises(CacheError) as caught:
        Database(str(tmp_path / "postcard.db"), key=_CACHE_KEY)
    assert caught.value.code == "crypto_unavailable"


@needs_sqlcipher
def test_a_new_file_database_is_encrypted_at_rest(tmp_path):
    path = tmp_path / "postcard.db"
    database = Database(str(path), key=_CACHE_KEY)
    database.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    database.close()

    stored = path.read_bytes()
    assert not stored.startswith(b"SQLite format 3\x00")
    assert b"me@example.com" not in stored

    reopened = Database(str(path), key=_CACHE_KEY)
    assert reopened.accounts()[0].email == "me@example.com"
    reopened.close()


@needs_sqlcipher
def test_a_wrong_key_opens_nothing(tmp_path):
    path = str(tmp_path / "postcard.db")
    Database(path, key=_CACHE_KEY).close()

    with pytest.raises(CacheError) as caught:
        Database(path, key=_OTHER_KEY)
    assert caught.value.code == "unlock_failed"


@needs_sqlcipher
def test_a_cache_without_its_key_opens_nothing(tmp_path):
    path = str(tmp_path / "postcard.db")
    Database(path, key=_CACHE_KEY).close()

    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(path).execute("SELECT count(*) FROM sqlite_master").fetchone()


@needs_sqlcipher
def test_a_tampered_page_fails_closed(tmp_path):
    path = tmp_path / "postcard.db"
    Database(str(path), key=_CACHE_KEY).close()

    raw = bytearray(path.read_bytes())
    raw[100] ^= 0xFF
    path.write_bytes(bytes(raw))

    with pytest.raises(CacheError) as caught:
        Database(str(path), key=_CACHE_KEY)
    assert caught.value.code == "unlock_failed"


@needs_sqlcipher
def test_every_open_pins_the_cipher_suite(tmp_path):
    path = str(tmp_path / "postcard.db")
    for _ in range(2):
        database = Database(path, key=_CACHE_KEY)
        try:
            assert database._conn.execute("PRAGMA kdf_iter").fetchone()[0] == "256000"
            assert (
                database._conn.execute("PRAGMA cipher_kdf_algorithm").fetchone()[0]
                == "PBKDF2_HMAC_SHA512"
            )
            assert (
                database._conn.execute("PRAGMA cipher_hmac_algorithm").fetchone()[0]
                == "HMAC_SHA512"
            )
            assert (
                database._conn.execute("PRAGMA cipher_memory_security").fetchone()[0]
                == "1"
            )
        finally:
            database.close()


def test_a_drifted_cipher_suite_fails_closed():
    answers = {
        "kdf_iter": "1000",
        "cipher_kdf_algorithm": database_module._KDF_ALGORITHM,
        "cipher_hmac_algorithm": database_module._HMAC_ALGORITHM,
        "cipher_memory_security": "1",
    }

    class _Cursor:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return (self._value,)

    class _Conn:
        def execute(self, sql):
            return _Cursor(answers[sql.removeprefix("PRAGMA ").strip()])

    with pytest.raises(CacheError) as caught:
        database_module._assert_cipher_suite(_Conn())
    assert caught.value.code == "unlock_failed"


@needs_sqlcipher
def test_a_cache_from_another_suite_opens_nothing(tmp_path):
    path = tmp_path / "postcard.db"
    conn = database_module._sqlcipher.connect(str(path))
    conn.execute(f"PRAGMA key = \"x'{_CACHE_KEY.hex()}'\"")
    conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA256")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()

    with pytest.raises(CacheError) as caught:
        Database(str(path), key=_CACHE_KEY)
    assert caught.value.code == "unlock_failed"


@needs_sqlcipher
def test_no_plaintext_lingers_in_the_file_or_its_sidecars(tmp_path):
    path = tmp_path / "postcard.db"
    marker = "plaintext-probe-7f3a9c"
    database = Database(str(path), key=_CACHE_KEY)
    account = database.save_account(
        f"{marker}@example.com",
        "Me",
        "imap.example.com",
        993,
        "smtp.example.com",
        587,
    )
    folder = database.get_or_create_folder(account.id, "INBOX")
    incoming(
        database,
        folder.id,
        "1",
        subject=f"subject {marker}",
        preview=f"preview {marker}",
    )
    # The FTS index holds the marker too, and still answers while encrypted.
    assert database.search_conversations([folder.id], marker)

    def _sidecar_bytes():
        blobs = []
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = tmp_path / f"postcard.db{suffix}"
            if sidecar.exists():
                blobs.append(sidecar.read_bytes())
        return blobs

    assert all(marker.encode() not in blob for blob in _sidecar_bytes())
    database._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.close()

    stored = path.read_bytes()
    assert not stored.startswith(b"SQLite format 3\x00")
    assert marker.encode() not in stored
    assert all(marker.encode() not in blob for blob in _sidecar_bytes())


@needs_sqlcipher
def test_a_plaintext_cache_migrates_in_place(tmp_path):
    path = tmp_path / "postcard.db"
    plain = sqlite3.connect(str(path))
    plain.execute("CREATE TABLE t(x)")
    plain.execute("INSERT INTO t VALUES ('secret')")
    plain.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
    plain.commit()
    plain.close()

    database = Database(str(path), key=_CACHE_KEY)

    assert not path.read_bytes().startswith(b"SQLite format 3\x00")
    assert b"secret" not in path.read_bytes()
    assert database._conn.execute("PRAGMA user_version").fetchone()[0] == len(
        MIGRATIONS
    )
    assert [row[0] for row in database._conn.execute("SELECT x FROM t")] == ["secret"]
    database.close()

    assert not (tmp_path / "postcard.db.migrating").exists()
    reopened = Database(str(path), key=_CACHE_KEY)
    reopened.close()
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(path)).execute("SELECT * FROM t").fetchall()


@needs_sqlcipher
def test_migration_leaves_a_broken_cache_untouched(tmp_path):
    path = tmp_path / "postcard.db"
    path.write_bytes(b"SQLite format 3\x00" + bytes(100))

    with pytest.raises(CacheError) as caught:
        Database(str(path), key=_CACHE_KEY)

    assert caught.value.code == "migration_failed"
    assert path.read_bytes() == b"SQLite format 3\x00" + bytes(100)
    assert not (tmp_path / "postcard.db.migrating").exists()


def test_a_missing_binding_is_detected_at_import():
    # The fallback only runs at import time, so a fresh interpreter with the
    # binding blocked exercises it without disturbing this process.
    root = Path(__file__).resolve().parents[3]
    probe = (
        "import sys; sys.modules['sqlcipher3'] = None;"
        " import postcard.core.store.database as d;"
        " assert d._sqlcipher is None"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_migration_without_the_binding_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, "_sqlcipher", None)
    with pytest.raises(CacheError) as caught:
        database_module._migrate_plaintext_to_encrypted(
            str(tmp_path / "postcard.db"), _CACHE_KEY
        )
    assert caught.value.code == "crypto_unavailable"


@needs_sqlcipher
def test_a_failed_verification_reraises_without_wrapping(tmp_path, monkeypatch):
    path = tmp_path / "postcard.db"
    plain = sqlite3.connect(str(path))
    plain.execute("CREATE TABLE t(x)")
    plain.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
    plain.commit()
    plain.close()
    before = path.read_bytes()

    def _broken(path, key, version):
        raise CacheError("migration_failed")

    monkeypatch.setattr(database_module, "_verify_encrypted", _broken)
    with pytest.raises(CacheError) as caught:
        database_module._migrate_plaintext_to_encrypted(str(path), _CACHE_KEY)

    assert caught.value.code == "migration_failed"
    assert path.read_bytes() == before
    assert not (tmp_path / "postcard.db.migrating").exists()


@needs_sqlcipher
def test_verification_rejects_a_version_mismatch(tmp_path):
    path = tmp_path / "postcard.db"
    conn = database_module._sqlcipher.connect(str(path))
    conn.execute(f"PRAGMA key = \"x'{_CACHE_KEY.hex()}'\"")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    with pytest.raises(CacheError) as caught:
        database_module._verify_encrypted(str(path), _CACHE_KEY, 7)
    assert caught.value.code == "migration_failed"


@needs_sqlcipher
def test_default_path_builds_under_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        database_module.GLib, "get_user_data_dir", lambda: str(tmp_path)
    )
    database = Database(key=_CACHE_KEY)
    database.close()

    assert (tmp_path / "postcard" / "postcard.db").exists()


# --- atomic schema upgrades -------------------------------------------------


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _attach_memory_db() -> Database:
    """A Database on a caller-managed :memory: connection.

    The constructor always builds a fresh database, so tests that start from
    a historical schema attach the connection themselves instead.
    """
    database = Database.__new__(Database)
    database._conn = sqlite3.connect(":memory:")
    database._conn.row_factory = sqlite3.Row
    return database


def _open_plaintext_file(path) -> Database:
    """Open a plaintext file database without the crypto binding.

    File persistence is what proves a failed upgrade reopens; routing every
    such test through SQLCipher would skip it wherever the binding is missing.
    Migrations only use the shared DB-API surface, so this runs the same
    statements the encrypted open runs.
    """
    database = Database.__new__(Database)
    database._conn = sqlite3.connect(str(path))
    database._conn.row_factory = sqlite3.Row
    database._conn.execute("PRAGMA journal_mode = WAL")
    database._conn.execute("PRAGMA synchronous = NORMAL")
    database._conn.execute("PRAGMA foreign_keys = ON")
    return database


def _load_fixture(conn, name: str) -> None:
    conn.executescript((_FIXTURES_DIR / name).read_text())
    conn.commit()


def _schema_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _column_names(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _normalize_ddl(sql: str) -> str:
    """Collapse formatting SQLite itself does not preserve.

    ALTER TABLE rewrites the stored CREATE TABLE with its own punctuation
    spacing, so a hand-written historical schema can only match modulo that.
    """
    collapsed = re.sub(r"\s+", " ", sql)
    return collapsed.replace(" ,", ",").replace("( ", "(").replace(" )", ")")


def _schema_signature(conn) -> list:
    """Every table's columns plus every schema object's normalized DDL."""
    signature = []
    for table in ("accounts", "folders", "emails", "contacts"):
        columns = [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")]
        signature.append((table, columns))
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master"
        " WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    signature.append(
        ("objects", [(kind, name, _normalize_ddl(sql)) for kind, name, sql in objects])
    )
    return signature


class _FlakyConnection:
    """Wraps a connection, failing the statements a predicate matches."""

    def __init__(self, inner, fail_when) -> None:
        self._inner = inner
        self._fail_when = fail_when

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and self._fail_when(sql):
            raise sqlite3.OperationalError("injected fault")
        return self._inner.execute(sql, *args, **kwargs)


def test_upgrading_the_oldest_schema_preserves_everything_it_holds():
    database = _attach_memory_db()
    _load_fixture(database._conn, "mail_v0.sql")

    database._migrate_schema()

    assert _schema_version(database._conn) == len(MIGRATIONS)
    assert [(a.email, a.display_name) for a in database.accounts()] == [
        ("ada@example.com", "Ada"),
        ("grace@work.example", "Grace"),
    ]
    folders = {f.name: f for f in database.folders_for_account(1)}
    assert sorted(folders) == ["Drafts", "INBOX", "Outbox", "Sent"]
    inbox = database.emails_in_folder(folders["INBOX"].id)
    assert sorted(m.subject for m in inbox) == [
        "Invoice 42",
        "Lunch plans",
        "Re: Lunch plans",
    ]
    flags = {m.subject: (m.is_unread, m.is_starred) for m in inbox}
    assert flags["Re: Lunch plans"] == (True, True)
    assert flags["Invoice 42"] == (False, False)
    # Migration 3 backfilled addresses from contacts where it could.
    assert {m.subject: m.sender_address for m in inbox} == {
        "Lunch plans": "ada@example.com",
        "Re: Lunch plans": "bob@example.com",
        "Invoice 42": "",
    }
    assert {m.subject: m.conversation_id for m in inbox} == {
        "Lunch plans": 1,
        "Re: Lunch plans": 1,
        "Invoice 42": None,
    }
    # New columns arrived with the defaults old rows were already living with.
    (account,) = [a for a in database.accounts() if a.email == "ada@example.com"]
    assert (account.username, account.goa_id, account.protocol) == ("", "", "imap")
    stored = folders["INBOX"]
    assert (stored.parent_id, stored.delimiter) == (None, "/")
    assert (stored.role, stored.label, stored.uidvalidity) == ("", "", None)
    assert "Ada Lovelace <ada@example.com>" in database.contact_addresses()
    # The Outbox queue and drafts have no server id, and keep their bodies.
    queued = database.emails_in_folder(folders["Outbox"].id)
    assert {m.server_id for m in queued} == {None}
    bodies = {m.subject: database.get_raw_message(m.id) for m in queued}
    assert bodies == {
        "Queued: trip report": b"Queued trip report body awaiting send",
        "Queued: no body yet": None,
    }
    (draft,) = database.emails_in_folder(folders["Drafts"].id)
    assert (draft.server_id, database.get_raw_message(draft.id)) == (
        None,
        b"Draft hello body",
    )
    # Rows that predated the index are searchable again (migration 6).
    (thread,) = database.search_conversations([folders["INBOX"].id], "dessert")
    assert (thread.subject, thread.count) == ("Re: Lunch plans", 2)
    assert len(database.search_conversations([folders["INBOX"].id], "invoice")) == 1
    database.close()


def test_upgrading_a_mid_history_schema_keeps_what_earlier_migrations_built():
    database = _attach_memory_db()
    _load_fixture(database._conn, "mail_v5.sql")

    database._migrate_schema()

    assert _schema_version(database._conn) == len(MIGRATIONS)
    folders = {f.name: f for f in database.folders_for_account(1)}
    assert folders["Work.Projects"].parent_id == folders["Work"].id
    assert folders["Work.Projects"].delimiter == "."
    accounts = {a.email: a for a in database.accounts()}
    assert accounts["grace@work.example"].goa_id == "account_1700_0"
    assert accounts["ada@example.com"].protocol == "imap"
    (nested,) = database.emails_in_folder(folders["Work.Projects"].id)
    assert (nested.subject, nested.sender_address) == (
        "Project notes",
        "bob@example.com",
    )
    (reply,) = [
        m
        for m in database.emails_in_folder(folders["INBOX"].id)
        if m.subject == "Re: Lunch plans"
    ]
    assert (reply.recipient, reply.recipient_address) == (
        "Ada Lovelace",
        "ada@example.com",
    )
    (queued,) = database.emails_in_folder(folders["Outbox"].id)
    assert database.get_raw_message(queued.id) == (
        b"Queued trip report body awaiting send"
    )
    (thread,) = database.search_conversations([folders["INBOX"].id], "lunch")
    assert thread.count == 2
    database.close()


@pytest.mark.parametrize("fixture", ["mail_v0.sql", "mail_v5.sql"])
def test_an_upgraded_fixture_matches_a_fresh_database_schema(fixture):
    # Upgrading the oldest database in the wild must yield exactly today's
    # schema -- any drift means a migration or a fixture stopped matching.
    fresh = Database(":memory:")
    upgraded = _attach_memory_db()
    try:
        _load_fixture(upgraded._conn, fixture)
        upgraded._migrate_schema()
        assert _schema_signature(upgraded._conn) == _schema_signature(fresh._conn)
    finally:
        fresh.close()
        upgraded.close()


def test_split_statements_keeps_quoted_semicolons_together():
    assert database_module._split_statements(
        "INSERT INTO t VALUES ('a;b');\nUPDATE t SET x = 'it''s';"
    ) == ["INSERT INTO t VALUES ('a;b');", "UPDATE t SET x = 'it''s';"]


def test_split_statements_tolerates_comments_and_a_missing_trailer():
    assert database_module._split_statements(
        "-- leading comment\nALTER TABLE t ADD COLUMN x TEXT;\n-- trailing\n"
    ) == ["-- leading comment\nALTER TABLE t ADD COLUMN x TEXT;"]
    assert database_module._split_statements("ALTER TABLE t ADD COLUMN x TEXT") == [
        "ALTER TABLE t ADD COLUMN x TEXT"
    ]


def test_every_migration_splits_into_single_statements():
    for sql in MIGRATIONS:
        statements = database_module._split_statements(sql)
        assert statements
        for statement in statements:
            assert database_module._split_statements(statement) == [statement]


def test_a_failed_migration_rolls_back_to_the_previous_version(caplog):
    database = _attach_memory_db()
    _load_fixture(database._conn, "mail_v0.sql")
    inner = database._conn
    database._conn = _FlakyConnection(
        inner, lambda sql: sql.startswith("UPDATE emails SET sender_address")
    )

    with pytest.raises(CacheError) as caught:
        database._migrate_schema()

    assert caught.value.code == "schema_upgrade_failed"
    assert str(caught.value) == "The mail cache could not be opened."
    assert "could not apply schema migration 3" in caplog.text
    database._conn = inner
    assert _schema_version(inner) == 2
    assert "sender_address" not in _column_names(inner, "emails")
    assert inner.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 7

    database._migrate_schema()
    assert _schema_version(inner) == len(MIGRATIONS)
    assert len(database.emails_in_folder(1)) == 3
    database.close()


def test_a_lost_version_write_rolls_back_the_schema_change():
    # The reported failure: the schema committed, then the separate version
    # bump failed, and every later open retried onto the half-applied schema.
    database = _attach_memory_db()
    _load_fixture(database._conn, "mail_v0.sql")
    inner = database._conn
    database._conn = _FlakyConnection(
        inner, lambda sql: sql.startswith("PRAGMA user_version =")
    )

    with pytest.raises(CacheError) as caught:
        database._migrate_schema()

    assert caught.value.code == "schema_upgrade_failed"
    database._conn = inner
    assert _schema_version(inner) == 0
    assert "parent_id" not in _column_names(inner, "folders")

    database._migrate_schema()
    assert _schema_version(inner) == len(MIGRATIONS)
    assert len(database.accounts()) == 2
    database.close()


def test_a_failed_upgrade_reopens_from_the_last_complete_migration(tmp_path):
    path = tmp_path / "postcard.db"
    setup = _open_plaintext_file(path)
    _load_fixture(setup._conn, "mail_v0.sql")
    setup.close()

    failing = _open_plaintext_file(path)
    inner = failing._conn
    failing._conn = _FlakyConnection(
        inner, lambda sql: sql.startswith("ALTER TABLE accounts ADD COLUMN goa_id")
    )
    with pytest.raises(CacheError):
        failing._migrate_schema()
    failing._conn = inner
    failing.close()

    resumed = _open_plaintext_file(path)
    assert _schema_version(resumed._conn) == 4
    assert "goa_id" not in _column_names(resumed._conn, "accounts")
    assert resumed._conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 7

    resumed._migrate_schema()
    assert _schema_version(resumed._conn) == len(MIGRATIONS)
    assert [a.email for a in resumed.accounts()] == [
        "ada@example.com",
        "grace@work.example",
    ]
    (thread,) = resumed.search_conversations([1], "dessert")
    assert thread.count == 2
    resumed.close()


def test_a_crash_mid_migration_reopens_on_the_previous_version(tmp_path):
    path = tmp_path / "postcard.db"
    setup = _open_plaintext_file(path)
    _load_fixture(setup._conn, "mail_v0.sql")
    setup.close()

    # A crash anywhere inside the migration's transaction -- here, right after
    # its first statement -- can only lose that uncommitted migration.
    root = Path(__file__).resolve().parents[3]
    crasher = (
        "import os, sqlite3, sys;"
        " sys.path.insert(0, 'src');"
        " from postcard.core.store.database import MIGRATIONS, _split_statements;"
        " conn = sqlite3.connect(sys.argv[1]);"
        " conn.execute('BEGIN IMMEDIATE');"
        " conn.execute(_split_statements(MIGRATIONS[0])[0]);"
        " os._exit(1)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", crasher, str(path)],
        cwd=root,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1

    reopened = _open_plaintext_file(path)
    assert _schema_version(reopened._conn) == 0
    assert "parent_id" not in _column_names(reopened._conn, "folders")
    assert reopened._conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 7

    reopened._migrate_schema()
    assert _schema_version(reopened._conn) == len(MIGRATIONS)
    assert len(reopened.accounts()) == 2
    reopened.close()


def test_a_newer_schema_is_refused_and_left_untouched(tmp_path):
    path = tmp_path / "postcard.db"
    database = _open_plaintext_file(path)
    database._create_tables()
    database._migrate_schema()
    account = database.save_account("a@x", "A", "imap.x", 993, "smtp.x", 587)
    folder = database.get_or_create_folder(account.id, "INBOX")
    mail = database.save_email(folder.id, "Ada", "Hi", "", "2026-01-01", True)
    database.save_raw_message(mail.id, b"From: ada@x stating hi")
    database._conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    database._conn.commit()
    database.close()

    refused = _open_plaintext_file(path)
    with pytest.raises(CacheError) as caught:
        refused._migrate_schema()
    assert caught.value.code == "schema_too_new"
    assert str(caught.value) == "The mail cache could not be opened."
    assert _schema_version(refused._conn) == len(MIGRATIONS) + 1
    assert refused._conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1
    refused.close()

    # The refusal damaged nothing: back at a supported version it opens whole.
    repaired = _open_plaintext_file(path)
    repaired._conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
    repaired._conn.commit()
    repaired._migrate_schema()
    assert repaired.get_raw_message(mail.id) == b"From: ada@x stating hi"
    repaired.close()


@needs_sqlcipher
def test_a_plaintext_v0_profile_converts_and_upgrades(tmp_path):
    path = tmp_path / "postcard.db"
    plain = sqlite3.connect(str(path))
    _load_fixture(plain, "mail_v0.sql")
    plain.close()

    database = Database(str(path), key=_CACHE_KEY)

    stored = path.read_bytes()
    assert not stored.startswith(b"SQLite format 3\x00")
    assert b"Lunch plans" not in stored
    assert _schema_version(database._conn) == len(MIGRATIONS)
    assert [a.email for a in database.accounts()] == [
        "ada@example.com",
        "grace@work.example",
    ]
    outbox = database.get_folder_by_name(1, "Outbox")
    assert outbox is not None
    queued = database.emails_in_folder(outbox.id)
    assert {m.server_id for m in queued} == {None}
    bodies = {m.subject: database.get_raw_message(m.id) for m in queued}
    assert bodies["Queued: trip report"] == b"Queued trip report body awaiting send"
    inbox = database.get_folder_by_name(1, "INBOX")
    assert inbox is not None
    (thread,) = database.search_conversations([inbox.id], "dessert")
    assert thread.count == 2
    database.close()

    reopened = Database(str(path), key=_CACHE_KEY)
    assert len(reopened.accounts()) == 2
    reopened.close()


@needs_sqlcipher
def test_a_failed_encrypted_upgrade_resumes_on_reopen(tmp_path, monkeypatch):
    path = tmp_path / "postcard.db"
    plain = sqlite3.connect(str(path))
    _load_fixture(plain, "mail_v5.sql")
    plain.close()
    database_module._migrate_plaintext_to_encrypted(str(path), _CACHE_KEY)

    opened = []
    real_open = database_module._open_encrypted

    def flaky_open(db_path, key):
        conn = real_open(db_path, key)
        opened.append(conn)
        return _FlakyConnection(
            conn,
            lambda sql: sql == "ALTER TABLE folders ADD COLUMN uidvalidity INTEGER",
        )

    monkeypatch.setattr(database_module, "_open_encrypted", flaky_open)
    with pytest.raises(CacheError) as caught:
        Database(str(path), key=_CACHE_KEY)
    assert caught.value.code == "schema_upgrade_failed"
    for conn in opened:
        conn.close()
    monkeypatch.undo()

    probe = database_module._open_encrypted(str(path), _CACHE_KEY)
    assert _schema_version(probe) == 9
    assert "uidvalidity" not in _column_names(probe, "folders")
    probe.close()

    resumed = Database(str(path), key=_CACHE_KEY)
    assert _schema_version(resumed._conn) == len(MIGRATIONS)
    folders = {f.name: f for f in resumed.folders_for_account(1)}
    assert folders["Work.Projects"].parent_id == folders["Work"].id
    (thread,) = resumed.search_conversations([folders["INBOX"].id], "lunch")
    assert thread.count == 2
    resumed.close()


@needs_sqlcipher
def test_a_broken_plaintext_profile_is_left_byte_identical(tmp_path):
    path = tmp_path / "postcard.db"
    plain = sqlite3.connect(str(path))
    _load_fixture(plain, "mail_v0.sql")
    plain.close()
    stored = bytearray(path.read_bytes())
    stored[128:384] = b"\x00" * 256
    path.write_bytes(bytes(stored))
    before = path.read_bytes()

    with pytest.raises(CacheError) as caught:
        Database(str(path), key=_CACHE_KEY)

    assert caught.value.code == "migration_failed"
    assert path.read_bytes() == before
    assert not (tmp_path / "postcard.db.migrating").exists()


@needs_sqlcipher
def test_a_newer_encrypted_schema_is_refused_without_touching_mail(tmp_path):
    path = str(tmp_path / "postcard.db")
    database = Database(path, key=_CACHE_KEY)
    account = database.save_account("a@x", "A", "imap.x", 993, "smtp.x", 587)
    folder = database.get_or_create_folder(account.id, "INBOX")
    mail = database.save_email(
        folder.id, "Ada", "Hi", "", "2026-01-01", True, server_id="1"
    )
    database.save_raw_message(mail.id, b"From: ada@x stating hi")
    database.close()

    bump = database_module._open_encrypted(path, _CACHE_KEY)
    bump.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    bump.commit()
    bump.close()

    with pytest.raises(CacheError) as caught:
        Database(path, key=_CACHE_KEY)
    assert caught.value.code == "schema_too_new"

    repair = database_module._open_encrypted(path, _CACHE_KEY)
    repair.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
    repair.commit()
    repair.close()
    reopened = Database(path, key=_CACHE_KEY)
    assert reopened.get_raw_message(mail.id) == b"From: ada@x stating hi"
    assert [m.subject for m in reopened.emails_in_folder(folder.id)] == ["Hi"]
    reopened.close()


# --- outbox queue ------------------------------------------------------------
# One row per mail waiting to go out: draft -> ready -> sending -> sent, with
# failed attempts landing on retry (or terminal failed).


def _queued(db, subject="Hi", raw=b"To: you@x\r\nMessage-ID: <a@x>\r\n\r\n"):
    account = db.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    outbox = db.get_or_create_folder(account.id, "Outbox")
    mail = db.save_email(outbox.id, "you@x", subject, subject, "now", False)
    db.save_raw_message(mail.id, raw)
    return outbox, mail


def test_queueing_keeps_the_envelope_claiming_counts_the_attempt(db):
    outbox, mail = _queued(db)
    db.queue_outbox_email(mail.id, ["you@x", "bcc@x"])

    claimed = db.claim_outbox_email(mail.id)

    assert claimed is not None
    assert claimed.email_id == mail.id
    assert claimed.state is OutboxState.SENDING
    assert claimed.attempts == 1
    assert claimed.envelope == ["you@x", "bcc@x"]
    assert claimed.message_id == "<a@x>"
    assert claimed.raw.startswith(b"To: you@x")


def test_a_second_claim_while_sending_holds_nothing(db):
    _outbox, mail = _queued(db)
    db.queue_outbox_email(mail.id, ["you@x"])

    assert db.claim_outbox_email(mail.id) is not None
    assert db.claim_outbox_email(mail.id) is None


def test_completing_forgets_the_send(db):
    outbox, mail = _queued(db)
    db.queue_outbox_email(mail.id, ["you@x"])
    db.claim_outbox_email(mail.id)

    db.complete_outbox_email(mail.id)
    # The caller files the Sent copy and deletes the Outbox row, which would
    # cascade here anyway; without that the mail reads as legacy mail again.
    db.delete_email(mail.id)

    assert db.queued_send(mail.id) is None
    assert db.pending_outbox_sends(outbox.id) == []


def test_failing_records_retry_with_the_reason(db):
    _outbox, mail = _queued(db)
    db.queue_outbox_email(mail.id, ["you@x"])
    db.claim_outbox_email(mail.id)

    db.fail_outbox_email(mail.id, "connection timed out", OutboxState.RETRY)

    failed = db.queued_send(mail.id)
    assert failed is not None
    assert failed.state is OutboxState.RETRY
    assert failed.attempts == 1
    assert failed.last_error == "connection timed out"
    assert failed.envelope == ["you@x"]


def test_only_a_claimed_row_can_fail(db):
    _outbox, mail = _queued(db)
    db.queue_outbox_email(mail.id, ["you@x"])

    db.fail_outbox_email(mail.id, "never claimed", OutboxState.RETRY)

    assert db.queued_send(mail.id).state is OutboxState.READY


def test_recovery_readies_interrupted_sends_but_spares_live_claims(db):
    _outbox, first = _queued(db, subject="first")
    _outbox, second = _queued(db, subject="second")
    for mail in (first, second):
        db.queue_outbox_email(mail.id, ["you@x"])
        db.claim_outbox_email(mail.id)

    recovered = db.recover_interrupted_sends(except_ids={second.id})

    assert recovered == 1
    assert db.queued_send(first.id).state is OutboxState.READY
    assert db.queued_send(first.id).attempts == 1
    assert db.queued_send(second.id).state is OutboxState.SENDING


def test_pending_lists_ready_retry_and_sending_but_never_failed(db):
    outbox, ready = _queued(db, subject="ready")
    others = []
    for subject in ("retry", "sending", "failed"):
        mail = db.save_email(outbox.id, "you@x", subject, subject, "now", False)
        db.save_raw_message(mail.id, b"To: you@x\r\n\r\n")
        others.append(mail)
    retry, sending, failed = others
    for mail in (ready, retry, sending, failed):
        db.queue_outbox_email(mail.id, ["you@x"])
        db.claim_outbox_email(mail.id)
    db.fail_outbox_email(retry.id, "boom", OutboxState.RETRY)
    db.fail_outbox_email(failed.id, "boom", OutboxState.FAILED)

    assert [send.subject for send in db.pending_outbox_sends(outbox.id)] == [
        "ready",
        "retry",
        "sending",
    ]


def test_pending_reads_legacy_mail_as_ready_without_an_envelope(db):
    outbox, mail = _queued(db)
    # No queue_outbox_email: queued before the queue table existed.

    [send] = db.pending_outbox_sends(outbox.id)

    assert send.email_id == mail.id
    assert send.state is OutboxState.READY
    assert send.envelope is None
    assert send.attempts == 0


def test_pending_skips_mail_without_a_body(db):
    outbox, mail = _queued(db)
    db._conn.execute("UPDATE emails SET raw_message = NULL WHERE id = ?", (mail.id,))
    db._conn.commit()

    assert db.pending_outbox_sends(outbox.id) == []


def test_requeueing_never_clobbers_a_send_in_flight(db):
    _outbox, mail = _queued(db)
    db.queue_outbox_email(mail.id, ["you@x", "bcc@x"])
    db.claim_outbox_email(mail.id)

    db.queue_outbox_email(mail.id, ["you@x"])

    assert db.queued_send(mail.id).envelope == ["you@x", "bcc@x"]
    assert db.queued_send(mail.id).state is OutboxState.SENDING


def test_deleting_the_mail_drops_its_queue_row(db):
    _outbox, mail = _queued(db)
    db.queue_outbox_email(mail.id, ["you@x"])
    db.claim_outbox_email(mail.id)

    db.delete_email(mail.id)

    assert db.queued_send(mail.id) is None


def test_filing_twice_replaces_the_earlier_copy(db):
    account = db.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    sent = db.get_or_create_folder(account.id, "Sent")
    first = db.save_email(sent.id, "me@x", "Hi", "Hi", "now", False, server_id="7")
    db.save_raw_message(first.id, b"Message-ID: <a@x>\r\n\r\n")

    db.delete_emails_by_message_id(sent.id, "<a@x>")

    assert db.emails_in_folder(sent.id) == []
