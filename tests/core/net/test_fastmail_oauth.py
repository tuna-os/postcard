import json
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from postcard.core.net.fastmail_oauth import (
    FASTMAIL_AUTHORIZE_URL,
    FASTMAIL_CLIENT_ID,
    FASTMAIL_ISSUER,
    FASTMAIL_JMAP_SESSION_URL,
    FASTMAIL_METADATA_URL,
    FASTMAIL_SCOPES,
    FASTMAIL_TOKEN_URL,
    FastmailOAuthError,
    authorization_url,
    code_challenge,
    code_verifier,
    encode_stored,
    exchange_code,
    fetch_metadata,
    native_credential,
    refresh_access_token,
    register_client,
    urlopen_noredirect_transport,
)
from postcard.core.net.jmap_session import Response

CLIENT_ID = "test-client-id"
REDIRECT_URI = "http://127.0.0.1:53147/"
REGISTRATION_URL = "https://api.fastmail.com/oauth/register"

# The live metadata shape, verified against api.fastmail.com: the issuer,
# the open registration endpoint, the public-client grants and methods,
# the registered scopes, and the protected JMAP session resource.
METADATA = {
    "issuer": FASTMAIL_ISSUER,
    "registration_endpoint": REGISTRATION_URL,
    "authorization_endpoint": FASTMAIL_AUTHORIZE_URL,
    "token_endpoint": FASTMAIL_TOKEN_URL,
    "scopes_supported": [
        "urn:ietf:params:oauth:scope:mail",
        "urn:ietf:params:oauth:scope:contacts",
        "urn:ietf:params:oauth:scope:calendars",
        "https://www.fastmail.com/dev/mcp",
        "openid",
        "profile",
        "email",
        "offline_access",
    ],
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_methods_supported": ["none"],
    "code_challenge_methods_supported": ["S256"],
    "authorization_response_iss_parameter_supported": True,
    "revocation_endpoint": "https://api.fastmail.com/oauth/revoke",
    "revocation_endpoint_auth_methods_supported": ["none"],
    "protected_resources": [
        FASTMAIL_JMAP_SESSION_URL,
        "https://api.fastmail.com/mcp",
        "https://caldav.fastmail.com",
        "https://carddav.fastmail.com",
        "https://webdav.fastmail.com",
        "imap.fastmail.com",
        "pop.fastmail.com",
        "smtp.fastmail.com",
    ],
}


def reply(
    status: int = 200,
    body: object = None,
    raw: bytes | None = None,
    headers: dict | None = None,
) -> Response:
    if raw is None:
        raw = b"" if body is None else json.dumps(body).encode()
    return Response(status, raw, headers or {})


def metadata_reply(metadata: object = None, **kwargs) -> Response:
    if metadata is None:
        metadata = METADATA
    headers = {"Content-Type": "application/json"}
    headers.update(kwargs.pop("headers", {}))
    return reply(body=metadata, headers=headers, **kwargs)


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


def test_the_picker_gate_stays_closed():
    # Dynamic registration replaced the manual step, so the flow needs no
    # static id -- but the picker row still gates on this flag, and the
    # ungate (provider_dialog plus the gui tests pinning both states) is
    # a follow-up outside this lane's file boundary.
    assert FASTMAIL_CLIENT_ID == ""


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
    assert "urn:ietf:params:oauth:scope:mail" in query["scope"].split()
    assert "offline_access" in query["scope"].split()


def test_the_authorize_url_names_the_jmap_session_as_its_resource():
    url = authorization_url(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        state="state-1",
        code_challenge="challenge-1",
    )
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert query["resource"] == FASTMAIL_JMAP_SESSION_URL
    assert query["prompt"] == "consent"


def test_an_empty_client_id_signs_nothing_in():
    transport = FakeTransport()
    with pytest.raises(FastmailOAuthError) as caught:
        authorization_url(
            client_id="", redirect_uri=REDIRECT_URI, state="s", code_challenge="c"
        )
    assert caught.value.code == "invalid_request"
    with pytest.raises(FastmailOAuthError):
        exchange_code("code", "verifier", redirect_uri=REDIRECT_URI, client_id="")
    with pytest.raises(FastmailOAuthError):
        refresh_access_token("refresh", client_id="", transport=transport)
    assert transport.requests == []


def test_the_metadata_fetch_reads_the_well_known_document():
    transport = FakeTransport(metadata_reply())

    server = fetch_metadata(transport)

    request = transport.requests[0]
    assert request.full_url == FASTMAIL_METADATA_URL
    assert request.get_method() == "GET"
    assert server.issuer == FASTMAIL_ISSUER
    assert server.registration_endpoint == REGISTRATION_URL
    assert server.authorization_endpoint == FASTMAIL_AUTHORIZE_URL
    assert server.token_endpoint == FASTMAIL_TOKEN_URL
    assert set(FASTMAIL_SCOPES) <= set(server.scopes_supported)
    assert FASTMAIL_JMAP_SESSION_URL in server.protected_resources


