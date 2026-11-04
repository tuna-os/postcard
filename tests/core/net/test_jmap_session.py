import base64
import dataclasses
import json
import logging
import threading

import pytest

from postcard.core.net.auth import Credential
from postcard.core.net.jmap_session import (
    CODE_BAD_SESSION,
    CODE_CANCELLED,
    CODE_HTTP,
    CODE_INVALID_ARGUMENTS,
    CODE_PUSH_FAILED,
    CODE_READ_ONLY,
    CORE_URN,
    MAIL_URN,
    SUBMISSION_URN,
    JmapAnswer,
    JmapCall,
    JmapError,
    JmapSession,
    PushConfig,
    Response,
    StateChange,
    batch_calls,
    discover,
    expand_template,
    is_state_mismatch,
    needs_full_sync,
    parse_set_errors,
    parse_state_changes,
    reconnect_delay,
    result_or_raise,
)

SESSION_URL = "https://jmap.example.com/.well-known/jmap"
API_URL = "https://jmap.example.com/jmap/api/"

SESSION_JSON = {
    "capabilities": {
        CORE_URN: {
            "maxCallsInRequest": 16,
            "maxObjectsInGet": 100,
            "maxObjectsInSet": 50,
        },
        MAIL_URN: {"maxMailboxesPerEmail": 100},
        "urn:ietf:params:jmap:submission": {"maxDelayedSend": 0},
    },
    "accounts": {
        "A1": {
            "name": "ada@example.com",
            "isPersonal": True,
            "isReadOnly": False,
            "accountCapabilities": {MAIL_URN: {}},
        }
    },
    "primaryAccounts": {MAIL_URN: "A1"},
    "username": "ada@example.com",
    "apiUrl": API_URL,
    "downloadUrl": "https://jmap.example.com/jmap/download/{accountId}/{blobId}/{name}",
    "uploadUrl": "https://jmap.example.com/jmap/upload/{accountId}/",
    "eventSourceUrl": "https://jmap.example.com/jmap/eventsource/"
    "?types={types}&closeafter={closeafter}&ping={ping}",
    "state": "abc123",
}

PASSWORD = Credential("ada@example.com", "hunter2")
TOKEN = Credential("ada@example.com", "tok-secret-123", "xoauth2")


def reply(status=200, body=None, **headers):
    return Response(
        status, json.dumps(body).encode() if body is not None else b"", headers
    )


