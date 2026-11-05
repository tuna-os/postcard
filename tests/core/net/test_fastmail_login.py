import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from postcard.core.net import fastmail_login
from postcard.core.net.fastmail_oauth import (
    FASTMAIL_AUTHORIZE_URL,
    FASTMAIL_ISSUER,
    FASTMAIL_METADATA_URL,
    FASTMAIL_SCOPES,
    FASTMAIL_TOKEN_URL,
    FastmailOAuthError,
    code_challenge,
    encode_stored,
)
from postcard.core.net.jmap_session import Response

CLIENT_ID = "test-client-id"
REGISTRATION_URL = "https://api.fastmail.com/oauth/register"


class FakeTransport:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def json_reply(body: object, status: int = 200) -> Response:
    headers = {"Content-Type": "application/json"} if status == 200 else {}
    return Response(status, json.dumps(body).encode(), headers)


def metadata_reply(overrides: dict | None = None) -> Response:
    # A conforming document; the live shape is pinned in test_fastmail_oauth.
    body = {
        "issuer": FASTMAIL_ISSUER,
        "registration_endpoint": REGISTRATION_URL,
        "authorization_endpoint": FASTMAIL_AUTHORIZE_URL,
        "token_endpoint": FASTMAIL_TOKEN_URL,
        "scopes_supported": list(FASTMAIL_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
    }
    if overrides:
        body.update(overrides)
    return json_reply(body)


def registration_reply(client_id: str = CLIENT_ID) -> Response:
    return json_reply(
        {
            "client_id": client_id,
            "scope": " ".join(FASTMAIL_SCOPES),
            "redirect_uris": ["http://127.0.0.1/"],
        },
        status=201,
    )


def token_reply(body: object, status: int = 200) -> Response:
    return json_reply(body, status)


class SimulatedIdp:
    """A browser opener that completes the login at the loopback URL itself.

    Like a real browser it returns at once; the callback lands on a thread,
    after log_in has started waiting for it.
    """

    def __init__(self, *, code="code-1", error=None, iss=FASTMAIL_ISSUER):
        self.visited = []
        self.threads = []
        self.code = code
        self.error = error
        self.iss = iss

    def __call__(self, url):
        self.visited.append(url)
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        callback = {"state": params["state"]}
        if self.error:
            callback["error"] = self.error
        else:
            callback["code"] = self.code
        if self.iss is not None:
            callback["iss"] = self.iss
        target = params["redirect_uri"] + "?" + urllib.parse.urlencode(callback)
        thread = threading.Thread(target=self._fetch, args=(target,), daemon=True)
        self.threads.append(thread)
        thread.start()

    def _fetch(self, target):
        try:
            urllib.request.urlopen(target, timeout=10.0).read()
        except urllib.error.HTTPError as failed:
            failed.read()

    def join(self):
        for thread in self.threads:
            thread.join(timeout=10.0)


def query_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_a_login_registers_before_it_opens_the_browser():
    transport = FakeTransport(
        metadata_reply(),
        registration_reply(),
        token_reply(
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 3600,
                "username": "ada@fastmail.com",
            }
        ),
    )
    browser = SimulatedIdp()

    login = fastmail_login.log_in(
        open_browser=browser,
        transport=transport,
        metadata_transport=transport,
        timeout=10.0,
    )
    browser.join()

    assert login.tokens.access_token == "a"
    assert login.username == "ada@fastmail.com"
    assert login.stored == encode_stored("r", client_id=CLIENT_ID)
    assert [request.full_url for request in transport.requests] == [
        FASTMAIL_METADATA_URL,
        REGISTRATION_URL,
        FASTMAIL_TOKEN_URL,
    ]
    assert len(browser.visited) == 1
    authorize = query_of(browser.visited[0])
    assert authorize["client_id"] == CLIENT_ID
    assert authorize["response_type"] == "code"
    assert authorize["code_challenge_method"] == "S256"
    assert authorize["redirect_uri"].startswith("http://127.0.0.1:")
    form = query_of("?" + transport.requests[2].data.decode())
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == authorize["redirect_uri"]
    assert form["client_id"] == CLIENT_ID
    assert code_challenge(form["code_verifier"]) == authorize["code_challenge"]