def test_a_metadata_http_failure_is_a_server_problem():
    transport = FakeTransport(reply(status=503, body={"error": "x"}))
    with pytest.raises(FastmailOAuthError) as caught:
        fetch_metadata(transport)
    assert caught.value.code == "server_error"


def test_a_misshapen_metadata_document_is_refused():
    cases = [
        {"raw": b"not json"},
        {"body": ["issuer"]},
        {"body": {**METADATA, "issuer": "https://api.fastmail.com.evil.net"}},
        {"body": {**METADATA, "issuer": "https://api.fastmail.com/"}},
        {"body": {k: v for k, v in METADATA.items() if k != "issuer"}},
        {
            "body": {**METADATA, "registration_endpoint": "http://x/register"},
            "headers": {"Content-Type": "application/json"},
        },
        {"body": {**METADATA, "token_endpoint": "https://"}},
        {"body": {**METADATA, "authorization_endpoint": None}},
        # A moved endpoint is not followed; the pins fail closed instead.
        {
            "body": {
                **METADATA,
                "authorization_endpoint": "https://api.fastmail.com/oauth/v2/auth",
            }
        },
        {
            "body": {
                **METADATA,
                "token_endpoint": "https://api.fastmail.com/oauth/v2/token",
            }
        },
        {"body": {**METADATA, "response_types_supported": ["token"]}},
        {"body": {**METADATA, "grant_types_supported": ["authorization_code"]}},
        {"body": {**METADATA, "token_endpoint_auth_methods_supported": ["none2"]}},
        {"body": {**METADATA, "code_challenge_methods_supported": ["plain"]}},
        {"body": {**METADATA, "code_challenge_methods_supported": "S256"}},
        {
            "body": {
                **METADATA,
                "authorization_response_iss_parameter_supported": False,
            }
        },
        {
            "body": {
                **METADATA,
                "authorization_response_iss_parameter_supported": "true",
            }
        },
        {
            "body": {
                **METADATA,
                "scopes_supported": ["urn:ietf:params:oauth:scope:mail"],
            }
        },
        {
            "body": {
                **METADATA,
                "protected_resources": ["https://api.fastmail.com/mcp"],
            }
        },
        # Anything but a JSON answer is not the metadata.
        {"body": METADATA, "headers": {"Content-Type": "text/html"}},
        {"body": METADATA, "headers": {"Content-Type": ""}},
    ]
    for case in cases:
        headers = {"Content-Type": "application/json"}
        headers.update(case.pop("headers", {}))
        with pytest.raises(FastmailOAuthError) as caught:
            fetch_metadata(FakeTransport(reply(headers=headers, **case)))
        assert caught.value.code == "invalid_response"


def test_metadata_without_protected_resources_still_parses():
    body = {k: v for k, v in METADATA.items() if k != "protected_resources"}
    server = fetch_metadata(FakeTransport(metadata_reply(body)))
    assert server.protected_resources == ()


class _Redirector(BaseHTTPRequestHandler):
    hits: list = []

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802 -- required by BaseHTTPRequestHandler.
        type(self).hits.append(self.path)
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
        else:
            page = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)


