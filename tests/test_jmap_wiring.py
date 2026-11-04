"""The native JMAP driver end to end over a fake transport: discovery
through delta sync, flags, moves, submission and the Sent-copy lookup.

Every id is opaque, every name non-English somewhere, and any touch of IMAP or
SMTP fails the test outright -- a JMAP account must sync purely over JMAP.
"""

import json

import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import (
    FASTMAIL_HOST,
    FASTMAIL_PORT,
    PROTOCOL_JMAP,
    Account,
)
from postcard.core.models.folder import FolderRole
from postcard.core.net.auth import MECHANISM_XOAUTH2, Credential
from postcard.core.net.fastmail_oauth import FASTMAIL_JMAP_SESSION_URL
from postcard.core.net.imap_session import FLAG_FLAGGED, FLAG_SEEN
from postcard.core.net.jmap_session import (
    CORE_URN,
    MAIL_URN,
    SUBMISSION_URN,
    JmapError,
    Response,
)
from postcard.core.net.jmap_session import (
    JmapSession as RealJmapSession,
)
from postcard.core.net.jmap_session import (
    discover as real_discover,
)

CREDENTIAL = Credential(
    "sync-tester@fastmail.com", "example-secret-9", MECHANISM_XOAUTH2
)

FASTMAIL_API_URL = "https://api.fastmail.com/jmap/api/"

SESSION = {
    "capabilities": {
        CORE_URN: {
            "maxCallsInRequest": 32,
            "maxObjectsInGet": 500,
            "maxObjectsInSet": 500,
        },
        MAIL_URN: {"maxMailboxesPerEmail": 1000},
        SUBMISSION_URN: {"maxDelayedSend": 0},
    },
    "accounts": {
        "u12345": {
            "name": "sync-tester@fastmail.com",
            "isPersonal": True,
            "isReadOnly": False,
            "accountCapabilities": {MAIL_URN: {}, SUBMISSION_URN: {}},
        }
    },
    "primaryAccounts": {MAIL_URN: "u12345", SUBMISSION_URN: "u12345"},
    "username": "sync-tester@fastmail.com",
    "apiUrl": FASTMAIL_API_URL,
    "downloadUrl": "https://api.fastmail.com/jmap/download/{accountId}/{blobId}/{name}",
    "uploadUrl": "https://api.fastmail.com/jmap/upload/{accountId}/",
    "eventSourceUrl": "https://api.fastmail.com/jmap/eventsource/"
    "?types={types}&closeafter={closeafter}&ping={ping}",
    "state": "fastmail-state-1",
}

# Opaque server ids throughout: nothing numeric, nothing English-derived.
INBOX = "FbInboxOpaque1"
SENT = "FbSentOpaque3"
DRAFTS = "FbDraftsOpaque7"
ARCHIVE = "FbArchiveOpaque9"


def fastmail_account() -> Account:
    return Account(
        id=7,
        email="sync-tester@fastmail.com",
        display_name="Sync Tester",
        imap_host=FASTMAIL_HOST,
        imap_port=FASTMAIL_PORT,
        smtp_host=FASTMAIL_HOST,
        smtp_port=FASTMAIL_PORT,
        protocol=PROTOCOL_JMAP,
    )


def reply(body=None, status=200):
    return Response(status, json.dumps(body).encode() if body is not None else b"")


def answers(*responses):
    return reply({"methodResponses": list(responses)})


