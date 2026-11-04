"""Query budgets for cached-mail refresh (issue #14).

Pins the deterministic invariants, not wall-clock time: batched sync writes
store exactly what sequential saves store, in one transaction; an unchanged
folder skips its rethread; a conversation page is the full list cut to size
(same grouping, same UID/date ordering, whole threads, same unread filter);
and every list query costs a constant number of statements however large the
folder grows. No test here asserts milliseconds -- those numbers live in
tests/bench/bench_mailbox.py, which re-checks them on every run.

Budgets (main loop, 50k-mail mailbox, ~4x headroom over baseline):

    op                        budget    baseline
    50-header batch write       5 ms      1.2 ms
    50-header sequential writes 40 ms     9.8 ms (why the batch exists)
    rethread, folder changed  1200 ms     292 ms
    rethread, folder unchanged  75 ms     17.7 ms
    conversation page (200)    500 ms     124 ms
    full conversation list    2500 ms     627 ms
    search page (200)          250 ms      53 ms
    full search                600 ms     138 ms
    peak RSS                   500 MB     116 MB

Baseline: 2026-11-04, Linux x86_64, 12 CPUs, 24 GB RAM, SQLite 3.54.1 over
SQLCipher (the app's own encrypted open). Reproduce it with
`python3 tests/bench/bench_mailbox.py`; the JSON record carries the hardware.

Two facts the budgets rest on. Every Database method runs on the main thread
only -- the batch write lands in _on_sync_done, which GLib.idle_add already
marshalled home, so no worker ever shares the connection. And EXPLAIN QUERY
PLAN shows every new query as a SEARCH over idx_emails_unread/idx_emails_uid
(the fingerprint, the page's two halves, the batch's UID lookup), so no new
index was added: the page-1 win comes from hydrating 200 threads instead of
the folder, not from a new access path.
"""

import contextlib
import sqlite3

import pytest

import postcard.core.threader as threader
from postcard.core.models.message_header import MessageHeader
from postcard.core.store.database import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def folder(db):
    account = db.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    return db.get_or_create_folder(account.id, "INBOX")


def _header(uid, subject="Subject", **kwargs):
    kwargs.setdefault("sender", "Ada <ada@example.com>")
    kwargs.setdefault("sender_address", "ada@example.com")
    kwargs.setdefault("recipient", "")
    kwargs.setdefault("recipient_address", "")
    kwargs.setdefault("preview", "preview")
    kwargs.setdefault("date", "2026-10-01T12:00:00+00:00")
    kwargs.setdefault("is_unread", True)
    kwargs.setdefault("message_id", f"<mid-{uid}@budgets>")
    return MessageHeader(uid=uid, subject=subject, **kwargs)


def _dump(db, folder_id):
    """Every stored column, in row order, for twin-database comparison."""
    rows = db._conn.execute(
        "SELECT * FROM emails WHERE folder_id = ? ORDER BY id", (folder_id,)
    ).fetchall()
    return [tuple(row) for row in rows]


def _fresh_inbox():
    """A second database holding one INBOX, to run the sequential twin in."""
    database = Database(":memory:")
    account = database.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    return database, database.get_or_create_folder(account.id, "INBOX")


@contextlib.contextmanager
def _counted_selects(db):
    """Collect the SELECT statements a block issues, to pin query counts."""
    selects: list[str] = []
    db._conn.set_trace_callback(
        lambda sql: (
            selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
        )
    )
    try:
        yield selects
    finally:
        db._conn.set_trace_callback(None)


def _threaded_headers():
    """Twelve mails: a three-mail thread, a two-mail thread, seven loners."""
    headers = [
        _header("1", "Lunch plans", message_id="<one@x>"),
        _header(
            "2",
            "Re: Lunch plans",
            message_id="<two@x>",
            in_reply_to="<one@x>",
            references="<one@x>",
        ),
        _header(
            "3",
            "Re: Lunch plans",
            message_id="<three@x>",
            in_reply_to="<two@x>",
            references="<one@x> <two@x>",
        ),
        _header("4", "Dentist", message_id="<four@x>"),
        _header("5", "Re: Dentist", message_id="<five@x>", in_reply_to="<four@x>"),
    ]
    headers += [
        _header(str(uid), f"Notice {uid}", is_unread=bool(uid % 2))
        for uid in range(6, 13)
    ]
    return headers


# --- batched sync writes ----------------------------------------------------


