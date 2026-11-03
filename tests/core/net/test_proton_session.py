import json
import subprocess
from unittest.mock import patch

import pytest

from postcard.core.net.auth import Credential
from postcard.core.net.proton_session import (
    ERROR_CODES,
    MECHANISM_PROTON,
    ProtonError,
    ProtonSession,
    helper_transport,
    login,
)

SESSION = {
    "login": "ada@proton.me",
    "user_id": "user",
    "access_token": "secret-token",
    "encrypted_key_packet": [1, 2, 3],
}
CREDENTIAL = Credential(SESSION["login"], json.dumps(SESSION), MECHANISM_PROTON)


def test_the_stub_only_names_its_own_codes():
    assert "unsupported_operation" in ERROR_CODES
    assert "helper_missing" in ERROR_CODES
    assert MECHANISM_PROTON == "proton-session"


def test_the_message_names_no_server_detail():
    error = ProtonError("unsupported_operation")
    assert error.code == "unsupported_operation"
    assert "unsupported_operation" not in str(error)


def test_login_returns_session_without_retaining_password():
    requests = []

    def transport(request):
        requests.append(request)
        return {"version": 1, "result": {"session": SESSION}}

    result = login(SESSION["login"], "fixture-password", "001234", transport=transport)
    assert result == CREDENTIAL
    assert "fixture-password" not in result.secret
    assert "secret-token" not in repr(result)
    assert requests[0]["totp"] == "001234"


def test_session_cannot_use_a_password_or_oauth_token():
    for mechanism in ("login", "xoauth2", "tuta-session"):
        with pytest.raises(ProtonError):
            ProtonSession(Credential("ada@proton.me", "password", mechanism))


def test_session_is_bound_to_account_identity():
    with pytest.raises(ProtonError):
        ProtonSession(
            Credential("other@proton.me", CREDENTIAL.secret, MECHANISM_PROTON)
        )


@pytest.mark.parametrize(
    "reply",
    [
        {"version": 2, "result": {}},
        {"version": 1, "result": []},
        {"version": 1},
        {"version": 1, "error": "secret-token"},
        {"version": 1, "error": {"code": "secret-token"}},
    ],
)
def test_malformed_response_and_unknown_errors_are_redacted(reply):
    with pytest.raises(ProtonError) as caught:
        ProtonSession(CREDENTIAL, lambda _: reply).request("sync")
    assert "secret-token" not in str(caught.value)
    assert "secret-token" not in caught.value.code


def test_helper_receives_secrets_only_through_stdin():
    with (
        patch("shutil.which", return_value="/app/bin/postcard-proton"),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, '{"version":1,"result":{}}'
            ),
        ) as run,
    ):
        helper_transport({"secret": "fixture-password"})
    args, kwargs = run.call_args
    assert args == (["/app/bin/postcard-proton"],)
    assert json.loads(kwargs["input"]) == {"secret": "fixture-password"}
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] > 0
    assert "shell" not in kwargs


@pytest.mark.parametrize(
    "failure",
    [
        OSError("fixture-password"),
        subprocess.TimeoutExpired(["postcard-proton"], 100, output="secret-token"),
    ],
)
def test_process_failures_hide_secret_diagnostics(failure):
    with (
        patch("shutil.which", return_value="/app/bin/postcard-proton"),
        patch("subprocess.run", side_effect=failure),
        pytest.raises(ProtonError) as caught,
    ):
        helper_transport({})
    assert "fixture-password" not in str(caught.value)
    assert "secret-token" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_missing_helper_is_explicit():
    with patch("shutil.which", return_value=None), pytest.raises(ProtonError) as caught:
        helper_transport({})
    assert caught.value.code == "helper_missing"


def test_a_helper_that_exits_uncleanly_fails_as_one():
    completed = subprocess.CompletedProcess(["postcard-proton"], 1, "")
    with (
        patch("shutil.which", return_value="/app/bin/postcard-proton"),
        patch("subprocess.run", return_value=completed),
        pytest.raises(ProtonError) as caught,
    ):
        helper_transport({})
    assert caught.value.code == "helper_failed"


@pytest.mark.parametrize("stdout", ["not json", "[]"])
def test_a_shapeless_helper_reply_is_not_a_session(stdout):
    completed = subprocess.CompletedProcess(["postcard-proton"], 0, stdout)
    with (
        patch("shutil.which", return_value="/app/bin/postcard-proton"),
        patch("subprocess.run", return_value=completed),
        pytest.raises(ProtonError) as caught,
    ):
        helper_transport({})
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "session",
    [{"login": "other@proton.me"}, "not-a-session"],
)
def test_login_rejects_a_session_for_someone_else(session):
    def transport(request):
        return {"version": 1, "result": {"session": session}}

    with pytest.raises(ProtonError) as caught:
        login(SESSION["login"], "fixture-password", transport=transport)
    assert caught.value.code == "invalid_response"


def test_a_session_cannot_resume_a_shapeless_secret():
    credential = Credential(SESSION["login"], "not-json", MECHANISM_PROTON)
    with pytest.raises(ProtonError) as caught:
        ProtonSession(credential)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize(
    "code",
    [
        "login_failed",
        "second_factor_required",
        "invalid_totp",
        "unsupported_second_factor",
        "second_factor_failed",
        "request_failed",
        "timeout",
    ],
)
def test_helper_failure_codes_reach_the_caller_verbatim(code):
    def transport(request):
        return {"version": 1, "error": {"code": code}}

    with pytest.raises(ProtonError) as caught:
        login(SESSION["login"], "fixture-password", transport=transport)
    assert caught.value.code == code


def test_compiled_helper_speaks_the_pinned_contract_without_network():
    import os

    binary = os.environ.get("POSTCARD_PROTON_TEST_BINARY")
    if not binary:
        reason = "set POSTCARD_PROTON_TEST_BINARY to run the compiled helper contract"
        pytest.skip(reason)
    with patch("shutil.which", return_value=binary):
        reply = helper_transport({"version": 2, "op": "login"})
        assert reply == {"version": 1, "error": {"code": "unsupported_version"}}
        reply = helper_transport({"version": 1, "op": "sync", "session": SESSION})
        assert reply == {"version": 1, "error": {"code": "unsupported_operation"}}
        with pytest.raises(ProtonError) as caught:
            ProtonSession(CREDENTIAL).request("send")
    assert caught.value.code == "invalid_request"