def test_a_nonconforming_server_never_opens_the_browser():
    transport = FakeTransport(metadata_reply({"issuer": "https://evil.example.com"}))
    visited = []

    with pytest.raises(FastmailOAuthError) as caught:
        fastmail_login.log_in(
            open_browser=visited.append,
            transport=transport,
            metadata_transport=transport,
            timeout=10.0,
        )

    assert caught.value.code == "invalid_response"
    assert visited == []


def test_a_refused_registration_never_opens_the_browser():
    transport = FakeTransport(
        metadata_reply(),
        json_reply({"error": "invalid_redirect_uri"}, status=400),
    )
    visited = []

    with pytest.raises(FastmailOAuthError) as caught:
        fastmail_login.log_in(
            open_browser=visited.append,
            transport=transport,
            metadata_transport=transport,
            timeout=10.0,
        )

    assert caught.value.code == "invalid_request"
    assert visited == []


def test_an_unanswered_login_times_out_before_any_token_request():
    transport = FakeTransport(metadata_reply(), registration_reply())
    visited = []

    with pytest.raises(FastmailOAuthError) as caught:
        fastmail_login.log_in(
            open_browser=visited.append,
            transport=transport,
            metadata_transport=transport,
            timeout=0.05,
        )

    assert caught.value.code == "invalid_request"
    assert len(visited) == 1
    assert [request.full_url for request in transport.requests] == [
        FASTMAIL_METADATA_URL,
        REGISTRATION_URL,
    ]


def test_a_provider_refusal_never_reaches_the_token_endpoint():
    transport = FakeTransport(metadata_reply(), registration_reply())
    browser = SimulatedIdp(error="access_denied")

    with pytest.raises(FastmailOAuthError) as caught:
        fastmail_login.log_in(
            open_browser=browser,
            transport=transport,
            metadata_transport=transport,
            timeout=10.0,
        )
    browser.join()

    assert caught.value.code == "invalid_grant"
    assert len(transport.requests) == 2


def test_a_code_from_another_issuer_is_never_exchanged():
    transport = FakeTransport(
        metadata_reply(),
        registration_reply(),
        token_reply({"access_token": "a", "refresh_token": "r"}),
    )
    browser = SimulatedIdp(iss="https://evil.example.com")

    with pytest.raises(FastmailOAuthError) as caught:
        fastmail_login.log_in(
            open_browser=browser,
            transport=transport,
            metadata_transport=transport,
            timeout=10.0,
        )
    browser.join()

    assert caught.value.code == "invalid_response"
    assert [request.full_url for request in transport.requests] == [
        FASTMAIL_METADATA_URL,
        REGISTRATION_URL,
    ]


def test_a_code_without_an_issuer_is_never_exchanged():
    transport = FakeTransport(metadata_reply(), registration_reply())
    browser = SimulatedIdp(iss=None)

    with pytest.raises(FastmailOAuthError) as caught:
        fastmail_login.log_in(
            open_browser=browser,
            transport=transport,
            metadata_transport=transport,
            timeout=10.0,
        )
    browser.join()

    assert caught.value.code == "invalid_response"
    assert len(transport.requests) == 2


def test_a_login_without_a_refresh_token_fails_closed():
    transport = FakeTransport(
        metadata_reply(), registration_reply(), token_reply({"access_token": "a"})
    )
    browser = SimulatedIdp()

    with pytest.raises(FastmailOAuthError) as caught:
        fastmail_login.log_in(
            open_browser=browser,
            transport=transport,
            metadata_transport=transport,
            timeout=10.0,
        )
    browser.join()

    assert caught.value.code == "invalid_response"


def test_a_refused_code_exchange_keeps_its_code():
    transport = FakeTransport(
        metadata_reply(),
        registration_reply(),
        token_reply({"error": "invalid_grant"}, status=400),
    )
    browser = SimulatedIdp()

    with pytest.raises(FastmailOAuthError) as caught:
        fastmail_login.log_in(
            open_browser=browser,
            transport=transport,
            metadata_transport=transport,
            timeout=10.0,
        )
    browser.join()

    assert caught.value.code == "invalid_grant"
