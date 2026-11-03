import json
import threading

import pytest

from postcard.core.net.auth import Credential
from postcard.core.net.jmap_messages import (
    fetch_emails,
    message_header,
    query_all_ids,
    query_changes,
    query_page,
    sync_mailbox,
    thread_ids,
)
from postcard.core.net.jmap_session import (
    CODE_CANCELLED,
    JmapAnswer,
    JmapError,
    JmapSession,
    Response,
    discover,
)
from postcard.core.threader import NO_SUBJECT

ACCOUNT = "A1"
MAILBOX = "m-inbox"


def person(name, email):
    return {"name": name, "email": email}


EMAIL = {
    "id": "M1",
    "threadId": "T1",
    "from": [person("Ada Lovelace", "Ada@Example.com")],
    "to": [person("", "grace@example.com"), person("Bob", "bob@example.com")],
    "cc": [person("Carol", "carol@example.com")],
    "subject": "Quarterly numbers",
    "sentAt": "2026-09-12T08:30:00Z",
    "receivedAt": "2026-09-12T08:31:00Z",
    "keywords": {"$seen": True, "$flagged": True},
    "preview": "Here they\r\n   are",
    "header:Message-ID:asText": "<m1@example.com>",
    "header:In-Reply-To:asText": "<m0@example.com>",
    "header:References:asText": "<r@example.com> <m0@example.com>",
}


def test_a_jmap_message_maps_onto_the_header_a_sync_stores():
    header = message_header(EMAIL)

    assert header.uid == "M1"
    assert (header.sender, header.sender_address) == (
        "Ada Lovelace",
        "ada@example.com",
    )
    assert (header.recipient, header.recipient_address) == (
        "grace@example.com",
        "grace@example.com",
    )
    assert header.date == "2026-09-12T08:30:00+00:00"
    assert header.is_unread is False
    assert header.is_starred is True
    assert header.preview == "Here they are"
    assert header.message_id == "<m1@example.com>"
    assert header.in_reply_to == "<m0@example.com>"
    assert header.references == "<r@example.com> <m0@example.com>"
    assert ("Carol", "carol@example.com") in header.addresses


def test_a_sparse_message_still_maps():
    header = message_header({"id": "M9"})

    assert header.subject == NO_SUBJECT
    assert header.sender == ""
    assert header.is_unread is True
    assert header.is_starred is False
    assert header.date == ""


def test_thread_ids_map_each_message_to_its_server_thread():
    assert thread_ids([EMAIL, {"id": "M2", "threadId": "T1"}, {"id": "M3"}]) == {
        "M1": "T1",
        "M2": "T1",
    }


class FakeJmap:
    def __init__(self, *answers, max_objects_in_get=500):
        self._answers = list(answers)
        self.account_id = ACCOUNT
        self.max_objects_in_get = max_objects_in_get
        self.calls = []
        self.on_request = None

    def request(self, calls, cancelled=None):
        self.calls.append(calls)
        if self.on_request is not None:
            self.on_request(len(self.calls))
        return [self._answers.pop(0) for _ in calls]


def query_answer(ids, total, state="s1"):
    return JmapAnswer(
        "Email/query", {"ids": ids, "total": total, "queryState": state}, "0"
    )


def changes_answer(added, removed, state, has_more=False):
    return JmapAnswer(
        "Email/queryChanges",
        {
            "added": [{"threadId": "T", "emailId": id} for id in added],
            "removed": removed,
            "newQueryState": state,
            "hasMoreChanges": has_more,
        },
        "0",
    )


def get_answer(items, not_found=()):
    return JmapAnswer("Email/get", {"list": items, "notFound": list(not_found)}, "0")


def method_error(type="accountNotFound"):
    return JmapAnswer("error", {"type": type, "description": type}, "0")


def test_a_query_page_asks_newest_first_with_a_bounded_window():
    jmap = FakeJmap(query_answer(["M1"], 1))

    page = query_page(jmap, MAILBOX, 0, 500)  # type: ignore[arg-type]

    assert page.ids == ("M1",)
    [[call]] = jmap.calls
    assert call.method == "Email/query"
    assert call.arguments["filter"] == {"inMailbox": MAILBOX}
    assert call.arguments["sort"] == [{"property": "receivedAt", "isAscending": False}]
    assert (call.arguments["position"], call.arguments["limit"]) == (0, 500)


def test_a_walk_pages_to_the_end_and_keeps_the_state():
    jmap = FakeJmap(
        query_answer([f"M{i}" for i in range(500)], 502, "s1"),
        query_answer(["M500", "M501"], 502, "s2"),
    )

    result = query_all_ids(jmap, MAILBOX)  # type: ignore[arg-type]

    assert len(result.ids) == 502
    assert result.query_state == "s2"
    assert result.removed_ids == []
    positions = [calls[0].arguments["position"] for calls in jmap.calls]
    assert positions == [0, 500]


def test_changes_since_a_stored_state_return_added_and_removed():
    jmap = FakeJmap(changes_answer(["M3"], ["M0"], "s2"))

    result = query_all_ids(jmap, MAILBOX, since_state="s1")  # type: ignore[arg-type]

    assert (result.ids, result.removed_ids, result.query_state) == (
        ["M3"],
        ["M0"],
        "s2",
    )
    [[call]] = jmap.calls
    assert call.arguments["sinceQueryState"] == "s1"
    assert call.arguments["collapseThreads"] is False


