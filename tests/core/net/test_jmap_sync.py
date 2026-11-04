"""Part 2 sync contracts, end to end over a fake transport: discovery through
delta sync, mutations, submission and push against two server shapes.

FASTMAIL_SESSION mirrors api.fastmail.com (rich capabilities, primary
accounts, submission offered); STALWART_SESSION mirrors a minimal generic
server (core+mail only, no submission, no primary accounts). Every id is
opaque, every name non-English somewhere, and the only credential is an
obviously fake one -- no production secrets may appear here.
"""

import dataclasses
import json
import logging
import threading
import urllib.parse

import pytest

from postcard.core.models.folder import FolderRole
from postcard.core.net.auth import Credential
from postcard.core.net.jmap_folders import (
    current_mailbox_state,
    list_mailboxes,
    mailbox_changes,
)
from postcard.core.net.jmap_messages import (
    SubmitRequest,
    confirm_submission,
    current_email_state,
    mark_read,
    move_emails,
    resolve_uncertain_submission,
    set_emails,
    submit_email,
    sync_email_delta,
    sync_mailbox,
    sync_mailbox_delta,
)
from postcard.core.net.jmap_session import (
    CODE_HTTP,
    CODE_NO_SUBMISSION,
    CODE_PUSH_FAILED,
    CODE_READ_ONLY,
    CORE_URN,
    MAIL_URN,
    SUBMISSION_URN,
    JmapError,
    JmapSession,
    PushConfig,
    Response,
    discover,
    is_state_mismatch,
    needs_full_sync,
)

CREDENTIAL = Credential("sync-tester", "example-secret-1")

FASTMAIL_SESSION_URL = "https://api.fastmail.com/jmap/session"
FASTMAIL_API_URL = "https://api.fastmail.com/jmap/api/"

