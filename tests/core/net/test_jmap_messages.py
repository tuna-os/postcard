import json
import threading

import pytest

from postcard.core.net.auth import Credential
from postcard.core.net.jmap_messages import (
    SubmitRequest,
    confirm_submission,
    create_emails,
    current_email_state,
    email_changes,
    fetch_emails,
    mark_read,
    mark_starred,
    message_header,
    move_emails,
    query_all_ids,
    query_changes,
    query_page,
    resolve_uncertain_submission,
    set_emails,
    set_keywords,
    submit_email,
    sync_email_delta,
    sync_mailbox,
    sync_mailbox_delta,
    thread_ids,
    was_submitted,
)
from postcard.core.net.jmap_session import (
    CODE_CANCELLED,
    CODE_HTTP,
    CODE_INVALID_ARGUMENTS,
    CODE_NO_SUBMISSION,
    JmapAnswer,
    JmapError,
    JmapSession,
    Response,
    discover,
    is_state_mismatch,
    needs_full_sync,
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
    def __init__(
        self,
        *answers,
        max_objects_in_get=500,
        max_objects_in_set=500,
        submission_supported=True,
    ):
        self._answers = list(answers)
        self.account_id = ACCOUNT
        self.max_objects_in_get = max_objects_in_get
        self.max_objects_in_set = max_objects_in_set
        self.submission_supported = submission_supported
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


def email_changes_answer(created, updated, destroyed, state, has_more=False):
    return JmapAnswer(
        "Email/changes",
        {
            "created": created,
            "updated": updated,
            "destroyed": destroyed,
            "newState": state,
            "hasMoreChanges": has_more,
        },
        "0",
    )


def set_answer(payload):
    return JmapAnswer("Email/set", payload, "0")


def submission_answer(payload):
    return JmapAnswer("EmailSubmission/set", payload, "0")


def test_current_email_state_reads_the_state_alone():
    jmap = FakeJmap(JmapAnswer("Email/get", {"list": [], "state": "e9"}, "0"))

    assert current_email_state(jmap) == "e9"  # type: ignore[arg-type]
    [[call]] = jmap.calls
    assert call.arguments["ids"] == []


def test_current_email_state_without_a_state_is_refused():
    jmap = FakeJmap(JmapAnswer("Email/get", {"list": []}, "0"))

    with pytest.raises(JmapError) as raised:
        current_email_state(jmap)  # type: ignore[arg-type]

    assert raised.value.code == CODE_HTTP


def test_email_changes_follow_has_more_changes():
    jmap = FakeJmap(
        email_changes_answer(["M1"], [], [], "e2", has_more=True),
        email_changes_answer([], ["M2"], ["M0"], "e3"),
    )

    changes = email_changes(jmap, "e1")  # type: ignore[arg-type]

    assert (changes.created, changes.updated, changes.destroyed) == (
        ["M1"],
        ["M2"],
        ["M0"],
    )
    assert changes.new_state == "e3"
    [[first], [second]] = jmap.calls
    assert first.arguments["sinceState"] == "e1"
    assert second.arguments["sinceState"] == "e2"


def test_an_expired_email_state_raises_for_a_full_walk():
    jmap = FakeJmap(method_error("cannotCalculateChanges"))

    with pytest.raises(JmapError) as raised:
        email_changes(jmap, "e-old")  # type: ignore[arg-type]

    assert needs_full_sync(raised.value) is True


def test_a_delta_pass_fetches_changed_and_reports_destroyed():
    jmap = FakeJmap(
        email_changes_answer(["M1"], ["M2"], ["M0"], "e2"),
        get_answer([{**EMAIL, "id": "M1"}, {**EMAIL, "id": "M2", "threadId": "T2"}]),
    )

    delta = sync_email_delta(jmap, "e1")  # type: ignore[arg-type]

    assert [header.uid for header in delta.headers] == ["M1", "M2"]
    assert delta.thread_ids == {"M1": "T1", "M2": "T2"}
    assert delta.destroyed_ids == ["M0"]
    assert delta.new_state == "e2"


def test_a_mailbox_delta_reports_changes_without_a_full_flag():
    jmap = FakeJmap(
        changes_answer(["M3"], ["M0"], "s2"),
        get_answer([{**EMAIL, "id": "M3"}]),
    )

    delta = sync_mailbox_delta(jmap, MAILBOX, "s1")  # type: ignore[arg-type]

    assert [header.uid for header in delta.headers] == ["M3"]
    assert delta.removed_ids == ["M0"]
    assert delta.new_state == "s2"
    assert delta.is_full is False


def test_a_mailbox_delta_with_an_expired_state_walks_and_flags_full():
    jmap = FakeJmap(
        method_error("cannotCalculateChanges"),
        query_answer(["M1", "M2"], 2, "s9"),
        get_answer([{**EMAIL, "id": "M1"}, {**EMAIL, "id": "M2"}]),
    )

    delta = sync_mailbox_delta(jmap, MAILBOX, "s-old")  # type: ignore[arg-type]

    assert [header.uid for header in delta.headers] == ["M1", "M2"]
    assert delta.removed_ids == []
    assert delta.new_state == "s9"
    assert delta.is_full is True


def test_a_mailbox_delta_refusal_is_raised_not_walked():
    jmap = FakeJmap(method_error("accountNotFound"))

    with pytest.raises(JmapError) as raised:
        sync_mailbox_delta(jmap, MAILBOX, "s1")  # type: ignore[arg-type]

    assert raised.value.code == "accountNotFound"
    assert len(jmap.calls) == 1


def test_set_emails_patches_and_reports_what_applied():
    jmap = FakeJmap(set_answer({"updated": ["M1"], "newState": "e2"}))

    result = set_emails(jmap, {"M1": {"keywords/$seen": True}})  # type: ignore[arg-type]

    assert result.updated == ["M1"]
    assert result.errors == {}
    [[call]] = jmap.calls
    assert call.method == "Email/set"
    assert call.arguments["accountId"] == ACCOUNT
    assert call.arguments["update"] == {"M1": {"keywords/$seen": True}}


def test_set_emails_destroys_and_carries_if_in_state():
    jmap = FakeJmap(set_answer({"destroyed": ["M1"]}))

    result = set_emails(jmap, None, ["M1"], "e1")  # type: ignore[arg-type]

    assert result.destroyed == ["M1"]
    [[call]] = jmap.calls
    assert call.arguments["destroy"] == ["M1"]
    assert call.arguments["ifInState"] == "e1"


def test_set_emails_chunks_on_the_server_limit():
    updates = {f"M{i}": {"keywords/$seen": True} for i in range(3)}
    jmap = FakeJmap(
        set_answer({"updated": ["M0"]}),
        set_answer({"updated": ["M1"]}),
        set_answer({"updated": ["M2"]}),
        max_objects_in_set=1,
    )

    result = set_emails(jmap, updates)  # type: ignore[arg-type]

    assert result.updated == ["M0", "M1", "M2"]
    assert len(jmap.calls) == 3


def test_set_emails_returns_partial_failures_without_raising():
    jmap = FakeJmap(
        set_answer(
            {
                "updated": ["M1"],
                "notUpdated": {"M2": {"type": "forbidden", "description": "no"}},
            }
        )
    )

    result = set_emails(  # type: ignore[arg-type]
        jmap, {"M1": {"keywords/$seen": True}, "M2": {"keywords/$seen": True}}
    )

    assert result.updated == ["M1"]
    assert result.errors["M2"].code == "forbidden"


def test_set_emails_with_nothing_to_do_is_refused():
    jmap = FakeJmap()

    with pytest.raises(JmapError) as raised:
        set_emails(jmap)  # type: ignore[arg-type]

    assert raised.value.code == CODE_INVALID_ARGUMENTS
    assert jmap.calls == []


def test_a_state_mismatch_raises_for_a_rebuilt_change():
    jmap = FakeJmap(method_error("stateMismatch"))

    with pytest.raises(JmapError) as raised:
        set_emails(jmap, {"M1": {"keywords/$seen": True}}, None, "e-old")  # type: ignore[arg-type]

    assert is_state_mismatch(raised.value) is True


def test_create_emails_returns_the_server_echo_per_creation_id():
    jmap = FakeJmap(set_answer({"created": {"c1": {"id": "M9", "threadId": "T9"}}}))

    result = create_emails(jmap, {"c1": {"mailboxIds": {"m": True}}})  # type: ignore[arg-type]

    assert result.created == {"c1": {"id": "M9", "threadId": "T9"}}


def test_create_emails_with_nothing_to_do_is_refused():
    jmap = FakeJmap()

    with pytest.raises(JmapError, match="needs a create"):
        create_emails(jmap, {})  # type: ignore[arg-type]


def test_set_keywords_patches_add_and_remove():
    jmap = FakeJmap(set_answer({"updated": ["M1", "M2"]}))

    set_keywords(jmap, ["M1", "M2"], ("$seen",), ("$flagged",))  # type: ignore[arg-type]

    [[call]] = jmap.calls
    assert call.arguments["update"] == {
        "M1": {"keywords/$seen": True, "keywords/$flagged": None},
        "M2": {"keywords/$seen": True, "keywords/$flagged": None},
    }


def test_set_keywords_without_ids_sends_nothing():
    jmap = FakeJmap()

    result = set_keywords(jmap, [], ("$seen",))  # type: ignore[arg-type]

    assert result.updated == [] and result.errors == {}
    assert jmap.calls == []


def test_set_keywords_without_keywords_is_refused():
    jmap = FakeJmap()

    with pytest.raises(JmapError, match="needs a keyword"):
        set_keywords(jmap, ["M1"])  # type: ignore[arg-type]


def test_mark_read_and_starred_use_the_right_keywords():
    jmap = FakeJmap(
        set_answer({"updated": ["M1"]}),
        set_answer({"updated": ["M1"]}),
        set_answer({"updated": ["M1"]}),
        set_answer({"updated": ["M1"]}),
    )

    mark_read(jmap, ["M1"], True)  # type: ignore[arg-type]
    mark_read(jmap, ["M1"], False)  # type: ignore[arg-type]
    mark_starred(jmap, ["M1"], True)  # type: ignore[arg-type]
    mark_starred(jmap, ["M1"], False)  # type: ignore[arg-type]

    patches = [calls[0].arguments["update"]["M1"] for calls in jmap.calls]
    assert patches == [
        {"keywords/$seen": True},
        {"keywords/$seen": None},
        {"keywords/$flagged": True},
        {"keywords/$flagged": None},
    ]


def test_move_emails_patches_mailbox_membership():
    jmap = FakeJmap(set_answer({"updated": ["M1"]}))

    move_emails(jmap, ["M1"], ["m-archive"], ["m-inbox"])  # type: ignore[arg-type]

    [[call]] = jmap.calls
    assert call.arguments["update"] == {
        "M1": {"mailboxIds/m-archive": True, "mailboxIds/m-inbox": None}
    }


def test_move_emails_escapes_opaque_mailbox_ids():
    jmap = FakeJmap(set_answer({"updated": ["M1"]}))

    move_emails(jmap, ["M1"], ["a/b~c"], [])  # type: ignore[arg-type]

    [[call]] = jmap.calls
    assert call.arguments["update"] == {"M1": {"mailboxIds/a~1b~0c": True}}


def test_move_emails_without_a_mailbox_is_refused():
    jmap = FakeJmap()

    with pytest.raises(JmapError, match="needs a mailbox"):
        move_emails(jmap, ["M1"], [], [])  # type: ignore[arg-type]


def test_move_emails_without_ids_sends_nothing():
    jmap = FakeJmap()

    result = move_emails(jmap, [], ["m-archive"], [])  # type: ignore[arg-type]

    assert result.updated == []
    assert jmap.calls == []


def test_submit_sends_the_draft_and_returns_its_handle():
    jmap = FakeJmap(submission_answer({"created": {"s1": {"id": "S1"}}}))

    result = submit_email(  # type: ignore[arg-type]
        jmap,
        SubmitRequest("M9", on_success_update={"mailboxIds": {"m-sent": True}}),
    )

    assert (result.submission_id, result.error, result.is_uncertain) == (
        "S1",
        None,
        False,
    )
    [[call]] = jmap.calls
    assert call.method == "EmailSubmission/set"
    assert call.arguments["create"]["s1"]["emailId"] == "M9"
    assert call.arguments["create"]["s1"]["onSuccessUpdateEmail"] == {
        "mailboxIds": {"m-sent": True}
    }


def test_a_refused_submission_comes_back_as_an_error():
    jmap = FakeJmap(
        submission_answer(
            {"notCreated": {"s1": {"type": "forbidden", "description": "no"}}}
        )
    )

    result = submit_email(jmap, SubmitRequest("M9"))  # type: ignore[arg-type]

    assert result.submission_id is None
    assert result.error is not None and result.error.code == "forbidden"
    assert result.is_uncertain is False


def test_a_lost_submission_answer_is_uncertain_not_retried():
    class DroppingJmap(FakeJmap):
        def request(self, calls, cancelled=None):
            self.calls.append(calls)
            raise ConnectionError("died mid-send")

    jmap = DroppingJmap()

    result = submit_email(jmap, SubmitRequest("M9"))  # type: ignore[arg-type]

    assert result.is_uncertain is True
    assert result.submission_id is None
    assert "died mid-send" in result.cause
    assert len(jmap.calls) == 1


def test_a_gateway_failure_during_submit_is_uncertain():
    class FailingJmap(FakeJmap):
        def request(self, calls, cancelled=None):
            self.calls.append(calls)
            raise JmapError(503, CODE_HTTP, "the JMAP request failed: HTTP 503")

    jmap = FailingJmap()

    result = submit_email(jmap, SubmitRequest("M9"))  # type: ignore[arg-type]

    assert result.is_uncertain is True


def test_a_client_failure_during_submit_is_raised():
    class RefusingJmap(FakeJmap):
        def request(self, calls, cancelled=None):
            self.calls.append(calls)
            raise JmapError(400, CODE_HTTP, "the JMAP request failed: HTTP 400")

    jmap = RefusingJmap()

    with pytest.raises(JmapError) as raised:
        submit_email(jmap, SubmitRequest("M9"))  # type: ignore[arg-type]

    assert raised.value.status == 400


def test_a_method_error_during_submit_is_raised():
    jmap = FakeJmap(method_error("invalidArguments"))

    with pytest.raises(JmapError) as raised:
        submit_email(jmap, SubmitRequest("M9"))  # type: ignore[arg-type]

    assert raised.value.code == "invalidArguments"


def test_submit_without_the_capability_is_refused():
    jmap = FakeJmap(submission_supported=False)

    with pytest.raises(JmapError) as raised:
        submit_email(jmap, SubmitRequest("M9"))  # type: ignore[arg-type]

    assert raised.value.code == CODE_NO_SUBMISSION
    assert jmap.calls == []


def test_confirm_submission_returns_the_stored_object():
    payload = {"id": "S1", "emailId": "M9", "sendAt": "2026-09-12T08:30:00Z"}
    jmap = FakeJmap(JmapAnswer("EmailSubmission/get", {"list": [payload]}, "0"))

    assert confirm_submission(jmap, "S1") == payload  # type: ignore[arg-type]


def test_confirm_submission_is_none_when_gone():
    jmap = FakeJmap(JmapAnswer("EmailSubmission/get", {"list": []}, "0"))

    assert confirm_submission(jmap, "S9") is None  # type: ignore[arg-type]


def test_was_submitted_reads_mailbox_membership():
    assert was_submitted({"mailboxIds": {"m-sent": True}}, "m-sent") is True
    assert was_submitted({"mailboxIds": {"m-drafts": True}}, "m-sent") is False
    assert was_submitted({}, "m-sent") is False


def test_an_uncertain_submission_in_sent_is_settled_sent():
    jmap = FakeJmap(
        get_answer([{"id": "M9", "mailboxIds": {"m-sent": True}}]),
    )

    assert (
        resolve_uncertain_submission(jmap, "M9", "m-sent")  # type: ignore[arg-type]
        is True
    )
    [[call]] = jmap.calls
    assert call.arguments["properties"] == ["id", "mailboxIds"]


def test_an_uncertain_submission_still_draft_is_settled_unsent():
    jmap = FakeJmap(
        get_answer([{"id": "M9", "mailboxIds": {"m-drafts": True}}]),
    )

    assert (
        resolve_uncertain_submission(jmap, "M9", "m-sent")  # type: ignore[arg-type]
        is False
    )


def test_an_uncertain_submission_gone_is_settled_unknown():
    jmap = FakeJmap(get_answer([], not_found=["M9"]))

    assert (
        resolve_uncertain_submission(jmap, "M9", "m-sent")  # type: ignore[arg-type]
        is None
    )


def test_fetch_emails_fetches_only_the_properties_asked_for():
    jmap = FakeJmap(get_answer([{"id": "M1"}]))

    fetch_emails(jmap, ["M1"], None, ["id", "mailboxIds"])  # type: ignore[arg-type]

    [[call]] = jmap.calls
    assert call.arguments["properties"] == ["id", "mailboxIds"]
