import json
import urllib.parse

import pytest

from postcard.core.net.fastmail_oauth import (
    FASTMAIL_AUTHORIZE_URL,
    FASTMAIL_CLIENT_ID,
    FASTMAIL_JMAP_SESSION_URL,
    FASTMAIL_SCOPES,
    FASTMAIL_TOKEN_URL,
    FastmailOAuthError,
    authorization_url,
    code_challenge,
    code_verifier,
    encode_stored,
    exchange_code,
    native_credential,
    refresh_access_token,
)
from postcard.core.net.jmap_session import Response

CLIENT_ID = "test-client-id"
REDIRECT_URI = "http://127.0.0.1:53147/"


def reply(status: int = 200, body: object = None, raw: bytes | None = None) -> Response:
    if raw is None:
        raw = b"" if body is None else json.dumps(body).encode()
    return Response(status, raw, {})


class FakeTransport:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def form_of(request) -> dict:
    return dict(urllib.parse.parse_qsl(request.data.decode()))


def test_a_verifier_is_random_and_within_the_pkce_length():
    first, second = code_verifier(), code_verifier()
    assert first != second
    assert 43 <= len(first) <= 128


def test_the_challenge_matches_the_rfc_vector():
    # RFC 7636 appendix B: fixed input, fixed output.
    assert code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_the_authorize_url_carries_the_pkce_challenge_and_scopes():
    url = authorization_url(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        state="state-1",
        code_challenge="challenge-1",
    )
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith(f"{FASTMAIL_AUTHORIZE_URL}?")
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["scope"] == " ".join(FASTMAIL_SCOPES)
    assert "urn:ietf:params:jmap:core" in query["scope"].split()
    assert "urn:ietf:params:jmap:mail" in query["scope"].split()


def test_the_authorize_url_names_the_jmap_session_as_its_resource():
    url = authorization_url(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        state="state-1",
        code_challenge="challenge-1",
    )
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert query["resource"] == FASTMAIL_JMAP_SESSION_URL


def test_nothing_signs_in_before_a_client_is_registered():
    transport = FakeTransport()
    assert FASTMAIL_CLIENT_ID == ""
    with pytest.raises(FastmailOAuthError) as caught:
        authorization_url(redirect_uri=REDIRECT_URI, state="s", code_challenge="c")
    assert caught.value.code == "invalid_request"
    with pytest.raises(FastmailOAuthError):
        exchange_code(
            "code", "verifier", redirect_uri=REDIRECT_URI, transport=transport
        )
    with pytest.raises(FastmailOAuthError):
        refresh_access_token("refresh", transport=transport)
    assert transport.requests == []


def test_the_code_exchange_posts_the_pkce_proof():
    transport = FakeTransport(
        reply(
            body={
                "access_token": "a",
                "refresh_token": "r",
                "token_type": "bearer",
                "expires_in": 3600,
            }
        )
    )

    tokens = exchange_code(
        "code-1",
        "verifier-1",
        redirect_uri=REDIRECT_URI,
        client_id=CLIENT_ID,
        transport=transport,
    )

    request = transport.requests[0]
    assert request.full_url == FASTMAIL_TOKEN_URL
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert form_of(request) == {
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": REDIRECT_URI,
        "code_verifier": "verifier-1",
    }
    assert (tokens.access_token, tokens.refresh_token, tokens.expires_in) == (
        "a",
        "r",
        3600,
    )


def test_a_refresh_posts_no_scope():
    transport = FakeTransport(reply(body={"access_token": "a2", "refresh_token": "r2"}))

    tokens = refresh_access_token(
        "old-refresh", client_id=CLIENT_ID, transport=transport
    )

    assert form_of(transport.requests[0]) == {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
    }
    assert (tokens.access_token, tokens.refresh_token) == ("a2", "r2")


def test_a_refused_grant_means_reconnect_not_retry():
    transport = FakeTransport(
        reply(status=400, body={"error": "invalid_grant", "error_description": "x"})
    )
    with pytest.raises(FastmailOAuthError) as caught:
        refresh_access_token("stale", client_id=CLIENT_ID, transport=transport)
    assert caught.value.code == "invalid_grant"
    assert "x" not in str(caught.value)


def test_a_temporary_refusal_is_still_a_grant_problem():
    transport = FakeTransport(
        reply(status=400, body={"error": "invalid_grant", "temporary": True})
    )
    with pytest.raises(FastmailOAuthError) as caught:
        refresh_access_token("locked", client_id=CLIENT_ID, transport=transport)
    assert caught.value.code == "invalid_grant"


def test_other_token_failures_stay_generic():
    for response in (
        reply(status=400, body={"error": "temporarily_unavailable"}),
        reply(status=500, body={"error": "server_error"}),
        reply(status=400, body={}),
    ):
        with pytest.raises(FastmailOAuthError) as caught:
            refresh_access_token(
                "r", client_id=CLIENT_ID, transport=FakeTransport(response)
            )
        assert caught.value.code == "server_error"


def test_a_malformed_token_answer_is_not_a_grant_problem():
    for response in (
        reply(raw=b"not json"),
        reply(body=["access_token"]),
        reply(body={"refresh_token": "r"}),
        reply(body={"access_token": ""}),
        reply(body={"access_token": 42}),
    ):
        with pytest.raises(FastmailOAuthError) as caught:
            refresh_access_token(
                "r", client_id=CLIENT_ID, transport=FakeTransport(response)
            )
        assert caught.value.code == "invalid_response"


def test_a_garbage_lifetime_falls_back_to_zero():
    transport = FakeTransport(
        reply(body={"access_token": "a", "refresh_token": "r", "expires_in": "soon"})
    )
    assert (
        refresh_access_token("r", client_id=CLIENT_ID, transport=transport).expires_in
        == 0
    )


def test_a_network_failure_reaches_the_caller_unwrapped():
    def failing(_request):
        raise OSError("network is down")

    with pytest.raises(OSError, match="network is down"):
        refresh_access_token("r", client_id=CLIENT_ID, transport=failing)


def test_a_stored_refresh_token_becomes_an_xoauth2_credential():
    transport = FakeTransport(
        reply(body={"access_token": "fresh", "refresh_token": "r2", "expires_in": 60})
    )

    credential = native_credential(
        "ada@fastmail.com",
        encode_stored("refresh-1"),
        client_id=CLIENT_ID,
        transport=transport,
    )

    assert credential.user == "ada@fastmail.com"
    assert credential.secret == "fresh"
    assert credential.mechanism == "xoauth2"
    assert form_of(transport.requests[0])["refresh_token"] == "refresh-1"


def test_a_stored_value_that_is_not_ours_is_rejected_before_any_request():
    transport = FakeTransport()
    for stored in ("hunter2", "{}", json.dumps({"refresh_token": ""}), "[]"):
        with pytest.raises(FastmailOAuthError) as caught:
            native_credential(
                "ada@fastmail.com", stored, client_id=CLIENT_ID, transport=transport
            )
        assert caught.value.code == "invalid_request"
    assert transport.requests == []
