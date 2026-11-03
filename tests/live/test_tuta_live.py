"""Tuta against a real account and the compiled helper. Read-only.

Needs POSTCARD_TUTA_EMAIL, POSTCARD_TUTA_PASSWORD and the helper binary
(POSTCARD_TUTA_TEST_BINARY, or postcard-tuta on PATH), plus
POSTCARD_TUTA_TOTP when the account uses a second factor. Logs in, lists the
folders, reads a single sync page and decrypts one body; it never flags,
moves or sends.
"""

import os
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

import pytest

# tests/live has no __init__.py, so pytest prepends it to sys.path and the
# helper module imports by its bare name.
from harness import require_env, require_helper

from postcard.core.net import tuta_messages
from postcard.core.net.tuta_session import TutaSession, login

pytestmark = pytest.mark.live


def test_login_lists_folders_and_reads_one_page():
    binary = require_helper("POSTCARD_TUTA_TEST_BINARY", "postcard-tuta")
    email = require_env("POSTCARD_TUTA_EMAIL")
    password = require_env("POSTCARD_TUTA_PASSWORD")
    totp = os.environ.get("POSTCARD_TUTA_TOTP")
    with patch("shutil.which", return_value=binary):
        credential = login(email, password, totp)
        session = TutaSession(credential)
        reply = session.request("sync", folder=None, limit=1, offset=0)
        bodies = [
            tuta_messages.fetch_mime(session, tuta_messages.message_header(row).uid)
            for row in reply["messages"]
        ]
    folders = tuta_messages.mailbox_infos(reply["folders"])
    assert folders, "the live account exposes no folders"
    assert all(folder.name for folder in folders)
    for raw in bodies:
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        assert parsed["Subject"] is not None
        assert parsed["Date"] is not None