def test_batch_save_matches_sequential_saves(db, folder):
    twin, twin_folder = _fresh_inbox()
    try:
        fresh = _threaded_headers()[:10]
        fresh[7] = _header("8", "No Message-ID here", message_id="")
        restated = [
            _header(
                header.uid,
                header.subject,
                is_unread=not header.is_unread,
                is_starred=True,
                recipient="Me",
                recipient_address="me@example.com",
                message_id=header.message_id,
                in_reply_to=header.in_reply_to,
                references=header.references,
            )
            for header in fresh
        ]
        newcomers = [_header(str(uid), f"Later {uid}") for uid in range(20, 25)]

        batch_first = db.save_incoming_emails(folder.id, fresh)
        twin_first = [
            twin.save_incoming_email(twin_folder.id, header) for header in fresh
        ]
        batch_second = db.save_incoming_emails(folder.id, restated + newcomers)
        twin_second = [
            twin.save_incoming_email(twin_folder.id, header)
            for header in restated + newcomers
        ]

        assert batch_first == twin_first == [True] * len(fresh)
        assert batch_second == twin_second == [False] * len(restated) + [True] * 5
        db.reassign_conversations(folder.id)
        twin.reassign_conversations(twin_folder.id)
        assert _dump(db, folder.id) == _dump(twin, twin_folder.id)
    finally:
        twin.close()


def test_batch_save_reports_each_message_as_new_only_once(db, folder):
    headers = _threaded_headers()

    assert db.save_incoming_emails(folder.id, headers) == [True] * len(headers)
    assert db.save_incoming_emails(folder.id, headers) == [False] * len(headers)
    assert len(_dump(db, folder.id)) == len(headers)


def test_batch_save_replaces_a_uid_holding_another_message(db, folder):
    db.save_incoming_email(folder.id, _header("5", "Old", message_id="<old@x>"))
    (row,) = db.emails_in_folder(folder.id)
    db.save_raw_message(row.id, b"Message-ID: <old@x>\n\nold body")

    (added,) = db.save_incoming_emails(
        folder.id, [_header("5", "New", message_id="<new@x>")]
    )

    assert added is True
    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.subject, mail.message_id) == ("New", "<new@x>")
    assert db.get_raw_message(mail.id) is None


def test_batch_save_drops_uidless_duplicates_but_keeps_other_drafts(db, folder):
    db.save_email(folder.id, "Me", "Sent copy", "", "2026-10-01", False)
    (sent_copy,) = db.emails_in_folder(folder.id)
    db.save_raw_message(sent_copy.id, b"Message-ID: <sent@x>\n\nsent")
    draft = db.save_email(folder.id, "Me", "Draft", "", "2026-10-01", False)

    (added,) = db.save_incoming_emails(
        folder.id, [_header("9", "Sent copy", message_id="<sent@x>")]
    )

    assert added is True
    remaining = db.emails_in_folder(folder.id)
    assert [(mail.id, mail.server_id) for mail in remaining] == [
        (draft.id + 1, "9"),
        (draft.id, None),
    ]


def test_batch_save_names_a_uid_twice_like_sequential_saves(db, folder):
    twin, twin_folder = _fresh_inbox()
    try:
        headers = [
            _header("5", "First", message_id="<first@x>"),
            _header(
                "5",
                "Second",
                message_id="<second@x>",
                is_unread=False,
                is_starred=True,
                recipient="Me",
                recipient_address="me@example.com",
            ),
        ]

        assert db.save_incoming_emails(folder.id, headers) == [True, True]
        assert [
            twin.save_incoming_email(twin_folder.id, header) for header in headers
        ] == [True, True]
        # Same final row, except the id: sequential saves insert, delete and
        # re-insert, consuming an extra rowid the single upsert never needs.
        batch_rows = [row[1:] for row in _dump(db, folder.id)]
        twin_rows = [row[1:] for row in _dump(twin, twin_folder.id)]
        assert batch_rows == twin_rows
        (mail,) = db.emails_in_folder(folder.id)
        assert (mail.subject, mail.message_id, mail.is_unread, mail.is_starred) == (
            "Second",
            "<second@x>",
            False,
            True,
        )
    finally:
        twin.close()


def test_batch_save_is_atomic(db, folder):
    bad = _header("2", subject=None)

    with pytest.raises(sqlite3.IntegrityError):
        db.save_incoming_emails(
            folder.id, [_header("1", "Ok"), bad, _header("3", "Ok")]
        )

    assert _dump(db, folder.id) == []


def test_batch_save_of_nothing_changes_nothing(db, folder):
    db.save_incoming_emails(folder.id, _threaded_headers())
    assert db.reassign_conversations(folder.id) is True

    assert db.save_incoming_emails(folder.id, []) == []

    assert db.reassign_conversations(folder.id) is False