def test_changes_follow_has_more_changes():
    jmap = FakeJmap(
        changes_answer(["M3"], [], "s2", has_more=True),
        changes_answer(["M4"], ["M0"], "s3"),
    )

    result = query_changes(jmap, MAILBOX, "s1")  # type: ignore[arg-type]

    assert (result.ids, result.removed_ids, result.query_state) == (
        ["M3", "M4"],
        ["M0"],
        "s3",
    )


def test_an_expired_state_walks_the_mailbox_over_instead_of_failing():
    jmap = FakeJmap(
        method_error("cannotCalculateChanges"),
        query_answer(["M1", "M2"], 2, "s9"),
    )

    result = query_all_ids(jmap, MAILBOX, since_state="s-old")  # type: ignore[arg-type]

    assert (result.ids, result.removed_ids, result.query_state) == (
        ["M1", "M2"],
        [],
        "s9",
    )


def test_a_refused_query_is_raised_not_retried_as_a_walk():
    jmap = FakeJmap(method_error("accountNotFound"))

    with pytest.raises(JmapError) as raised:
        query_all_ids(jmap, MAILBOX, since_state="s1")  # type: ignore[arg-type]

    assert raised.value.code == "accountNotFound"
    assert len(jmap.calls) == 1


def test_get_batches_follow_the_server_s_max_objects_in_get():
    emails = [{**EMAIL, "id": f"M{i}"} for i in range(5)]
    jmap = FakeJmap(
        get_answer(emails[:2]),
        get_answer(emails[2:4]),
        get_answer(emails[4:]),
        max_objects_in_get=2,
    )

    found, missing = fetch_emails(jmap, [f"M{i}" for i in range(5)])  # type: ignore[arg-type]

    assert [item["id"] for item in found] == [f"M{i}" for i in range(5)]
    assert missing == []
    assert [calls[0].arguments["ids"] for calls in jmap.calls] == [
        ["M0", "M1"],
        ["M2", "M3"],
        ["M4"],
    ]


def test_ids_the_server_no_longer_has_are_reported_not_failing():
    jmap = FakeJmap(get_answer([{**EMAIL, "id": "M1"}], not_found=["M2"]))

    found, missing = fetch_emails(jmap, ["M1", "M2"])  # type: ignore[arg-type]

    assert [item["id"] for item in found] == ["M1"]
    assert missing == ["M2"]


def test_cancellation_stops_a_walk_between_pages():
    jmap = FakeJmap(
        query_answer([f"M{i}" for i in range(500)], 600, "s1"),
        query_answer(["M500"], 600, "s2"),
    )
    cancelled = threading.Event()
    jmap.on_request = lambda count: cancelled.set() if count == 1 else None

    with pytest.raises(JmapError) as raised:
        query_all_ids(jmap, MAILBOX, cancelled=cancelled)  # type: ignore[arg-type]

    assert raised.value.code == CODE_CANCELLED
    assert len(jmap.calls) == 1


SESSION_URL = "https://jmap.example.com/.well-known/jmap"
API_URL = "https://jmap.example.com/jmap/api/"

SESSION_JSON = {
    "capabilities": {
        "urn:ietf:params:jmap:core": {
            "maxCallsInRequest": 16,
            "maxObjectsInGet": 500,
        },
        "urn:ietf:params:jmap:mail": {},
    },
    "accounts": {"A1": {"accountCapabilities": {"urn:ietf:params:jmap:mail": {}}}},
    "primaryAccounts": {"urn:ietf:params:jmap:mail": "A1"},
    "apiUrl": API_URL,
    "downloadUrl": "https://jmap.example.com/dl/{accountId}/{blobId}",
    "uploadUrl": "https://jmap.example.com/up/{accountId}/",
    "eventSourceUrl": "https://jmap.example.com/es/"
    "?types={types}&closeafter={closeafter}&ping={ping}",
}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def http(body):
    return Response(200, json.dumps(body).encode())


def test_an_initial_sync_reads_a_mailbox_end_to_end():
    transport = FakeTransport(
        http(SESSION_JSON),
        http(
            {
                "methodResponses": [
                    [
                        "Email/query",
                        {"ids": ["M1", "M2"], "total": 2, "queryState": "s1"},
                        "0",
                    ]
                ]
            }
        ),
        http(
            {
                "methodResponses": [
                    [
                        "Email/get",
                        {
                            "list": [EMAIL, {**EMAIL, "id": "M2", "threadId": "T2"}],
                            "notFound": [],
                        },
                        "0",
                    ]
                ]
            }
        ),
    )
    credential = Credential("ada@example.com", "hunter2")
    jmap = JmapSession(
        discover(SESSION_URL, credential, transport), credential, transport
    )

    sync = sync_mailbox(jmap, MAILBOX)

    assert [header.uid for header in sync.headers] == ["M1", "M2"]
    assert sync.thread_ids == {"M1": "T1", "M2": "T2"}
    assert sync.query_state == "s1"
    assert sync.not_found_ids == []
    methods = [
        call[0]
        for request in transport.requests[1:]
        for call in json.loads(request.data)["methodCalls"]
    ]
    assert methods == ["Email/query", "Email/get"]
