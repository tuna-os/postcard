import json
import subprocess
from unittest.mock import patch

import pytest

from postcard.core.net.auth import Credential
from postcard.core.net.tuta_session import (
    MECHANISM_TUTA,
    TutaError,
    TutaSession,
    helper_transport,
    is_valid_totp,
    login,
)

SESSION = {
    "login": "ada@tuta.com",
    "user_id": "user",
    "access_token": "secret-token",
    "encrypted_passphrase_key": [1, 2, 3],
}
CREDENTIAL = Credential(SESSION["login"], json.dumps(SESSION), MECHANISM_TUTA)


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
    for mechanism in ("login", "xoauth2"):
        with pytest.raises(TutaError):
            TutaSession(Credential("ada@tuta.com", "password", mechanism))


def test_session_is_bound_to_account_identity():
    with pytest.raises(TutaError):
        TutaSession(Credential("other@tuta.com", CREDENTIAL.secret, MECHANISM_TUTA))


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
    with pytest.raises(TutaError) as caught:
        TutaSession(CREDENTIAL, lambda _: reply).request("sync")
    assert "secret-token" not in str(caught.value)
    assert "secret-token" not in caught.value.code


def test_helper_receives_secrets_only_through_stdin():
    with (
        patch("shutil.which", return_value="/app/bin/postcard-tuta"),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, '{"version":1,"result":{}}'
            ),
        ) as run,
    ):
        helper_transport({"secret": "fixture-password"})
    args, kwargs = run.call_args
    assert args == (["/app/bin/postcard-tuta"],)
    assert json.loads(kwargs["input"]) == {"secret": "fixture-password"}
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] > 0
    assert "shell" not in kwargs


@pytest.mark.parametrize(
    "failure",
    [
        OSError("fixture-password"),
        subprocess.TimeoutExpired(["postcard-tuta"], 100, output="secret-token"),
    ],
)
def test_process_failures_hide_secret_diagnostics(failure):
    with (
        patch("shutil.which", return_value="/app/bin/postcard-tuta"),
        patch("subprocess.run", side_effect=failure),
        pytest.raises(TutaError) as caught,
    ):
        helper_transport({})
    assert "fixture-password" not in str(caught.value)
    assert "secret-token" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_missing_helper_is_explicit():
    with patch("shutil.which", return_value=None), pytest.raises(TutaError) as caught:
        helper_transport({})
    assert caught.value.code == "helper_missing"


def test_a_helper_that_exits_uncleanly_fails_as_one():
    completed = subprocess.CompletedProcess(["postcard-tuta"], 1, "")
    with (
        patch("shutil.which", return_value="/app/bin/postcard-tuta"),
        patch("subprocess.run", return_value=completed),
        pytest.raises(TutaError) as caught,
    ):
        helper_transport({})
    assert caught.value.code == "helper_failed"


@pytest.mark.parametrize("stdout", ["not json", "[]"])
def test_a_shapeless_helper_reply_is_not_a_session(stdout):
    completed = subprocess.CompletedProcess(["postcard-tuta"], 0, stdout)
    with (
        patch("shutil.which", return_value="/app/bin/postcard-tuta"),
        patch("subprocess.run", return_value=completed),
        pytest.raises(TutaError) as caught,
    ):
        helper_transport({})
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "session",
    [{"login": "other@tuta.com"}, "not-a-session"],
)
def test_login_rejects_a_session_for_someone_else(session):
    def transport(request):
        return {"version": 1, "result": {"session": session}}

    with pytest.raises(TutaError) as caught:
        login(SESSION["login"], "fixture-password", transport=transport)
    assert caught.value.code == "invalid_response"


def test_a_session_cannot_resume_a_shapeless_secret():
    credential = Credential(SESSION["login"], "not-json", MECHANISM_TUTA)
    with pytest.raises(TutaError) as caught:
        TutaSession(credential)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize(
    ("code", "is_valid"),
    [
        ("001234", True),
        ("000000", True),
        ("", False),
        ("12345", False),
        ("1234567", False),
        ("12345a", False),
        (" 12345", False),
        # Non-ASCII numerals pass str.isdigit but the helper rejects them.
        ("١٢٣٤٥٦", False),
    ],
)
def test_totp_codes_are_six_ascii_digits(code, is_valid):
    assert is_valid_totp(code) is is_valid


def test_login_challenge_and_totp_retry_are_distinct_calls():
    calls = []

    def transport(request):
        calls.append(request)
        if request["totp"] is None:
            return {"version": 1, "error": {"code": "second_factor_required"}}
        return {"version": 1, "result": {"session": SESSION}}

    with pytest.raises(TutaError) as challenged:
        login(SESSION["login"], "fixture-password", transport=transport)
    assert challenged.value.code == "second_factor_required"
    credential = login(
        SESSION["login"], "fixture-password", "001234", transport=transport
    )
    assert credential == CREDENTIAL
    assert [call["totp"] for call in calls] == [None, "001234"]


@pytest.mark.parametrize(
    "code",
    [
        "session_revoked",
        "session_invalid",
        "client_outdated",
        "revoke_failed",
        "unsupported_u2f",
        "unsupported_webauthn",
    ],
)
def test_resume_and_factor_taxonomy_survives_the_transport(code):
    def transport(request):
        return {"version": 1, "error": {"code": code}}

    with pytest.raises(TutaError) as caught:
        TutaSession(CREDENTIAL, transport).request("sync")
    assert caught.value.code == code


def test_revoke_sends_the_stored_session():
    requests = []

    def transport(request):
        requests.append(request)
        return {"version": 1, "result": {}}

    TutaSession(CREDENTIAL, transport).request("revoke")
    assert requests == [{"version": 1, "op": "revoke", "session": SESSION}]


def test_compiled_helper_rejects_incompatible_protocol_without_network():
    import os

    binary = os.environ.get("POSTCARD_TUTA_TEST_BINARY")
    if not binary:
        pytest.skip("set POSTCARD_TUTA_TEST_BINARY to run the compiled helper contract")
    with patch("shutil.which", return_value=binary), pytest.raises(TutaError) as caught:
        helper = TutaSession(CREDENTIAL)
        # Bypass the matching client version to exercise the actual executable.
        reply = helper_transport(
            {
                "version": 2,
                "op": "login",
                "email": SESSION["login"],
                "password": "fixture-password",
            }
        )
        assert reply == {"version": 1, "error": {"code": "unsupported_version"}}
        helper.request("send")
    assert caught.value.code == "invalid_request"