class FakeTransport:
    """Scripted JMAP answers. Anything unscripted -- including any IMAP or
    SMTP traffic, which never reaches a transport -- fails the test."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def bind_jmap(monkeypatch, transport):
    """Serve mail_sync's JMAP calls from the scripted transport, through the
    real discovery and session layers."""

    def fake_discover(url, credential):
        assert url == FASTMAIL_JMAP_SESSION_URL
        return real_discover(url, credential, transport)

    def fake_session(session, credential, **kwargs):
        return RealJmapSession(session, credential, transport, **kwargs)

    monkeypatch.setattr(mail_sync, "discover", fake_discover)
    monkeypatch.setattr(mail_sync, "JmapSession", fake_session)


def sent_calls(transport):
    """The JMAP method calls made, in order (blob and discovery traffic out)."""
    calls = []
    for request in transport.requests:
        if not request.data or request.full_url != FASTMAIL_API_URL:
            continue
        calls.extend(json.loads(request.data)["methodCalls"])
    return calls


def mailbox_row(mailbox_id, name, role, unread=0, total=0):
    return {
        "id": mailbox_id,
        "name": name,
        "parentId": None,
        "role": role,
        "unreadEmails": unread,
        "totalEmails": total,
        "sortOrder": 0,
    }


ALL_MAILBOXES = [
    mailbox_row(INBOX, "Posteingang", "inbox", unread=2, total=3),
    mailbox_row(SENT, "Gesendete Elemente", "sent", total=12),
    mailbox_row(DRAFTS, "Entwürfe", "drafts", unread=1, total=1),
    mailbox_row(ARCHIVE, "Archiv", "archive", total=40),
]


def mailbox_list(boxes=None):
    return ["Mailbox/get", {"list": ALL_MAILBOXES if boxes is None else boxes}, "0"]


def mailbox_state(state="mb1"):
    return ["Mailbox/get", {"list": [], "state": state}, "0"]


def email_row(uid, subject="Quartalszahlen", message_id=None):
    return {
        "id": uid,
        "threadId": f"T{uid}",
        "from": [{"name": "Beate Beispiel", "email": "beate@example.org"}],
        "to": [{"name": "", "email": "sync-tester@fastmail.com"}],
        "cc": [],
        "subject": subject,
        "sentAt": "2026-09-12T08:30:00Z",
        "receivedAt": "2026-09-12T08:31:00Z",
        "keywords": {},
        "preview": "Hier sind sie",
        "header:Message-ID:asText": message_id or f"<{uid.lower()}@example.org>",
        "header:In-Reply-To:asText": "",
        "header:References:asText": "",
    }


@pytest.fixture(autouse=True)
def isolated_jmap(monkeypatch):
    """No IMAP/SMTP traffic and no cursors leaking between tests."""

    def forbidden(*args, **kwargs):
        pytest.fail("a JMAP operation reached IMAP/SMTP")

    monkeypatch.setattr(mail_sync, "ImapSession", forbidden)
    monkeypatch.setattr(mail_sync, "SmtpSession", forbidden)
    mail_sync._jmap_states.clear()
    yield
    mail_sync._jmap_states.clear()


def first_sync_script(*extra):
    """Discovery plus a first inbox sync over three messages, newest first."""
    return [
        reply(SESSION),
        answers(mailbox_list()),
        answers(mailbox_state("mb1")),
        answers(
            [
                "Email/query",
                {"ids": ["M3", "M2", "M1"], "total": 3, "queryState": "qs1"},
                "0",
            ]
        ),
        answers(
            [
                "Email/get",
                {
                    "list": [email_row("M3"), email_row("M2"), email_row("M1")],
                    "notFound": [],
                },
                "0",
            ]
        ),
        *extra,
    ]


def test_first_sync_walks_folders_and_inbox_over_jmap(monkeypatch):
    transport = FakeTransport(*first_sync_script())
    bind_jmap(monkeypatch, transport)

    result = mail_sync.fetch_mailbox(fastmail_account(), CREDENTIAL)

    assert result.folder == INBOX
    assert [(box.name, box.role, box.label) for box in result.folders] == [
        (INBOX, FolderRole.INBOX, "Posteingang"),
        (SENT, FolderRole.SENT, "Gesendete Elemente"),
        (DRAFTS, FolderRole.DRAFTS, "Entwürfe"),
        (ARCHIVE, FolderRole.ARCHIVE, "Archiv"),
    ]
    assert [message.uid for message in result.messages] == ["M3", "M2", "M1"]
    assert result.all_uids == {"M1", "M2", "M3"}
    assert result.uidvalidity is None
    assert result.exists == 3
    assert result.unread_counts == {SENT: 0, DRAFTS: 1, ARCHIVE: 0}
    assert [method for method, _args, _id in sent_calls(transport)] == [
        "Mailbox/get",
        "Mailbox/get",
        "Email/query",
        "Email/get",
    ]
    state = mail_sync._jmap_states[7]
    assert state.mailbox_state == "mb1"
    assert state.queries[INBOX].state == "qs1"


def test_second_sync_walks_folders_via_delta(monkeypatch):
    transport = FakeTransport(
        *first_sync_script(
            answers(
                [
                    "Mailbox/changes",
                    {
                        "created": [],
                        "updated": [],
                        "destroyed": [],
                        "newState": "mb2",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/queryChanges",
                    {
                        "added": [{"threadId": "TM4", "emailId": "M4"}],
                        "removed": ["M1"],
                        "newQueryState": "qs2",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/query",
                    {"ids": ["M4", "M3", "M2"], "total": 3, "queryState": "qs-page"},
                    "0",
                ]
            ),
            answers(
                [
                    "Email/get",
                    {
                        "list": [email_row("M4"), email_row("M3"), email_row("M2")],
                        "notFound": [],
                    },
                    "0",
                ]
            ),
        )
    )
    bind_jmap(monkeypatch, transport)
    account = fastmail_account()

    mail_sync.fetch_mailbox(account, CREDENTIAL)
    result = mail_sync.fetch_mailbox(account, CREDENTIAL)

    assert [message.uid for message in result.messages] == ["M4", "M3", "M2"]
    assert result.all_uids == {"M2", "M3", "M4"}
    assert [method for method, _args, _id in sent_calls(transport)][4:] == [
        "Mailbox/changes",
        "Email/queryChanges",
        "Email/query",
        "Email/get",
    ]
    [[_method, arguments, _id]] = [
        call for call in sent_calls(transport) if call[0] == "Email/queryChanges"
    ]
    assert arguments["sinceQueryState"] == "qs1"
    assert mail_sync._jmap_states[7].queries[INBOX].state == "qs2"


def test_a_failed_fetch_keeps_the_old_cursor(monkeypatch):
    transport = FakeTransport(
        *first_sync_script(
            answers(
                [
                    "Mailbox/changes",
                    {
                        "created": [],
                        "updated": [],
                        "destroyed": [],
                        "newState": "mb2",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/queryChanges",
                    {
                        "added": [{"threadId": "TM4", "emailId": "M4"}],
                        "removed": [],
                        "newQueryState": "qs2",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/query",
                    {"ids": ["M4", "M3", "M2"], "total": 4, "queryState": "qs-page"},
                    "0",
                ]
            ),
            Response(500, b"{}"),
            # The retry resumes from the stored states, not the failed ones.
            answers(
                [
                    "Mailbox/changes",
                    {
                        "created": [],
                        "updated": [],
                        "destroyed": [],
                        "newState": "mb3",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/queryChanges",
                    {
                        "added": [{"threadId": "TM4", "emailId": "M4"}],
                        "removed": [],
                        "newQueryState": "qs3",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/query",
                    {"ids": ["M4", "M3", "M2"], "total": 4, "queryState": "qs-page"},
                    "0",
                ]
            ),
            answers(
                [
                    "Email/get",
                    {
                        "list": [email_row("M4"), email_row("M3"), email_row("M2")],
                        "notFound": [],
                    },
                    "0",
                ]
            ),
        )
    )
    bind_jmap(monkeypatch, transport)
    account = fastmail_account()
    mail_sync.fetch_mailbox(account, CREDENTIAL)

    with pytest.raises(JmapError):
        mail_sync.fetch_mailbox(account, CREDENTIAL)

    state = mail_sync._jmap_states[7]
    assert state.mailbox_state == "mb2"
    assert state.queries[INBOX].state == "qs1"

    result = mail_sync.fetch_mailbox(account, CREDENTIAL)

    assert result.all_uids == {"M1", "M2", "M3", "M4"}
    assert mail_sync._jmap_states[7].queries[INBOX].state == "qs3"


def test_expired_query_state_triggers_a_full_walk(monkeypatch):
    transport = FakeTransport(
        *first_sync_script(
            answers(
                [
                    "Mailbox/changes",
                    {
                        "created": [],
                        "updated": [],
                        "destroyed": [],
                        "newState": "mb2",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                ["error", {"type": "cannotCalculateChanges", "description": "old"}, "0"]
            ),
            answers(
                [
                    "Email/query",
                    {
                        "ids": ["M5", "M4", "M3", "M2"],
                        "total": 4,
                        "queryState": "qs9",
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/get",
                    {"list": [email_row("M5"), email_row("M4")], "notFound": []},
                    "0",
                ]
            ),
        )
    )
    bind_jmap(monkeypatch, transport)
    account = fastmail_account()
    mail_sync.fetch_mailbox(account, CREDENTIAL)

    result = mail_sync.fetch_mailbox(account, CREDENTIAL, limit=2)

    assert [message.uid for message in result.messages] == ["M5", "M4"]
    assert result.all_uids == {"M2", "M3", "M4", "M5"}
    assert mail_sync._jmap_states[7].queries[INBOX].state == "qs9"


def test_expired_mailbox_state_relists_the_tree(monkeypatch):
    transport = FakeTransport(
        *first_sync_script(
            answers(
                ["error", {"type": "cannotCalculateChanges", "description": "old"}, "0"]
            ),
            answers(mailbox_list()),
            answers(mailbox_state("mb9")),
            answers(
                [
                    "Email/queryChanges",
                    {
                        "added": [],
                        "removed": [],
                        "newQueryState": "qs2",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/query",
                    {"ids": ["M3", "M2", "M1"], "total": 3, "queryState": "qs-page"},
                    "0",
                ]
            ),
            answers(
                [
                    "Email/get",
                    {
                        "list": [email_row("M3"), email_row("M2"), email_row("M1")],
                        "notFound": [],
                    },
                    "0",
                ]
            ),
        )
    )
    bind_jmap(monkeypatch, transport)
    account = fastmail_account()
    mail_sync.fetch_mailbox(account, CREDENTIAL)

    result = mail_sync.fetch_mailbox(account, CREDENTIAL)

    assert {box.name for box in result.folders} == {INBOX, SENT, DRAFTS, ARCHIVE}
    assert mail_sync._jmap_states[7].mailbox_state == "mb9"
    # The message cursor is its own token and survives the mailbox re-list.
    assert mail_sync._jmap_states[7].queries[INBOX].state == "qs2"


def test_paging_older_mail_leaves_the_cursors_alone(monkeypatch):
    transport = FakeTransport(
        *first_sync_script(
            answers(
                [
                    "Mailbox/changes",
                    {
                        "created": [],
                        "updated": [],
                        "destroyed": [],
                        "newState": "mb2",
                        "hasMoreChanges": False,
                    },
                    "0",
                ]
            ),
            answers(
                [
                    "Email/query",
                    {"ids": ["M1"], "total": 3, "queryState": "qs-page"},
                    "0",
                ]
            ),
            answers(["Email/get", {"list": [email_row("M1")], "notFound": []}, "0"]),
        )
    )
    bind_jmap(monkeypatch, transport)
    account = fastmail_account()
    mail_sync.fetch_mailbox(account, CREDENTIAL)

    result = mail_sync.fetch_mailbox(account, CREDENTIAL, limit=1, offset=2)

    assert [message.uid for message in result.messages] == ["M1"]
    assert result.all_uids is None
    assert "Email/queryChanges" not in [
        method for method, _args, _id in sent_calls(transport)
    ]
    assert mail_sync._jmap_states[7].queries[INBOX].state == "qs1"


def test_a_folder_is_addressed_by_its_id(monkeypatch):
    sent_ids = [f"Ms{i}" for i in range(12)]
    transport = FakeTransport(
        reply(SESSION),
        answers(mailbox_list()),
        answers(mailbox_state("mb1")),
        answers(
            [
                "Email/query",
                {"ids": sent_ids, "total": 12, "queryState": "qs-sent"},
                "0",
            ]
        ),
        answers(
            [
                "Email/get",
                {"list": [email_row(uid) for uid in sent_ids], "notFound": []},
                "0",
            ]
        ),
    )
    bind_jmap(monkeypatch, transport)

    result = mail_sync.fetch_mailbox(fastmail_account(), CREDENTIAL, folder=SENT)

    assert result.folder == SENT
    assert result.exists == 12
    assert result.all_uids == set(sent_ids)
    [[_method, arguments, _id]] = [
        call for call in sent_calls(transport) if call[0] == "Email/query"
    ]
    assert arguments["filter"] == {"inMailbox": SENT}


@pytest.mark.parametrize(
    ("flag", "should_add", "patch"),
    [
        (FLAG_SEEN, True, {"keywords/$seen": True}),
        (FLAG_SEEN, False, {"keywords/$seen": None}),
        (FLAG_FLAGGED, True, {"keywords/$flagged": True}),
        (FLAG_FLAGGED, False, {"keywords/$flagged": None}),
    ],
)
def test_flags_flow_through_as_keyword_patches(monkeypatch, flag, should_add, patch):
    transport = FakeTransport(
        reply(SESSION),
        answers(["Email/set", {"updated": ["M1", "M2"], "newState": "e2"}, "0"]),
    )
    bind_jmap(monkeypatch, transport)

    outcomes = mail_sync.set_flag(
        fastmail_account(),
        CREDENTIAL,
        INBOX,
        ["M1", "M2"],
        flag,
        should_add,
        uidvalidity=None,
    )

    assert [(outcome.server_id, outcome.succeeded) for outcome in outcomes] == [
        ("M1", True),
        ("M2", True),
    ]
    [[_method, arguments, _id]] = sent_calls(transport)
    assert arguments["update"] == {"M1": patch, "M2": patch}


def test_a_refused_flag_raises_fail_closed(monkeypatch):
    transport = FakeTransport(
        reply(SESSION),
        answers(
            [
                "Email/set",
                {
                    "updated": [],
                    "notUpdated": {
                        "M1": {"type": "forbidden", "description": "locked"}
                    },
                },
                "0",
            ]
        ),
    )
    bind_jmap(monkeypatch, transport)

    with pytest.raises(JmapError) as raised:
        mail_sync.set_flag(
            fastmail_account(),
            CREDENTIAL,
            INBOX,
            ["M1"],
            FLAG_SEEN,
            True,
            uidvalidity=None,
        )

    assert raised.value.code == "forbidden"


def test_an_unknown_flag_is_a_caller_bug(monkeypatch):
    transport = FakeTransport(reply(SESSION))
    bind_jmap(monkeypatch, transport)

    with pytest.raises(JmapError) as raised:
        mail_sync.set_flag(
            fastmail_account(),
            CREDENTIAL,
            INBOX,
            ["M1"],
            "\\Answered",
            True,
            uidvalidity=None,
        )

    assert raised.value.code == "invalidArguments"
    assert sent_calls(transport) == []


def test_moves_change_mailbox_membership_by_id(monkeypatch):
    transport = FakeTransport(
        reply(SESSION),
        answers(["Email/set", {"updated": ["M1", "M2"], "newState": "e2"}, "0"]),
    )
    bind_jmap(monkeypatch, transport)

    result = mail_sync.move_messages(
        fastmail_account(), CREDENTIAL, INBOX, ["M1", "M2"], ARCHIVE, uidvalidity=None
    )

    assert result.destination_uids == ["M1", "M2"]
    assert result.failed_index is None
    assert result.error is None
    [[_method, arguments, _id]] = sent_calls(transport)
    assert arguments["update"] == {
        "M1": {f"mailboxIds/{ARCHIVE}": True, f"mailboxIds/{INBOX}": None},
        "M2": {f"mailboxIds/{ARCHIVE}": True, f"mailboxIds/{INBOX}": None},
    }


def test_a_move_stops_at_the_first_refusal(monkeypatch):
    transport = FakeTransport(
        reply(SESSION),
        answers(
            [
                "Email/set",
                {
                    "updated": ["M1"],
                    "notUpdated": {
                        "M2": {"type": "forbidden", "description": "locked"}
                    },
                },
                "0",
            ]
        ),
    )
    bind_jmap(monkeypatch, transport)

    result = mail_sync.move_messages(
        fastmail_account(),
        CREDENTIAL,
        INBOX,
        ["M1", "M2", "M3"],
        ARCHIVE,
        uidvalidity=None,
    )

    assert result.destination_uids == ["M1"]
    assert result.failed_index == 1
    assert isinstance(result.error, JmapError)
    assert result.error.code == "forbidden"


def test_reading_downloads_the_message_blob(monkeypatch):
    mime = b"From: beate@example.org\r\nSubject: Quartalszahlen\r\n\r\nHier sind sie"
    transport = FakeTransport(
        reply(SESSION),
        answers(
            [
                "Email/get",
                {"list": [{"id": "M1", "blobId": "Bm1blob"}], "notFound": []},
                "0",
            ]
        ),
        Response(200, mime),
    )
    bind_jmap(monkeypatch, transport)

    fetched = mail_sync.fetch_full_message(
        fastmail_account(), CREDENTIAL, INBOX, "M1", uidvalidity=None
    )

    assert fetched == mime
    download = transport.requests[-1]
    assert "Bm1blob" in download.full_url
    assert download.full_url.startswith("https://api.fastmail.com/")


def test_reading_a_deleted_message_raises_not_found(monkeypatch):
    transport = FakeTransport(
        reply(SESSION),
        answers(["Email/get", {"list": [], "notFound": ["M1"]}, "0"]),
    )
    bind_jmap(monkeypatch, transport)

    with pytest.raises(JmapError) as raised:
        mail_sync.fetch_full_message(
            fastmail_account(), CREDENTIAL, INBOX, "M1", uidvalidity=None
        )

    assert raised.value.code == "notFound"


RAW_SEND = (
    b"From: sync-tester@fastmail.com\r\n"
    b"To: beate@example.org\r\n"
    b"Subject: Antwort\r\n"
    b"Message-ID: <antwort@example.org>\r\n"
    b"\r\n"
    b"Danke\r\n"
)


def submit_script(*extra):
    """Discovery, the mailbox list, an upload and a draft, ready to submit."""
    return [
        reply(SESSION),
        answers(mailbox_list()),
        reply({"accountId": "u12345", "blobId": "Bdraft9", "size": len(RAW_SEND)}),
        answers(["Email/set", {"created": {"postcard": {"id": "Md1"}}}, "0"]),
        *extra,
    ]


def test_send_uploads_drafts_and_submits_with_an_envelope(monkeypatch):
    transport = FakeTransport(
        *submit_script(
            answers(["EmailSubmission/set", {"created": {"s1": {"id": "S1"}}}, "0"])
        )
    )
    bind_jmap(monkeypatch, transport)

    outcome = mail_sync.send_message(
        fastmail_account(),
        CREDENTIAL,
        "sync-tester@fastmail.com",
        ["Beate Beispiel <beate@example.org>", "carla@example.org"],
        RAW_SEND,
    )

    assert (outcome.filed_uid, outcome.uidvalidity, outcome.file_error) == (
        None,
        None,
        None,
    )
    calls = sent_calls(transport)
    assert [method for method, _args, _id in calls] == [
        "Mailbox/get",
        "Email/set",
        "EmailSubmission/set",
    ]
    [[_method, draft_args, _id]] = [call for call in calls if call[0] == "Email/set"]
    assert draft_args["create"] == {
        "postcard": {"blobId": "Bdraft9", "mailboxIds": {DRAFTS: True}}
    }
    [[_method, submit_args, _id]] = [
        call for call in calls if call[0] == "EmailSubmission/set"
    ]
    assert submit_args["create"]["s1"]["envelope"] == {
        "mailFrom": {"email": "sync-tester@fastmail.com"},
        "rcptTo": [{"email": "beate@example.org"}, {"email": "carla@example.org"}],
    }
    upload = next(
        request
        for request in transport.requests
        if request.data and request.full_url != FASTMAIL_API_URL
    )
    assert upload.data == RAW_SEND


def test_a_refused_submission_raises_with_the_server_code(monkeypatch):
    transport = FakeTransport(
        *submit_script(
            answers(
                [
                    "EmailSubmission/set",
                    {
                        "notCreated": {
                            "s1": {"type": "forbidden", "description": "blocked"}
                        }
                    },
                    "0",
                ]
            )
        )
    )
    bind_jmap(monkeypatch, transport)

    with pytest.raises(JmapError) as raised:
        mail_sync.send_message(
            fastmail_account(),
            CREDENTIAL,
            "sync-tester@fastmail.com",
            ["beate@example.org"],
            RAW_SEND,
        )

    assert raised.value.code == "forbidden"


def test_an_uncertain_submission_never_blind_retries(monkeypatch):
    transport = FakeTransport(
        *submit_script(
            ConnectionError("died mid-send"),
            answers(
                [
                    "Email/get",
                    {
                        "list": [{"id": "Md1", "mailboxIds": {DRAFTS: True}}],
                        "notFound": [],
                    },
                    "0",
                ]
            ),
        )
    )
    bind_jmap(monkeypatch, transport)

    with pytest.raises(JmapError) as raised:
        mail_sync.send_message(
            fastmail_account(),
            CREDENTIAL,
            "sync-tester@fastmail.com",
            ["beate@example.org"],
            RAW_SEND,
        )

    assert raised.value.code == "uncertainSubmission"
    submissions = [
        call for call in sent_calls(transport) if call[0] == "EmailSubmission/set"
    ]
    assert len(submissions) == 1


def test_an_uncertain_submission_that_reached_sent_counts_as_sent(monkeypatch):
    transport = FakeTransport(
        *submit_script(
            ConnectionError("died mid-send"),
            answers(
                [
                    "Email/get",
                    {
                        "list": [{"id": "Md1", "mailboxIds": {SENT: True}}],
                        "notFound": [],
                    },
                    "0",
                ]
            ),
        )
    )
    bind_jmap(monkeypatch, transport)

    outcome = mail_sync.send_message(
        fastmail_account(),
        CREDENTIAL,
        "sync-tester@fastmail.com",
        ["beate@example.org"],
        RAW_SEND,
    )

    assert (outcome.filed_uid, outcome.file_error) == (None, None)


def test_send_without_submission_fails_before_any_side_effect(monkeypatch):
    session = {
        **SESSION,
        "capabilities": {urn: caps for urn, caps in SESSION["capabilities"].items()},
    }
    del session["capabilities"][SUBMISSION_URN]
    transport = FakeTransport(reply(session))
    bind_jmap(monkeypatch, transport)

    with pytest.raises(JmapError) as raised:
        mail_sync.send_message(
            fastmail_account(),
            CREDENTIAL,
            "sync-tester@fastmail.com",
            ["beate@example.org"],
            RAW_SEND,
        )

    assert raised.value.code == "submissionUnsupported"
    assert sent_calls(transport) == []


def test_a_read_only_account_refuses_writes_fail_closed(monkeypatch):
    session = {
        **SESSION,
        "accounts": {"u12345": {**SESSION["accounts"]["u12345"], "isReadOnly": True}},
    }
    transport = FakeTransport(reply(session))
    bind_jmap(monkeypatch, transport)
    account = fastmail_account()

    for attempt in (
        lambda: mail_sync.set_flag(
            account, CREDENTIAL, INBOX, ["M1"], FLAG_SEEN, True, uidvalidity=None
        ),
        lambda: mail_sync.move_messages(
            account, CREDENTIAL, INBOX, ["M1"], ARCHIVE, uidvalidity=None
        ),
        lambda: mail_sync.send_message(
            account, CREDENTIAL, account.email, ["beate@example.org"], RAW_SEND
        ),
    ):
        with pytest.raises(JmapError) as raised:
            attempt()
        assert raised.value.code == "readOnly"

    assert len(transport.requests) == 1


def test_sent_copy_lookup_finds_the_message_by_id(monkeypatch):
    transport = FakeTransport(
        reply(SESSION),
        answers(
            ["Email/query", {"ids": ["Msent1"], "total": 1, "queryState": "qs-f"}, "0"]
        ),
        answers(
            [
                "Email/get",
                {
                    "list": [
                        {
                            "id": "Msent1",
                            "header:Message-ID:asText": "<antwort@example.org>",
                        }
                    ],
                    "notFound": [],
                },
                "0",
            ]
        ),
    )
    bind_jmap(monkeypatch, transport)

    found = mail_sync._sent_copy(
        fastmail_account(), CREDENTIAL, "<antwort@example.org>"
    )

    assert found == ("Msent1", None)
    [[_method, arguments, _id]] = [
        call for call in sent_calls(transport) if call[0] == "Email/query"
    ]
    assert arguments["filter"] == {"header": ["Message-ID", "<antwort@example.org>"]}


def test_sent_copy_lookup_compares_exactly_and_reads_absent_as_none(monkeypatch):
    transport = FakeTransport(
        reply(SESSION),
        answers(
            ["Email/query", {"ids": ["Msent1"], "total": 1, "queryState": "qs-f"}, "0"]
        ),
        answers(
            [
                "Email/get",
                {
                    "list": [
                        {
                            "id": "Msent1",
                            "header:Message-ID:asText": "<antwort.evil@example.org>",
                        }
                    ],
                    "notFound": [],
                },
                "0",
            ]
        ),
        answers(["Email/query", {"ids": [], "total": 0, "queryState": "qs-f"}, "0"]),
    )
    bind_jmap(monkeypatch, transport)
    account = fastmail_account()

    assert mail_sync._sent_copy(account, CREDENTIAL, "<antwort@example.org>") is None
    assert mail_sync._sent_copy(account, CREDENTIAL, "<missing@example.org>") is None


def test_a_failed_sent_copy_check_raises_rather_than_reading_absent(monkeypatch):
    transport = FakeTransport(reply(SESSION), Response(500, b"{}"))
    bind_jmap(monkeypatch, transport)

    with pytest.raises(JmapError):
        mail_sync._sent_copy(fastmail_account(), CREDENTIAL, "<antwort@example.org>")


def test_fastmail_account_syncs_reads_flags_moves_and_sends(monkeypatch):
    """One Fastmail account end to end: every operation over JMAP, with the
    IMAP/SMTP guard armed for the whole run."""
    mime = b"From: beate@example.org\r\nSubject: Quartalszahlen\r\n\r\nHier sind sie"
    transport = FakeTransport(
        *first_sync_script(
            # Read M3's body.
            answers(
                [
                    "Email/get",
                    {"list": [{"id": "M3", "blobId": "Bm3blob"}], "notFound": []},
                    "0",
                ]
            ),
            Response(200, mime),
            # Mark M3 read.
            answers(["Email/set", {"updated": ["M3"], "newState": "e2"}, "0"]),
            # Archive M2.
            answers(["Email/set", {"updated": ["M2"], "newState": "e3"}, "0"]),
            # Send a reply.
            answers(mailbox_list()),
            reply({"accountId": "u12345", "blobId": "Bdraft9", "size": len(RAW_SEND)}),
            answers(["Email/set", {"created": {"postcard": {"id": "Md1"}}}, "0"]),
            answers(["EmailSubmission/set", {"created": {"s1": {"id": "S1"}}}, "0"]),
            # Find the Sent copy by its Message-ID.
            answers(
                [
                    "Email/query",
                    {"ids": ["Msent1"], "total": 1, "queryState": "qs-f"},
                    "0",
                ]
            ),
            answers(
                [
                    "Email/get",
                    {
                        "list": [
                            {
                                "id": "Msent1",
                                "header:Message-ID:asText": "<antwort@example.org>",
                            }
                        ],
                        "notFound": [],
                    },
                    "0",
                ]
            ),
        )
    )
    bind_jmap(monkeypatch, transport)
    account = fastmail_account()

    synced = mail_sync.fetch_mailbox(account, CREDENTIAL)
    assert [message.uid for message in synced.messages] == ["M3", "M2", "M1"]

    body = mail_sync.fetch_full_message(
        account, CREDENTIAL, INBOX, "M3", uidvalidity=None
    )
    assert body == mime

    outcomes = mail_sync.set_flag(
        account, CREDENTIAL, INBOX, ["M3"], FLAG_SEEN, True, uidvalidity=None
    )
    assert [outcome.succeeded for outcome in outcomes] == [True]

    moved = mail_sync.move_messages(
        account, CREDENTIAL, INBOX, ["M2"], ARCHIVE, uidvalidity=None
    )
    assert (moved.destination_uids, moved.failed_index, moved.error) == (
        ["M2"],
        None,
        None,
    )

    outcome = mail_sync.send_message(
        account, CREDENTIAL, account.email, ["beate@example.org"], RAW_SEND
    )
    assert (outcome.filed_uid, outcome.file_error) == (None, None)

    assert mail_sync._sent_copy(account, CREDENTIAL, "<antwort@example.org>") == (
        "Msent1",
        None,
    )
    assert [method for method, _args, _id in sent_calls(transport)] == [
        "Mailbox/get",
        "Mailbox/get",
        "Email/query",
        "Email/get",
        "Email/get",
        "Email/set",
        "Email/set",
        "Mailbox/get",
        "Email/set",
        "EmailSubmission/set",
        "Email/query",
        "Email/get",
    ]