# --- skipping the rethread --------------------------------------------------


def test_rethread_skips_an_unchanged_folder(db, folder, monkeypatch):
    calls: list[int] = []
    original = threader.group_keys

    def counting(keys):
        calls.append(len(keys))
        return original(keys)

    monkeypatch.setattr(threader, "group_keys", counting)
    db.save_incoming_emails(folder.id, _threaded_headers())

    assert db.reassign_conversations(folder.id) is True
    assert db.reassign_conversations(folder.id) is False
    assert calls == [12]

    restated = [
        _header(
            header.uid,
            header.subject,
            is_unread=False,
            message_id=header.message_id,
            in_reply_to=header.in_reply_to,
            references=header.references,
        )
        for header in _threaded_headers()
    ]
    assert db.save_incoming_emails(folder.id, restated) == [False] * 12
    assert db.reassign_conversations(folder.id) is False
    assert calls == [12]

    db.save_incoming_email(folder.id, _header("50", "New arrival"))
    assert db.reassign_conversations(folder.id) is True
    assert calls == [12, 13]


def test_rethread_runs_after_prune_and_validity_change(db, folder):
    db.save_incoming_emails(folder.id, _threaded_headers())
    assert db.reassign_conversations(folder.id) is True
    assert db.reassign_conversations(folder.id) is False

    db.prune_stale_emails(folder.id, {str(uid) for uid in range(1, 12)})
    assert db.reassign_conversations(folder.id) is True
    assert db.reassign_conversations(folder.id) is False

    assert db.set_folder_uidvalidity(folder.id, 4242) is True
    assert db.reassign_conversations(folder.id) is True
    assert db.set_folder_uidvalidity(folder.id, 4242) is False
    assert db.reassign_conversations(folder.id) is False


def test_rethread_runs_after_raw_message_fills_a_message_id(db, folder):
    mail = db.save_email(folder.id, "Ada", "Hello", "", "2026-10-01", True)
    assert db.reassign_conversations(folder.id) is True
    assert db.reassign_conversations(folder.id) is False

    db.save_raw_message(mail.id, b"Message-ID: <hello@x>\n\nhello")

    assert db.reassign_conversations(folder.id) is True
    assert db.reassign_conversations(folder.id) is False


# --- paged conversation queries ---------------------------------------------


def _summaries(conversations):
    return [
        (item.id, item.subject, item.count, [mail.id for mail in item.emails])
        for item in conversations
    ]


def test_paged_conversations_equal_the_full_list(db, folder):
    db.save_incoming_emails(folder.id, _threaded_headers())
    db.reassign_conversations(folder.id)
    full = _summaries(db.conversations_in_folders([folder.id]))

    paged = []
    for offset in (0, 5, 10):
        paged += _summaries(
            db.conversations_in_folders([folder.id], limit=5, offset=offset)
        )

    assert len(full) == 9
    assert paged == full


def test_paging_preserves_uid_ordering_with_uidless_newest(db, folder):
    db.save_incoming_emails(
        folder.id,
        [
            _header("30", "Thirty"),
            _header("7", "Seven"),
            _header("200", "Two hundred"),
        ],
    )
    local = db.save_email(folder.id, "Me", "Unsent", "", "2026-10-04", False)
    (local_mail,) = [
        mail for mail in db.emails_in_folder(folder.id) if mail.id == local.id
    ]
    assert local_mail.server_id is None
    db.reassign_conversations(folder.id)

    full = [item.subject for item in db.conversations_in_folders([folder.id])]
    paged = []
    for offset in (0, 2):
        paged += [
            item.subject
            for item in db.conversations_in_folders([folder.id], limit=2, offset=offset)
        ]

    assert full == ["Unsent", "Two hundred", "Thirty", "Seven"]
    assert paged == full


def test_paging_preserves_date_ordering_with_junk_last(db, folder):
    # Opaque Graph-style ids order by Date; an unreadable one sorts last.
    db.save_incoming_emails(
        folder.id,
        [
            _header("graph-b", "Graph newer", date="2026-10-03T00:00:00+00:00"),
            _header("graph-a", "Graph older", date="2026-10-01T00:00:00+00:00"),
            _header("graph-junk", "Graph junk", date="whenever"),
        ],
    )
    db.reassign_conversations(folder.id)

    full = [item.subject for item in db.conversations_in_folders([folder.id])]
    paged = [
        item.subject
        for offset in (0, 2)
        for item in db.conversations_in_folders([folder.id], limit=2, offset=offset)
    ]

    assert full == ["Graph newer", "Graph older", "Graph junk"]
    assert paged == full


