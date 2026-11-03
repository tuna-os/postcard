import base64
import json
import logging
import threading

import pytest

from postcard.core.net.auth import Credential
from postcard.core.net.jmap_session import (
    CODE_BAD_SESSION,
    CODE_CANCELLED,
    CODE_HTTP,
    CODE_READ_ONLY,
    CORE_URN,
    MAIL_URN,
    JmapAnswer,
    JmapCall,
    JmapError,
    JmapSession,
    Response,
    discover,
    expand_template,
    result_or_raise,
)

SESSION_URL = "https://jmap.example.com/.well-known/jmap"
API_URL = "https://jmap.example.com/jmap/api/"

SESSION_JSON = {
    "capabilities": {
        CORE_URN: {"maxCallsInRequest": 16, "maxObjectsInGet": 100},
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
            JmapCall("Email/get", {}),
            JmapCall("Mailbox/get", {}),
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

    with pytest.raises(JmapError, match="not a read call"):
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