FASTMAIL_SESSION = {
    "capabilities": {
        CORE_URN: {
            "maxCallsInRequest": 32,
            "maxObjectsInGet": 500,
            "maxObjectsInSet": 500,
        },
        MAIL_URN: {"maxMailboxesPerEmail": 1000, "emailQuerySortOptions": []},
        SUBMISSION_URN: {"maxDelayedSend": 0},
        "urn:ietf:params:jmap:vacation": {},
    },
    "accounts": {
        "u12345": {
            "name": "sync-tester@fastmail.com",
            "isPersonal": True,
            "isReadOnly": False,
            "accountCapabilities": {
                MAIL_URN: {},
                SUBMISSION_URN: {},
                "urn:ietf:params:jmap:vacation": {},
            },
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

STALWART_SESSION_URL = "https://mail.example.org/jmap/session"
STALWART_API_URL = "https://mail.example.org/jmap/api/"

STALWART_SESSION = {
    "capabilities": {
        CORE_URN: {"maxCallsInRequest": 16, "maxObjectsInGet": 100},
        MAIL_URN: {"maxMailboxesPerEmail": 10},
    },
    "accounts": {
        "alice": {
            "name": "alice@example.org",
            "isPersonal": True,
            "isReadOnly": False,
            "accountCapabilities": {MAIL_URN: {}},
        }
    },
    "primaryAccounts": {},
    "username": "alice@example.org",
    "apiUrl": STALWART_API_URL,
    "downloadUrl": "https://mail.example.org/jmap/download/{accountId}/{blobId}",
    "uploadUrl": "https://mail.example.org/jmap/upload/{accountId}/",
    "eventSourceUrl": "https://mail.example.org/jmap/eventsource/"
    "?types={types}&closeafter={closeafter}&ping={ping}",
    "state": "stalwart-state-1",
}

# Opaque server ids throughout: nothing numeric, nothing English-derived.
INBOX = "FbInboxOpaque1"
ARCHIVE = "FbArchiveOpaque9"
SENT = "FbSentOpaque3"

EMAIL = {
    "id": "Mf8e02a1",
    "threadId": "Tf71aa04",
    "from": [{"name": "Beate Beispiel", "email": "beate@example.org"}],
    "to": [{"name": "", "email": "alice@example.org"}],
    "cc": [],
    "subject": "Quartalszahlen",
    "sentAt": "2026-09-12T08:30:00Z",
    "receivedAt": "2026-09-12T08:31:00Z",
    "keywords": {},
    "preview": "Hier sind sie",
    "header:Message-ID:asText": "<q3@example.org>",
    "header:In-Reply-To:asText": "",
    "header:References:asText": "",
}


def reply(body=None, status=200, **headers):
    return Response(status, json.dumps(body).encode() if body is not None else b"")


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def answers(*responses):
    return reply({"methodResponses": list(responses)})


def fastmail_jmap(transport, writable=False):
    session = discover(
        FASTMAIL_SESSION_URL, CREDENTIAL, FakeTransport(reply(FASTMAIL_SESSION))
    )
    return JmapSession(session, CREDENTIAL, transport, writable=writable)


def stalwart_jmap(transport, session_body=None, writable=False):
    body = session_body if session_body is not None else STALWART_SESSION
    session = discover(STALWART_SESSION_URL, CREDENTIAL, FakeTransport(reply(body)))
    return JmapSession(session, CREDENTIAL, transport, writable=writable)


def sent_calls(transport):
    return [
        call
        for request in transport.requests
        for call in json.loads(request.data)["methodCalls"]
    ]


def test_initial_sync_walks_a_fastmail_mailbox():
    transport = FakeTransport(
        answers(
            ["Email/query", {"ids": ["Mf8e02a1"], "total": 1, "queryState": "s1"}, "0"]
        ),
        answers(
            [
                "Email/get",
                {"list": [EMAIL], "notFound": [], "state": "e1"},
                "0",
            ]
        ),
    )
    jmap = fastmail_jmap(transport)

    sync = sync_mailbox(jmap, INBOX)

    assert [header.uid for header in sync.headers] == ["Mf8e02a1"]
    assert sync.thread_ids == {"Mf8e02a1": "Tf71aa04"}
    assert sync.query_state == "s1"
    assert [method for method, _args, _id in sent_calls(transport)] == [
        "Email/query",
        "Email/get",
    ]


def test_delta_sync_advances_the_stored_cursor():
    transport = FakeTransport(
        answers(
            [
                "Email/queryChanges",
                {
                    "added": [{"threadId": "T", "emailId": "Mb7c19d4"}],
                    "removed": ["Mf8e02a1"],
                    "newQueryState": "s2",
                    "hasMoreChanges": False,
                },
                "0",
            ]
        ),
        answers(
            [
                "Email/get",
                {"list": [{**EMAIL, "id": "Mb7c19d4"}], "notFound": []},
                "0",
            ]
        ),
    )
    jmap = fastmail_jmap(transport)

    delta = sync_mailbox_delta(jmap, INBOX, "s1")

    assert [header.uid for header in delta.headers] == ["Mb7c19d4"]
    assert delta.removed_ids == ["Mf8e02a1"]
    assert delta.new_state == "s2"
    assert delta.is_full is False


def test_expired_state_falls_back_to_a_flagged_full_walk():
    transport = FakeTransport(
        answers(
            [
                "error",
                {"type": "cannotCalculateChanges", "description": "too old"},
                "0",
            ]
        ),
        answers(["Email/query", {"ids": ["M1"], "total": 1, "queryState": "s9"}, "0"]),
        answers(["Email/get", {"list": [{**EMAIL, "id": "M1"}], "notFound": []}, "0"]),
    )
    jmap = fastmail_jmap(transport)

    delta = sync_mailbox_delta(jmap, INBOX, "s-expired")

    assert delta.is_full is True
    assert delta.removed_ids == []
    assert delta.new_state == "s9"


def test_destroyed_emails_come_back_for_cache_deletes():
    transport = FakeTransport(
        answers(
            [
                "Email/changes",
                {
                    "created": [],
                    "updated": ["Mb7c19d4"],
                    "destroyed": ["Mf8e02a1"],
                    "newState": "e2",
                    "hasMoreChanges": False,
                },
                "0",
            ]
        ),
        answers(
            [
                "Email/get",
                {"list": [{**EMAIL, "id": "Mb7c19d4"}], "notFound": []},
                "0",
            ]
        ),
    )
    jmap = stalwart_jmap(transport)

    delta = sync_email_delta(jmap, "e1")

    assert delta.destroyed_ids == ["Mf8e02a1"]
    assert [header.uid for header in delta.headers] == ["Mb7c19d4"]
    assert delta.new_state == "e2"


def test_mailbox_changes_cover_created_updated_and_destroyed():
    transport = FakeTransport(
        answers(
            [
                "Mailbox/changes",
                {
                    "created": ["FbNewOpaque5"],
                    "updated": [INBOX],
                    "destroyed": ["FbOldOpaque0"],
                    "newState": "b2",
                    "hasMoreChanges": False,
                },
                "0",
            ]
        ),
        answers(
            [
                "Mailbox/get",
                {
                    "list": [
                        {
                            "id": "FbNewOpaque5",
                            "name": "Neu",
                            "parentId": None,
                            "role": None,
                            "unreadEmails": 1,
                            "totalEmails": 1,
                            "sortOrder": 0,
                        },
                        {
                            "id": INBOX,
                            "name": "Posteingang",
                            "parentId": None,
                            "role": "inbox",
                            "unreadEmails": 7,
                            "totalEmails": 42,
                            "sortOrder": 0,
                        },
                    ]
                },
                "0",
            ]
        ),
    )
    jmap = stalwart_jmap(transport)

    delta = mailbox_changes(jmap, "b1")

    assert [(m.id, m.role) for m in delta.changed] == [
        ("FbNewOpaque5", FolderRole.OTHER),
        (INBOX, FolderRole.INBOX),
    ]
    assert delta.destroyed_ids == ["FbOldOpaque0"]
    assert delta.new_state == "b2"


def test_archive_moves_mailbox_membership_over_the_wire():
    transport = FakeTransport(
        answers(["Email/set", {"updated": ["Mf8e02a1"], "newState": "e2"}, "0"])
    )
    jmap = fastmail_jmap(transport, writable=True)

    result = move_emails(jmap, ["Mf8e02a1"], [ARCHIVE], [INBOX])

    assert result.updated == ["Mf8e02a1"]
    [[method, arguments, _id]] = sent_calls(transport)
    assert method == "Email/set"
    assert arguments["update"] == {
        "Mf8e02a1": {f"mailboxIds/{ARCHIVE}": True, f"mailboxIds/{INBOX}": None}
    }


def test_partial_mutation_applies_siblings_and_reports_refusals():
    transport = FakeTransport(
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
        )
    )
    jmap = fastmail_jmap(transport, writable=True)

    result = mark_read(jmap, ["M1", "M2"], True)

    assert result.updated == ["M1"]
    assert result.errors["M2"].code == "forbidden"


def test_state_mismatch_rereads_then_rebuilds():
    transport = FakeTransport(
        answers(["error", {"type": "stateMismatch", "description": "moved"}, "0"]),
        answers(["Email/get", {"list": [], "state": "e9"}, "0"]),
        answers(["Email/set", {"updated": ["M1"], "newState": "e10"}, "0"]),
    )
    jmap = fastmail_jmap(transport, writable=True)

    try:
        set_emails(jmap, {"M1": {"keywords/$seen": True}}, None, "e-old")
        raise AssertionError("expected stateMismatch")
    except JmapError as error:
        assert is_state_mismatch(error) is True
    state = current_email_state(jmap)
    result = set_emails(jmap, {"M1": {"keywords/$seen": True}}, None, state)

    assert result.updated == ["M1"]
    assert len(transport.requests) == 3


def test_submission_succeeds_and_confirms():
    transport = FakeTransport(
        answers(["EmailSubmission/set", {"created": {"s1": {"id": "S1"}}}, "0"]),
        answers(
            [
                "EmailSubmission/get",
                {
                    "list": [
                        {
                            "id": "S1",
                            "emailId": "MdRaFt01",
                            "sendAt": "2026-09-12T08:30:00Z",
                        }
                    ]
                },
                "0",
            ]
        ),
    )
    jmap = fastmail_jmap(transport, writable=True)

    submitted = submit_email(jmap, SubmitRequest("MdRaFt01"))
    stored = confirm_submission(jmap, submitted.submission_id or "")

    assert submitted.submission_id == "S1"
    assert submitted.is_uncertain is False
    assert stored is not None and stored["sendAt"] == "2026-09-12T08:30:00Z"


def test_uncertain_submission_never_resends_itself():
    posted = []

    def dropping(request):
        posted.append(request)
        raise ConnectionError("died mid-send")

    jmap = fastmail_jmap(dropping, writable=True)

    result = submit_email(jmap, SubmitRequest("MdRaFt01"))

    assert result.is_uncertain is True
    assert len(posted) == 1

    transport = FakeTransport(
        answers(
            [
                "Email/get",
                {
                    "list": [{"id": "MdRaFt01", "mailboxIds": {"FbDrafts1": True}}],
                    "notFound": [],
                },
                "0",
            ]
        )
    )
    settling = resolve_uncertain_submission(fastmail_jmap(transport), "MdRaFt01", SENT)

    assert settling is False


def test_blobs_upload_and_download_on_the_session_origin():
    mime = b"From: a@example.org\r\n\r\nbody"
    transport = FakeTransport(
        reply({"accountId": "u12345", "blobId": "B7", "size": len(mime)}),
        Response(200, mime),
    )
    jmap = fastmail_jmap(transport, writable=True)

    blob_id = jmap.upload_blob(mime, "message/rfc822")
    fetched = jmap.download_blob(blob_id, "m.eml")

    assert blob_id == "B7"
    assert fetched == mime
    upload_request, download_request = transport.requests
    assert (
        urllib.parse.urlsplit(upload_request.full_url).netloc
        == urllib.parse.urlsplit(FASTMAIL_API_URL).netloc
        == urllib.parse.urlsplit(download_request.full_url).netloc
    )
    assert upload_request.get_header("Authorization") == download_request.get_header(
        "Authorization"
    )


def test_cancellation_stops_a_sync_without_a_cursor():
    inner = FakeTransport(
        answers(["Email/query", {"ids": ["M1"], "total": 1, "queryState": "s1"}, "0"]),
    )
    posted = []
    cancelled = threading.Event()

    def cancelling(request):
        posted.append(request)
        response = inner(request)
        cancelled.set()
        return response

    jmap = fastmail_jmap(cancelling)

    with pytest.raises(JmapError) as raised:
        sync_mailbox(jmap, INBOX, cancelled=cancelled)

    assert "cancelled" in raised.value.code
    assert len(posted) == 1


def test_read_only_account_refuses_writes_but_reads():
    body = {
        **STALWART_SESSION,
        "accounts": {
            "alice": {
                "name": "alice@example.org",
                "isPersonal": True,
                "isReadOnly": True,
                "accountCapabilities": {MAIL_URN: {}},
            }
        },
    }
    read_transport = FakeTransport(
        answers(["Mailbox/get", {"list": [], "state": "b1"}, "0"])
    )
    jmap = stalwart_jmap(read_transport, session_body=body, writable=True)

    assert current_mailbox_state(jmap) == "b1"

    write_transport = FakeTransport()
    writer = stalwart_jmap(write_transport, session_body=body, writable=True)
    with pytest.raises(JmapError) as raised:
        set_emails(writer, {"M1": {"keywords/$seen": True}})

    assert raised.value.code == CODE_READ_ONLY
    assert write_transport.requests == []


def test_server_without_submission_refuses_submit():
    transport = FakeTransport()
    jmap = stalwart_jmap(transport, writable=True)

    with pytest.raises(JmapError) as raised:
        submit_email(jmap, SubmitRequest("M9"))

    assert raised.value.code == CODE_NO_SUBMISSION
    assert transport.requests == []


def test_push_exhaustion_signals_the_polling_fallback():
    jmap = fastmail_jmap(FakeTransport())

    def down(_request, _cancelled):
        raise ConnectionError("no route")

    with pytest.raises(JmapError) as raised:
        jmap.watch_push(down, lambda _c: None, None, PushConfig(sleep=lambda _s: None))

    assert raised.value.code == CODE_PUSH_FAILED


def test_non_english_mailboxes_classify_by_role_not_name():
    transport = FakeTransport(
        answers(
            [
                "Mailbox/get",
                {
                    "list": [
                        {
                            "id": INBOX,
                            "name": "Posteingang",
                            "parentId": None,
                            "role": "inbox",
                            "unreadEmails": 3,
                            "totalEmails": 40,
                            "sortOrder": 0,
                        },
                        {
                            "id": "FbSentFake8",
                            "name": "Gesendete Elemente",
                            "parentId": None,
                            "role": "sent",
                            "unreadEmails": 0,
                            "totalEmails": 12,
                            "sortOrder": 0,
                        },
                    ]
                },
                "0",
            ]
        )
    )
    jmap = fastmail_jmap(transport)

    roles = {mailbox.id: mailbox.role for mailbox in list_mailboxes(jmap)}

    assert roles == {INBOX: FolderRole.INBOX, "FbSentFake8": FolderRole.SENT}


def test_failures_are_never_authoritative_empties():
    transport = FakeTransport(Response(500, b"{}"))
    jmap = stalwart_jmap(transport)

    with pytest.raises(JmapError) as raised:
        sync_email_delta(jmap, "e1")

    assert raised.value.status == 500
    assert needs_full_sync(raised.value) is False


def test_no_credential_secret_leaves_in_bodies_errors_or_logs(caplog):
    transport = FakeTransport(
        answers(["Email/query", {"ids": [], "total": 0, "queryState": "s1"}, "0"]),
        Response(200, b"<html>not json</html>"),
    )
    jmap = fastmail_jmap(transport, writable=True)

    with caplog.at_level(logging.DEBUG, logger="postcard.core.net.jmap_session"):
        sync_mailbox(jmap, INBOX)
        try:
            set_emails(jmap, {"M1": {"keywords/$seen": True}})
        except JmapError as error:
            failure = str(error)

    bodies = "".join(
        request.data.decode("utf-8", "replace")
        for request in transport.requests
        if request.data
    )
    assert "example-secret-1" not in bodies
    assert "example-secret-1" not in failure
    assert "example-secret-1" not in caplog.text


def test_tampered_blob_template_is_refused_before_credentials_go_out():
    session = discover(
        STALWART_SESSION_URL, CREDENTIAL, FakeTransport(reply(STALWART_SESSION))
    )
    tampered = dataclasses.replace(
        session,
        download_template="https://cdn.example.net/dl/{accountId}/{blobId}",
    )
    transport = FakeTransport()
    jmap = JmapSession(tampered, CREDENTIAL, transport)

    with pytest.raises(JmapError) as raised:
        jmap.download_blob("B1", "n.txt")

    assert "cdn.example.net" in raised.value.message
    assert transport.requests == []


def test_http_error_vocabulary_stays_fixed():
    transport = FakeTransport(Response(500, b"{}"))
    jmap = fastmail_jmap(transport)

    with pytest.raises(JmapError) as raised:
        current_email_state(jmap)

    assert (raised.value.status, raised.value.code) == (500, CODE_HTTP)