class FakeTransport:
    """Answers each request with the next queued Response and records it."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def session_body(**overrides):
    body = {**SESSION_JSON, **overrides}
    return reply(body=body)


def test_discovery_maps_the_session_resource():
    session = discover(SESSION_URL, PASSWORD, FakeTransport(session_body()))

    assert session.account_id == "A1"
    assert session.api_url == API_URL
    assert session.max_calls == 16
    assert session.max_objects_in_get == 100
    assert MAIL_URN in session.capabilities
    assert (
        session.download_url("B1", "n.txt")
        == "https://jmap.example.com/jmap/download/A1/B1/n.txt"
    )
    assert session.upload_url() == "https://jmap.example.com/jmap/upload/A1/"
    assert session.event_source_url("Email", 300, 30).endswith(
        "?types=Email&closeafter=300&ping=30"
    )


def test_a_read_only_account_is_accepted_since_part_1_only_reads():
    body = {
        **SESSION_JSON,
        "accounts": {
            "A1": {
                "name": "ada@example.com",
                "isPersonal": True,
                "isReadOnly": True,
                "accountCapabilities": {MAIL_URN: {}},
            }
        },
    }

    session = discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert session.account_id == "A1"


def test_the_only_mail_account_is_used_without_a_primary():
    body = {
        **SESSION_JSON,
        "primaryAccounts": {},
        "accounts": {
            "A1": {"accountCapabilities": {MAIL_URN: {}}},
            "A2": {"accountCapabilities": {"urn:ietf:params:jmap:vacation": {}}},
        },
    }

    session = discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert session.account_id == "A1"


def test_discovery_without_a_mail_account_is_refused():
    body = {
        **SESSION_JSON,
        "primaryAccounts": {},
        "accounts": {
            "A1": {"accountCapabilities": {MAIL_URN: {}}},
            "A2": {"accountCapabilities": {MAIL_URN: {}}},
        },
    }

    with pytest.raises(JmapError) as raised:
        discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert raised.value.code == CODE_BAD_SESSION


def test_discovery_without_the_mail_capability_is_refused():
    body = {
        **SESSION_JSON,
        "capabilities": {CORE_URN: SESSION_JSON["capabilities"][CORE_URN]},
    }

    with pytest.raises(JmapError, match="does not speak JMAP mail"):
        discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))


def test_an_api_url_outside_the_session_is_refused():
    body = {**SESSION_JSON, "apiUrl": "https://evil.example.net/jmap/api/"}

    with pytest.raises(JmapError) as raised:
        discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert raised.value.code == CODE_BAD_SESSION
    assert "evil.example.net" in raised.value.message


def test_a_download_template_outside_the_session_is_refused():
    body = {
        **SESSION_JSON,
        "downloadUrl": "https://cdn.example.net/dl/{accountId}/{blobId}",
    }

    with pytest.raises(JmapError) as raised:
        discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert raised.value.code == CODE_BAD_SESSION


def test_a_session_url_without_tls_is_refused():
    with pytest.raises(JmapError, match="must use https"):
        discover("http://jmap.example.com/.well-known/jmap", PASSWORD)


def test_a_template_without_its_variables_is_refused():
    body = {**SESSION_JSON, "uploadUrl": "https://jmap.example.com/upload/"}

    with pytest.raises(JmapError) as raised:
        discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert raised.value.code == CODE_BAD_SESSION
    assert "{accountId}" in raised.value.message


def test_an_api_url_with_template_braces_is_refused():
    body = {**SESSION_JSON, "apiUrl": API_URL + "{accountId}"}

    with pytest.raises(JmapError, match="not a plain URL"):
        discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))


def test_a_session_answer_that_is_not_json_is_refused():
    transport = FakeTransport(Response(200, b"<html>login page</html>"))

    with pytest.raises(JmapError) as raised:
        discover(SESSION_URL, PASSWORD, transport)

    assert raised.value.code == CODE_BAD_SESSION


def test_a_rejected_session_request_raises_its_status():
    transport = FakeTransport(Response(401, b"{}"))

    with pytest.raises(JmapError) as raised:
        discover(SESSION_URL, PASSWORD, transport)

    assert (raised.value.status, raised.value.code) == (401, CODE_HTTP)


def test_a_password_signs_in_with_basic_auth():
    transport = FakeTransport(session_body())

    discover(SESSION_URL, PASSWORD, transport)

    expected = base64.b64encode(b"ada@example.com:hunter2").decode()
    assert transport.requests[0].get_header("Authorization") == f"Basic {expected}"


def test_a_token_signs_in_with_a_bearer_header():
    transport = FakeTransport(session_body())

    discover(SESSION_URL, TOKEN, transport)

    assert transport.requests[0].get_header("Authorization") == "Bearer tok-secret-123"


def test_an_unknown_mechanism_cannot_sign_in():
    transport = FakeTransport(session_body())

    with pytest.raises(JmapError, match="apikey cannot sign in"):
        discover(SESSION_URL, Credential("ada", "x", "apikey"), transport)

    assert transport.requests == []


def discovered_session():
    return discover(SESSION_URL, PASSWORD, FakeTransport(session_body()))


def test_requests_post_method_calls_using_only_core_and_mail():
    transport = FakeTransport(
        reply(body={"methodResponses": [["Email/get", {"list": []}, "0"]]})
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    [answer] = jmap.request([JmapCall("Email/get", {"ids": ["M1"]})])

    assert answer.is_error is False
    request = transport.requests[0]
    assert request.full_url == API_URL
    sent = json.loads(request.data)
    assert sent["using"] == [CORE_URN, MAIL_URN]
    assert sent["methodCalls"] == [
        [
            "Email/get",
            {"ids": ["M1"]},
            "0",
        ]
    ]


def test_answers_come_back_in_request_order():
    transport = FakeTransport(
        reply(
            body={
                "methodResponses": [
                    ["Email/get", {"list": [{"id": "M1"}]}, "0"],
                    ["Mailbox/get", {"list": []}, "1"],
                ]
            }
        )
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    answers = jmap.request(
        [
            JmapCall("Email/get", {}, "0"),
            JmapCall("Mailbox/get", {}, "1"),
        ]
    )

    assert [(a.method, a.call_id) for a in answers] == [
        ("Email/get", "0"),
        ("Mailbox/get", "1"),
    ]


def test_a_method_error_carries_the_server_s_type():
    transport = FakeTransport(
        reply(
            body={
                "methodResponses": [
                    [
                        "error",
                        {"type": "accountNotFound", "description": "no such A9"},
                        "0",
                    ]
                ]
            }
        )
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    [answer] = jmap.request([JmapCall("Mailbox/get", {})])

    assert answer.is_error is True
    with pytest.raises(JmapError) as raised:
        result_or_raise(answer)
    assert (raised.value.code, raised.value.message) == (
        "accountNotFound",
        "no such A9",
    )


def test_a_write_is_refused_before_it_reaches_the_network():
    transport = FakeTransport()
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError) as raised:
        jmap.request([JmapCall("Email/set", {"update": {"M1": {}}})])

    assert raised.value.code == CODE_READ_ONLY
    assert transport.requests == []


def test_a_non_mail_call_is_refused_before_it_reaches_the_network():
    transport = FakeTransport()
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError, match="not a JMAP mail call"):
        jmap.request([JmapCall("CalendarEvent/get", {})])

    assert transport.requests == []


def test_throttling_is_retried_after_the_delay_asked_for():
    sleeps = []
    transport = FakeTransport(
        reply(429, {"error": "slow down"}, **{"Retry-After": "7"}),
        reply(body={"methodResponses": [["Email/query", {}, "0"]]}),
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport, sleep=sleeps.append)

    jmap.request([JmapCall("Email/query", {})])

    assert sleeps == [7.0]
    assert len(transport.requests) == 2


def test_a_backend_that_stays_busy_raises_after_the_last_attempt():
    transport = FakeTransport(*[reply(503, {"error": "busy"})] * 4)
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError) as raised:
        jmap.request([JmapCall("Email/query", {})])

    assert raised.value.status == 503
    assert len(transport.requests) == 4


def test_a_malformed_answer_is_refused():
    transport = FakeTransport(reply(body={"methodResponses": [["Email/get"]]}))
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError) as raised:
        jmap.request([JmapCall("Email/get", {})])

    assert raised.value.code == CODE_HTTP


def test_a_shapeless_answer_body_is_refused():
    transport = FakeTransport(reply(body=["Email/get"]))
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError) as raised:
        jmap.request([JmapCall("Email/get", {})])

    assert raised.value.code == CODE_HTTP


def test_a_cancelled_request_never_reaches_the_network():
    transport = FakeTransport()
    jmap = JmapSession(discovered_session(), PASSWORD, transport)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(JmapError) as raised:
        jmap.request([JmapCall("Email/get", {})], cancelled)

    assert raised.value.code == CODE_CANCELLED
    assert transport.requests == []


def test_errors_and_logs_carry_no_credentials(caplog):
    transport = FakeTransport(
        reply(429, {"error": "slow down"}, **{"Retry-After": "0"}),
        reply(body={"methodResponses": [["Email/query", {}, "0"]]}),
    )
    jmap = JmapSession(discovered_session(), TOKEN, transport, sleep=lambda _s: None)

    with caplog.at_level(logging.DEBUG, logger="postcard.core.net.jmap_session"):
        jmap.request([JmapCall("Email/query", {})])
    try:
        jmap.request([JmapCall("Email/set", {})])
    except JmapError as error:
        refusal = str(error)

    assert "tok-secret-123" not in caplog.text
    assert "Bearer" not in caplog.text
    assert "tok-secret-123" not in refusal


def test_templates_quote_their_variables():
    assert (
        expand_template(
            "https://h/dl/{accountId}/{name}",
            {
                "accountId": "A1",
                "name": "a b.txt",
            },
        )
        == "https://h/dl/A1/a%20b.txt"
    )


def test_a_template_without_a_value_is_refused():
    with pytest.raises(JmapError, match="could not expand"):
        expand_template("https://h/dl/{accountId}/{blobId}", {"accountId": "A1"})


def test_a_success_answer_passes_through_result_or_raise():
    assert result_or_raise(JmapAnswer("Email/get", {"list": []}, "0")) == {"list": []}


def test_discovery_maps_the_set_limit_read_only_and_submission():
    session = discover(SESSION_URL, PASSWORD, FakeTransport(session_body()))

    assert session.max_objects_in_set == 50
    assert session.is_read_only is False
    assert session.submission_supported is True


def test_a_missing_set_limit_defaults_instead_of_failing():
    body = {
        **SESSION_JSON,
        "capabilities": {
            CORE_URN: {"maxCallsInRequest": 16, "maxObjectsInGet": 100},
            MAIL_URN: {},
        },
    }

    session = discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert session.max_objects_in_set == 100


def test_a_read_only_account_is_flagged_for_write_refusal():
    body = {
        **SESSION_JSON,
        "accounts": {
            "A1": {
                "name": "ada@example.com",
                "isPersonal": True,
                "isReadOnly": True,
                "accountCapabilities": {MAIL_URN: {}},
            }
        },
    }

    session = discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert session.is_read_only is True


def test_submission_is_unsupported_without_its_capability():
    body = {
        **SESSION_JSON,
        "capabilities": {
            CORE_URN: SESSION_JSON["capabilities"][CORE_URN],
            MAIL_URN: {},
        },
    }

    session = discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))

    assert session.submission_supported is False


def writable_session(transport, **overrides):
    body = {**SESSION_JSON, **overrides}
    session_transport = FakeTransport(reply(body=body))
    session = discover(SESSION_URL, PASSWORD, session_transport)
    return JmapSession(session, PASSWORD, transport, writable=True)


def test_a_writable_session_sends_email_set():
    transport = FakeTransport(
        reply(body={"methodResponses": [["Email/set", {"updated": ["M1"]}, "0"]]})
    )
    jmap = writable_session(transport)

    [answer] = jmap.request(
        [JmapCall("Email/set", {"update": {"M1": {"keywords/$seen": True}}})]
    )

    assert result_or_raise(answer) == {"updated": ["M1"]}
    assert len(transport.requests) == 1


def test_a_write_on_a_default_session_is_refused():
    transport = FakeTransport()
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError, match="needs a writable session"):
        jmap.request([JmapCall("Email/set", {"update": {"M1": {}}})])

    assert transport.requests == []


def test_a_write_to_a_read_only_account_is_refused():
    transport = FakeTransport()
    jmap = writable_session(
        transport,
        accounts={
            "A1": {
                "isPersonal": True,
                "isReadOnly": True,
                "accountCapabilities": {MAIL_URN: {}},
            }
        },
    )

    assert jmap.is_writable is False
    with pytest.raises(JmapError) as raised:
        jmap.request([JmapCall("Email/set", {"update": {"M1": {}}})])

    assert raised.value.code == CODE_READ_ONLY
    assert transport.requests == []


def test_reads_still_go_out_on_a_read_only_account():
    body = {
        **SESSION_JSON,
        "accounts": {
            "A1": {
                "isPersonal": True,
                "isReadOnly": True,
                "accountCapabilities": {MAIL_URN: {}},
            }
        },
    }
    session = discover(SESSION_URL, PASSWORD, FakeTransport(reply(body=body)))
    transport = FakeTransport(
        reply(body={"methodResponses": [["Mailbox/get", {"list": []}, "0"]]})
    )
    jmap = JmapSession(session, PASSWORD, transport, writable=True)

    [answer] = jmap.request([JmapCall("Mailbox/get", {})])

    assert answer.is_error is False


def test_submission_calls_claim_the_submission_capability():
    transport = FakeTransport(
        reply(body={"methodResponses": [["EmailSubmission/get", {"list": []}, "0"]]})
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    jmap.request([JmapCall("EmailSubmission/get", {"ids": ["S1"]})])

    sent = json.loads(transport.requests[0].data)
    assert sent["using"] == [CORE_URN, MAIL_URN, SUBMISSION_URN]


def test_mail_calls_do_not_claim_submission():
    transport = FakeTransport(
        reply(body={"methodResponses": [["Email/get", {"list": []}, "0"]]})
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    jmap.request([JmapCall("Email/get", {"ids": ["M1"]})])

    sent = json.loads(transport.requests[0].data)
    assert sent["using"] == [CORE_URN, MAIL_URN]


def test_duplicate_call_ids_are_refused():
    transport = FakeTransport()
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError) as raised:
        jmap.request([JmapCall("Email/get", {}), JmapCall("Mailbox/get", {})])

    assert raised.value.code == CODE_INVALID_ARGUMENTS
    assert transport.requests == []


def test_reordered_answers_land_on_their_own_calls():
    transport = FakeTransport(
        reply(
            body={
                "methodResponses": [
                    ["Mailbox/get", {"list": []}, "b"],
                    ["Email/get", {"list": [{"id": "M1"}]}, "a"],
                ]
            }
        )
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    answers = jmap.request(
        [JmapCall("Email/get", {}, "a"), JmapCall("Mailbox/get", {}, "b")]
    )

    assert [(a.method, a.call_id) for a in answers] == [
        ("Email/get", "a"),
        ("Mailbox/get", "b"),
    ]


def test_an_answer_missing_a_call_id_is_refused():
    transport = FakeTransport(
        reply(
            body={
                "methodResponses": [
                    ["Email/get", {"list": []}, "a"],
                    ["Mailbox/get", {"list": []}, "zzz"],
                ]
            }
        )
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError, match="missed a call id"):
        jmap.request([JmapCall("Email/get", {}, "a"), JmapCall("Mailbox/get", {}, "b")])


def test_an_answer_repeating_a_call_id_is_refused():
    transport = FakeTransport(
        reply(
            body={
                "methodResponses": [
                    ["Email/get", {"list": []}, "a"],
                    ["Mailbox/get", {"list": []}, "a"],
                ]
            }
        )
    )
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError, match="repeated a call id"):
        jmap.request([JmapCall("Email/get", {}, "a"), JmapCall("Mailbox/get", {}, "b")])


def test_a_write_retries_on_an_explicit_rate_limit():
    sleeps = []
    transport = FakeTransport(
        reply(429, {"error": "slow down"}, **{"Retry-After": "2"}),
        reply(body={"methodResponses": [["Email/set", {"updated": ["M1"]}, "0"]]}),
    )
    jmap = writable_session(transport)
    jmap._sleep = sleeps.append

    [answer] = jmap.request([JmapCall("Email/set", {"update": {"M1": {}}})])

    assert result_or_raise(answer) == {"updated": ["M1"]}
    assert sleeps == [2.0]
    assert len(transport.requests) == 2


def test_a_write_is_not_retried_on_a_gateway_failure():
    transport = FakeTransport(
        reply(503, {"error": "busy"}),
        reply(body={"methodResponses": [["Email/set", {}, "0"]]}),
    )
    jmap = writable_session(transport)

    with pytest.raises(JmapError) as raised:
        jmap.request([JmapCall("Email/set", {"update": {"M1": {}}})])

    assert raised.value.status == 503
    assert len(transport.requests) == 1


def test_batch_calls_split_on_the_server_limit():
    calls = [JmapCall("Email/get", {}, str(n)) for n in range(5)]

    assert [len(batch) for batch in batch_calls(calls, 2)] == [2, 2, 1]
    assert batch_calls(calls[:1], 16) == [calls[:1]]


def test_batch_calls_refuse_an_unusable_limit():
    with pytest.raises(JmapError, match="not usable"):
        batch_calls([JmapCall("Email/get", {})], 0)


def test_set_errors_keep_the_server_s_type_per_object():
    errors = parse_set_errors(
        {
            "notUpdated": {
                "M1": {"type": "forbidden", "description": "locked"},
                "M2": {"type": "notFound"},
            }
        },
        "notUpdated",
    )

    assert (errors["M1"].code, errors["M1"].message) == ("forbidden", "locked")
    assert (errors["M2"].code, errors["M2"].message) == ("notFound", "notFound")


def test_a_shapeless_set_error_map_is_refused():
    with pytest.raises(JmapError, match="no notUpdated map"):
        parse_set_errors({"notUpdated": ["M1"]}, "notUpdated")


def test_only_an_expired_state_needs_a_full_sync():
    assert needs_full_sync(JmapError(0, "cannotCalculateChanges", "old")) is True
    assert needs_full_sync(JmapError(0, "accountNotFound", "gone")) is False
    assert needs_full_sync(JmapError(503, CODE_HTTP, "busy")) is False


def test_only_a_state_mismatch_is_one():
    assert is_state_mismatch(JmapError(0, "stateMismatch", "moved")) is True
    assert is_state_mismatch(JmapError(0, "cannotCalculateChanges", "old")) is False


def test_a_blob_downloads_with_the_session_credentials():
    transport = FakeTransport(Response(200, b"raw bytes"))
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    assert jmap.download_blob("B1", "n.txt") == b"raw bytes"
    request = transport.requests[0]
    assert request.full_url == ("https://jmap.example.com/jmap/download/A1/B1/n.txt")
    assert request.get_header("Authorization") is not None


def test_a_missing_blob_raises_its_status():
    transport = FakeTransport(Response(404, b"{}"))
    jmap = JmapSession(discovered_session(), PASSWORD, transport)

    with pytest.raises(JmapError) as raised:
        jmap.download_blob("B9", "n.txt")

    assert raised.value.status == 404


def test_a_blob_outside_the_session_is_refused():
    session = discovered_session()
    tampered = dataclasses.replace(
        session, download_template="https://cdn.example.net/dl/{accountId}/{blobId}"
    )
    jmap = JmapSession(tampered, PASSWORD, FakeTransport())

    with pytest.raises(JmapError) as raised:
        jmap.download_blob("B1", "n.txt")

    assert raised.value.code == CODE_BAD_SESSION
    assert "cdn.example.net" in raised.value.message


def test_an_upload_returns_its_blob_id():
    transport = FakeTransport(
        reply(body={"accountId": "A1", "blobId": "B7", "size": 11})
    )
    jmap = writable_session(transport)

    assert jmap.upload_blob(b"hello world") == "B7"
    request = transport.requests[0]
    assert request.full_url == "https://jmap.example.com/jmap/upload/A1/"
    assert request.data == b"hello world"


def test_an_upload_on_a_default_session_is_refused():
    jmap = JmapSession(discovered_session(), PASSWORD, FakeTransport())

    with pytest.raises(JmapError) as raised:
        jmap.upload_blob(b"hello")

    assert raised.value.code == CODE_READ_ONLY


def test_an_upload_without_a_blob_id_is_refused():
    transport = FakeTransport(reply(body={"accountId": "A1"}))
    jmap = writable_session(transport)

    with pytest.raises(JmapError, match="without a blob id"):
        jmap.upload_blob(b"hello")


STATE_CHUNK = (
    ": ping\n"
    "\n"
    'event: state\ndata: {"@type":"StateChange","changed":'
    '{"A1":{"Email":{},"Mailbox":{}}}}\n'
    "\n"
    "event: other\ndata: {}\n"
    "\n"
)


def test_push_events_parse_to_changed_types():
    [change] = parse_state_changes(STATE_CHUNK)

    assert change == StateChange(changed={"A1": frozenset({"Email", "Mailbox"})})


def test_pings_and_unknown_events_are_ignored():
    assert parse_state_changes(": ping\n\n") == []
    assert parse_state_changes('event: other\ndata: {"a":1}\n\n') == []
    assert parse_state_changes("") == []


def test_a_malformed_push_event_is_skipped_not_fatal(caplog):
    changes = parse_state_changes("event: state\ndata: not json\n\n")

    assert changes == []


def test_reconnect_delay_backs_off_with_a_cap():
    assert reconnect_delay(1, 1.0, 60.0) == 1.0
    assert reconnect_delay(3, 1.0, 60.0) == 4.0
    assert reconnect_delay(99, 1.0, 60.0) == 60.0


def quiet_config(**overrides):
    values = {"sleep": lambda _s: None, **overrides}
    return PushConfig(**values)


def test_push_delivers_changes_until_cancelled():
    jmap = JmapSession(discovered_session(), PASSWORD, FakeTransport())
    cancelled = threading.Event()
    seen = []

    def on_change(change):
        seen.append(change)
        cancelled.set()

    def stream(request, _cancelled):
        assert request.get_header("Accept") == "text/event-stream"
        yield STATE_CHUNK

    with pytest.raises(JmapError) as raised:
        jmap.watch_push(stream, on_change, cancelled, quiet_config())

    assert raised.value.code == CODE_CANCELLED
    assert seen == [StateChange(changed={"A1": frozenset({"Email", "Mailbox"})})]


def test_push_reconnects_after_a_drop():
    sleeps = []
    jmap = JmapSession(discovered_session(), PASSWORD, FakeTransport())
    cancelled = threading.Event()
    seen = []
    attempts = []

    def stream(request, _cancelled):
        attempts.append(request)
        if len(attempts) == 1:
            raise ConnectionError("dropped")
        yield STATE_CHUNK
        cancelled.set()
        yield ""

    with pytest.raises(JmapError) as raised:
        jmap.watch_push(
            stream,
            lambda change: seen.append(change),
            cancelled,
            PushConfig(sleep=sleeps.append),
        )

    assert raised.value.code == CODE_CANCELLED
    assert len(attempts) == 2
    assert sleeps == [1.0]
    assert len(seen) == 1


def test_push_gives_up_to_polling_after_its_budget():
    sleeps = []
    jmap = JmapSession(discovered_session(), PASSWORD, FakeTransport())

    def stream(request, _cancelled):
        raise ConnectionError("down")

    with pytest.raises(JmapError) as raised:
        jmap.watch_push(stream, lambda _c: None, None, PushConfig(sleep=sleeps.append))

    assert raised.value.code == CODE_PUSH_FAILED
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_a_cancelled_watch_never_opens_the_stream():
    opened = []
    jmap = JmapSession(discovered_session(), PASSWORD, FakeTransport())
    cancelled = threading.Event()
    cancelled.set()

    def stream(request, _cancelled):
        opened.append(request)
        yield ""

    with pytest.raises(JmapError) as raised:
        jmap.watch_push(stream, lambda _c: None, cancelled, quiet_config())

    assert raised.value.code == CODE_CANCELLED
    assert opened == []
