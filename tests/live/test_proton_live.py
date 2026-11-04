"""Proton login against a real account and the compiled helper.

Needs POSTCARD_PROTON_EMAIL, POSTCARD_PROTON_PASSWORD and the helper binary
(POSTCARD_PROTON_TEST_BINARY, or postcard-proton on PATH), plus
POSTCARD_PROTON_TOTP when the account uses a second factor. Proves the SRP
login and returns the resumable session; it reads nothing else and the
password never comes back in the session blob.
"""

import json
import os
from unittest.mock import patch

import pytest

# tests/live has no __init__.py, so pytest prepends it to sys.path and the
# helper module imports by its bare name.
from harness import require_env, require_helper

from postcard.core.net.proton_session import (
    MECHANISM_PROTON,
    ProtonError,
    login,
)

pytestmark = pytest.mark.live


def test_login_returns_a_resumable_session_without_the_password():
    binary = require_helper("POSTCARD_PROTON_TEST_BINARY", "postcard-proton")
    email = require_env("POSTCARD_PROTON_EMAIL")
    password = require_env("POSTCARD_PROTON_PASSWORD")
    totp = os.environ.get("POSTCARD_PROTON_TOTP")
    try:
        with patch("shutil.which", return_value=binary):
            credential = login(email, password, totp)
    except ProtonError as error:
        # The daily unblock check reads this line: the fixed-vocabulary code
        # says whether the server refused, the TOTP failed, or the helper did.
        pytest.fail(f"Proton login failed: {error.code}")
    assert credential.mechanism == MECHANISM_PROTON
    assert password not in credential.secret
    session = json.loads(credential.secret)
    assert session["login"].casefold() == email.casefold()
    assert session["uid"]
    assert session["access_token"]
    assert session["refresh_token"]
