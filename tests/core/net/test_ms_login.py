import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from postcard.core.net import ms_login
from postcard.core.net.graph_session import Response
from postcard.core.net.ms_oauth import (
    MsOAuthError,
    code_challenge,
    encode_stored,
)


class FakeTransport:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def token_reply(body: object, status: int = 200) -> Response:
    return Response(status, json.dumps(body).encode(), {})


class SimulatedIdp:
    """A browser opener that completes the login at the loopback URL itself.

    Like a real browser it returns at once; the callback lands on a thread,
    after log_in has started waiting for it.
    """

    def __init__(self, *, code="code-1", error=None):
        self.visited = []
        self.threads = []
        self.code = code
        self.error = error

    def __call__(self, url):
        self.visited.append(url)
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        target = params["redirect_uri"] + (
            f"?error={self.error}&state={params['state']}"
            if self.error
            else f"?code={self.code}&state={params['state']}"
        )
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


def test_a_login_binds_the_browser_proof_to_the_code_exchange():
    transport = FakeTransport(
        token_reply({"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    )
    browser = SimulatedIdp()

    login = ms_login.log_in(
        client_id="test-client",
        open_browser=browser,
        transport=transport,
        timeout=10.0,
    )
    browser.join()

    assert login.tokens.access_token == "a"
    assert login.stored == encode_stored("r")
    assert len(browser.visited) == 1
    authorize = query_of(browser.visited[0])
    assert authorize["response_type"] == "code"
    assert authorize["code_challenge_method"] == "S256"
    form = query_of("?" + transport.requests[0].data.decode())
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == authorize["redirect_uri"]
    assert code_challenge(form["code_verifier"]) == authorize["code_challenge"]


def test_an_unanswered_login_times_out_before_any_token_request():
    transport = FakeTransport()
    visited = []

    with pytest.raises(MsOAuthError) as caught:
        ms_login.log_in(
            client_id="test-client",
            open_browser=visited.append,
            transport=transport,
            timeout=0.05,
        )

    assert caught.value.code == "invalid_request"
    assert transport.requests == []


def test_a_provider_refusal_never_reaches_the_token_endpoint():
    transport = FakeTransport()
    browser = SimulatedIdp(error="access_denied")

    with pytest.raises(MsOAuthError) as caught:
        ms_login.log_in(
            client_id="test-client",
            open_browser=browser,
            transport=transport,
            timeout=10.0,
        )
    browser.join()

    assert caught.value.code == "invalid_grant"
    assert transport.requests == []


def test_a_login_without_a_refresh_token_fails_closed():
    transport = FakeTransport(token_reply({"access_token": "a"}))
    browser = SimulatedIdp()

    with pytest.raises(MsOAuthError) as caught:
        ms_login.log_in(
            client_id="test-client",
            open_browser=browser,
            transport=transport,
            timeout=10.0,
        )
    browser.join()

    assert caught.value.code == "invalid_response"


def test_a_refused_code_exchange_keeps_its_code():
    transport = FakeTransport(token_reply({"error": "invalid_grant"}, status=400))
    browser = SimulatedIdp()

    with pytest.raises(MsOAuthError) as caught:
        ms_login.log_in(
            client_id="test-client",
            open_browser=browser,
            transport=transport,
            timeout=10.0,
        )
    browser.join()

    assert caught.value.code == "invalid_grant"


def test_nothing_happens_before_an_app_is_registered():
    def fail_on_browser(_url):
        pytest.fail("the browser opened with no client id")

    with pytest.raises(MsOAuthError) as caught:
        ms_login.log_in(open_browser=fail_on_browser, transport=FakeTransport())

    assert caught.value.code == "invalid_request"