def test_paging_across_folders_orders_by_date(db, folder):
    account_id = db.accounts()[0].id
    sent = db.get_or_create_folder(account_id, "Sent")
    db.save_incoming_emails(
        folder.id, [_header("1", "Inbox old", date="2026-10-01T00:00:00+00:00")]
    )
    db.save_incoming_emails(
        sent.id, [_header("1", "Sent new", date="2026-10-05T00:00:00+00:00")]
    )
    db.reassign_conversations(folder.id)
    db.reassign_conversations(sent.id)
    ids = [folder.id, sent.id]

    full = [item.subject for item in db.conversations_in_folders(ids)]
    pages = [
        item.subject
        for offset in (0, 1)
        for item in db.conversations_in_folders(ids, limit=1, offset=offset)
    ]

    assert full == ["Sent new", "Inbox old"]
    assert pages == full


def test_paged_search_returns_whole_threads(db, folder):
    headers = _threaded_headers()
    headers[1] = _header(
        "2",
        "Re: Lunch plans",
        message_id="<two@x>",
        in_reply_to="<one@x>",
        references="<one@x>",
        preview="meet at the lighthouse",
    )
    db.save_incoming_emails(folder.id, headers)
    db.reassign_conversations(folder.id)

    full = db.search_conversations([folder.id], "lighthouse")
    assert [(item.subject, item.count) for item in full] == [("Re: Lunch plans", 3)]
    pages = [
        item
        for offset in (0, 1)
        for item in db.search_conversations(
            [folder.id], "lighthouse", limit=1, offset=offset
        )
    ]
    assert _summaries(pages) == _summaries(full)

    blank = db.search_conversations([folder.id], "   ", limit=2)
    assert _summaries(blank) == _summaries(
        db.conversations_in_folders([folder.id], limit=2)
    )


def test_unread_filter_keeps_the_open_conversation_in_place(db, folder):
    db.save_incoming_emails(folder.id, _threaded_headers())
    db.reassign_conversations(folder.id)
    full = db.conversations_in_folders([folder.id])
    (keep, *_) = [item for item in full if item.is_unread]
    # Opening it marks it read; the filter must still list it, in place.
    for mail in keep.emails:
        db.set_email_unread(mail.id, False)

    matches = db.conversations_in_folders(
        [folder.id], unread_only=True, keep_id=keep.id
    )
    reread = db.conversations_in_folders([folder.id])
    expected = [item for item in reread if item.is_unread or item.id == keep.id]

    assert [item.id for item in matches] == [item.id for item in expected]
    assert keep.id in [item.id for item in matches]
    without_keep = db.conversations_in_folders([folder.id], unread_only=True)
    assert keep.id not in [item.id for item in without_keep]


def test_page_bounds(db, folder):
    db.save_incoming_emails(folder.id, _threaded_headers())
    db.reassign_conversations(folder.id)
    full = db.conversations_in_folders([folder.id])

    assert db.conversations_in_folders([folder.id], limit=0) == []
    assert db.conversations_in_folders([folder.id], limit=5, offset=99) == []
    tail = db.conversations_in_folders([folder.id], offset=7)
    assert [item.id for item in tail] == [item.id for item in full[7:]]
    assert db.conversations_in_folders([]) == []
    assert db.search_conversations([], "lighthouse") == []


def test_large_folder_pages_without_hydrating_everything(db, folder):
    headers = [_header(str(uid), f"Bulk message {uid}") for uid in range(1, 2001)]
    db.save_incoming_emails(folder.id, headers)
    db.reassign_conversations(folder.id)

    with _counted_selects(db) as selects:
        page = db.conversations_in_folders([folder.id], limit=200)
        page_selects = len(selects)
        selects.clear()
        full = db.conversations_in_folders([folder.id])
        full_selects = len(selects)

    assert len(page) == 200
    assert len(full) == 2000
    assert [item.id for item in page] == [item.id for item in full[:200]]
    assert page_selects == 2
    assert full_selects == 1

    # One page naming more threads than a single IN list holds still reads
    # back whole and in order.
    whole = db.conversations_in_folders([folder.id], limit=2000)
    assert _summaries(whole) == _summaries(full)


def test_search_page_costs_two_selects(db, folder):
    db.save_incoming_emails(folder.id, _threaded_headers())
    db.reassign_conversations(folder.id)

    with _counted_selects(db) as selects:
        db.search_conversations([folder.id], "lunch", limit=5)

    assert len(selects) == 2
