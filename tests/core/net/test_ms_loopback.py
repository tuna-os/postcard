import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer
from unittest.mock import patch

import pytest

from postcard.core.net.ms_loopback import LoopbackRedirect
from postcard.core.net.ms_oauth import MsOAuthError


def fetch_all(urls, results, timeout=10.0):
    """GET each URL in order on a thread, recording (status, body)."""

    def run():
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=timeout) as reply:
                    results.append((reply.status, reply.read()))
            except urllib.error.HTTPError as error:
                results.append((error.code, error.read()))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_listeners_bind_ephemeral_loopback_ports():
    with LoopbackRedirect() as first, LoopbackRedirect() as second:
        for redirect in (first, second):
            parts = urllib.parse.urlsplit(redirect.redirect_uri)
            assert (parts.hostname, parts.path) == ("127.0.0.1", "/")
            assert parts.port not in (None, 80, 443)
        assert first.redirect_uri != second.redirect_uri


def test_a_matching_callback_returns_the_code():
    with LoopbackRedirect() as redirect:
        results = []
        thread = fetch_all(
            [f"{redirect.redirect_uri}?code=code-1&state=state-1"], results
        )
        try:
            assert redirect.wait_for_code("state-1", timeout=10.0) == "code-1"
        finally:
            thread.join(timeout=10.0)
    [(status, body)] = results
    assert status == 200
    assert b"code-1" not in body
    assert b"state-1" not in body


def test_a_wrong_state_does_not_consume_the_login():
    with LoopbackRedirect() as redirect:
        results = []
        thread = fetch_all(
            [
                f"{redirect.redirect_uri}?code=code-wrong&state=state-wrong",
                f"{redirect.redirect_uri}?code=code-1&state=state-1",
            ],
            results,
        )
        try:
            assert redirect.wait_for_code("state-1", timeout=10.0) == "code-1"
        finally:
            thread.join(timeout=10.0)
    assert [status for status, _ in results] == [400, 200]
    assert all(b"code-wrong" not in body for _, body in results)


@pytest.mark.parametrize(
    ("bad_path", "status"),
    [
        ("/favicon.ico", 404),
        ("/?code=&state=state-1", 400),
        ("/?code=code-1", 400),
        ("/?state=state-1", 400),
    ],
)
def test_a_stray_request_is_answered_and_ignored(bad_path, status):
    with LoopbackRedirect() as redirect:
        results = []
        thread = fetch_all(
            [
                redirect.redirect_uri.removesuffix("/") + bad_path,
                f"{redirect.redirect_uri}?code=code-1&state=state-1",
            ],
            results,
        )
        try:
            assert redirect.wait_for_code("state-1", timeout=10.0) == "code-1"
        finally:
            thread.join(timeout=10.0)
    assert [result[0] for result in results] == [status, 200]


def test_a_provider_refusal_is_a_grant_failure_without_provider_text():
    with LoopbackRedirect() as redirect:
        results = []
        thread = fetch_all(
            [
                f"{redirect.redirect_uri}"
                "?error=access_denied&error_description=tenant-secret&state=s"
            ],
            results,
        )
        try:
            with pytest.raises(MsOAuthError) as caught:
                redirect.wait_for_code("s", timeout=10.0)
        finally:
            thread.join(timeout=10.0)
    assert caught.value.code == "invalid_grant"
    [(status, body)] = results
    assert status == 400
    assert b"tenant-secret" not in body
    assert b"access_denied" not in body


def test_waiting_times_out_without_a_callback():
    with (
        LoopbackRedirect() as redirect,
        pytest.raises(MsOAuthError) as caught,
    ):
        redirect.wait_for_code("state-1", timeout=0.05)
    assert caught.value.code == "invalid_request"


def test_an_empty_state_or_timeout_is_rejected_before_serving():
    with LoopbackRedirect() as redirect:
        with pytest.raises(MsOAuthError) as caught:
            redirect.wait_for_code("", timeout=10.0)
        assert caught.value.code == "invalid_request"
        with pytest.raises(MsOAuthError) as caught:
            redirect.wait_for_code("state-1", timeout=0)
        assert caught.value.code == "invalid_request"
        assert redirect._server.requests == []


def test_a_listener_that_cannot_bind_fails_as_a_bad_request():
    with (
        patch.object(HTTPServer, "server_bind", side_effect=OSError("denied")),
        pytest.raises(MsOAuthError) as caught,
    ):
        LoopbackRedirect()
    assert caught.value.code == "invalid_request"


def test_broken_accepts_count_as_knocks_then_give_up():
    with (
        LoopbackRedirect() as redirect,
        patch.object(
            HTTPServer, "handle_request", side_effect=OSError("accept failed")
        ),
        pytest.raises(MsOAuthError) as caught,
    ):
        redirect.wait_for_code("state-1", timeout=10.0)
    assert caught.value.code == "invalid_request"
    assert len(redirect._server.requests) == 20