def test_the_metadata_transport_refuses_redirects():
    _Redirector.hits = []
    server = HTTPServer(("127.0.0.1", 0), _Redirector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(f"http://127.0.0.1:{port}/moved")
        response = urlopen_noredirect_transport(request)
    finally:
        server.shutdown()
        thread.join(timeout=10.0)
        server.server_close()

    assert response.status == 302
    assert _Redirector.hits == ["/moved"]


def _server():
    return fetch_metadata(FakeTransport(metadata_reply()))


def test_registration_posts_an_open_public_client_request():
    transport = FakeTransport(
        reply(
            status=201,
            body={
                "client_id": CLIENT_ID,
                "scope": " ".join(FASTMAIL_SCOPES),
                "redirect_uris": ["http://127.0.0.1/"],
            },
        )
    )

    registration = register_client(_server(), transport=transport)

    request = transport.requests[0]
    assert request.full_url == REGISTRATION_URL
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode()) == {
        "redirect_uris": ["http://127.0.0.1/"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": " ".join(FASTMAIL_SCOPES),
        "client_name": "Postcard",
        "client_uri": "https://postcard.gxanshu.in/",
        "software_id": "in.gxanshu.postcard",
    }
    assert (registration.client_id, registration.scope) == (
        CLIENT_ID,
        " ".join(FASTMAIL_SCOPES),
    )


def test_a_registration_answer_without_a_client_id_is_refused():
    for body in (
        {"scope": " ".join(FASTMAIL_SCOPES)},
        {"client_id": "", "scope": " ".join(FASTMAIL_SCOPES)},
        {"client_id": 42},
        ["client_id"],
    ):
        with pytest.raises(FastmailOAuthError) as caught:
            register_client(_server(), transport=FakeTransport(reply(201, body)))
        assert caught.value.code == "invalid_response"


def test_a_registration_that_narrows_scope_or_redirects_aborts():
    narrowed = {"client_id": CLIENT_ID, "scope": "openid"}
    moved = {"client_id": CLIENT_ID, "redirect_uris": ["http://127.0.0.1/x"]}
    for body in (narrowed, moved):
        with pytest.raises(FastmailOAuthError) as caught:
            register_client(_server(), transport=FakeTransport(reply(201, body)))
        assert caught.value.code == "invalid_response"


def test_a_registration_answer_may_omit_the_echoes():
    registration = register_client(
        _server(), transport=FakeTransport(reply(201, {"client_id": CLIENT_ID}))
    )
    assert (registration.client_id, registration.scope) == (CLIENT_ID, "")


def test_a_client_secret_in_the_answer_is_ignored():
    registration = register_client(
        _server(),
        transport=FakeTransport(
            reply(
                201,
                {"client_id": CLIENT_ID, "client_secret": "shh"},
            )
        ),
    )
    assert registration.client_id == CLIENT_ID


def test_a_refused_registration_keeps_its_code():
    for body, code in (
        ({"error": "invalid_redirect_uri"}, "invalid_request"),
        ({"error": "invalid_client_metadata"}, "invalid_request"),
        ({"error": "temporarily_unavailable"}, "server_error"),
        ({"error": "server_error"}, "server_error"),
        ({}, "server_error"),
    ):
        with pytest.raises(FastmailOAuthError) as caught:
            register_client(_server(), transport=FakeTransport(reply(400, body)))
        assert caught.value.code == code


def test_a_garbled_registration_answer_is_not_a_server_problem():
    transport = FakeTransport(reply(status=201, raw=b"not json"))
    with pytest.raises(FastmailOAuthError) as caught:
        register_client(_server(), transport=transport)
    assert caught.value.code == "invalid_response"


def test_a_registration_outage_stays_generic():
    transport = FakeTransport(reply(status=500, body={"error": "oops"}))
    with pytest.raises(FastmailOAuthError) as caught:
        register_client(_server(), transport=transport)
    assert caught.value.code == "server_error"


def test_the_code_exchange_posts_the_pkce_proof():
    transport = FakeTransport(
        reply(
            body={
                "access_token": "a",
                "refresh_token": "r",
                "token_type": "bearer",
                "expires_in": 3600,
                "scope": " ".join(FASTMAIL_SCOPES),
                "username": "ada@fastmail.com",
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
    assert tokens.username == "ada@fastmail.com"
    assert tokens.scope == " ".join(FASTMAIL_SCOPES)


def test_a_narrowed_grant_aborts_the_exchange():
    transport = FakeTransport(
        reply(body={"access_token": "a", "refresh_token": "r", "scope": "openid"})
    )
    with pytest.raises(FastmailOAuthError) as caught:
        exchange_code(
            "code-1",
            "verifier-1",
            redirect_uri=REDIRECT_URI,
            client_id=CLIENT_ID,
            transport=transport,
        )
    assert caught.value.code == "invalid_response"


def test_an_unnamed_grant_and_user_still_exchange():
    transport = FakeTransport(reply(body={"access_token": "a", "refresh_token": "r"}))
    tokens = exchange_code(
        "code-1",
        "verifier-1",
        redirect_uri=REDIRECT_URI,
        client_id=CLIENT_ID,
        transport=transport,
    )
    assert (tokens.username, tokens.scope) == (None, "")


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


def test_a_dead_client_id_means_reconnect_not_retry():
    transport = FakeTransport(reply(status=400, body={"error": "invalid_client"}))
    with pytest.raises(FastmailOAuthError) as caught:
        refresh_access_token("stale", client_id=CLIENT_ID, transport=transport)
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
        encode_stored("refresh-1", client_id=CLIENT_ID),
        transport=transport,
    )

    assert credential.user == "ada@fastmail.com"
    assert credential.secret == "fresh"
    assert credential.mechanism == "xoauth2"
    form = form_of(transport.requests[0])
    assert form["refresh_token"] == "refresh-1"
    assert form["client_id"] == CLIENT_ID


def test_a_stored_value_that_is_not_ours_is_rejected_before_any_request():
    transport = FakeTransport()
    for stored in (
        "hunter2",
        "{}",
        "[]",
        json.dumps({"refresh_token": ""}),
        json.dumps({"refresh_token": "r"}),
        json.dumps({"refresh_token": "r", "client_id": ""}),
        json.dumps({"client_id": CLIENT_ID}),
    ):
        with pytest.raises(FastmailOAuthError) as caught:
            native_credential("ada@fastmail.com", stored, transport=transport)
        assert caught.value.code == "invalid_request"
    assert transport.requests == []
