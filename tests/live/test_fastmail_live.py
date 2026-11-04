"""Native Fastmail refresh against api.fastmail.com, then one read-only JMAP call.

Needs POSTCARD_FASTMAIL_CLIENT_ID and POSTCARD_FASTMAIL_REFRESH_TOKEN (the
stored form from fastmail_oauth.encode_stored, a JSON object with a
refresh_token member). There is no browser here: this covers the refresh path
the app uses on every operation, not the first interactive login. It refreshes
exactly once -- Fastmail rotates the refresh token on every refresh, and
reusing the old one revokes the grant -- then lists the mailboxes and writes
nothing.
"""

import json
import os

import pytest

# tests/live has no __init__.py, so pytest prepends it to sys.path and the
# helper module imports by its bare name.
from harness import require_env

from postcard.core.models.folder import FolderRole
from postcard.core.net.auth import MECHANISM_XOAUTH2, Credential
from postcard.core.net.fastmail_oauth import (
    FASTMAIL_JMAP_SESSION_URL,
    refresh_access_token,
)
from postcard.core.net.jmap_folders import list_mailboxes
from postcard.core.net.jmap_session import JmapSession, discover

pytestmark = pytest.mark.live


def test_refresh_mints_a_token_that_lists_mailboxes():
    client_id = require_env("POSTCARD_FASTMAIL_CLIENT_ID")
    stored = require_env("POSTCARD_FASTMAIL_REFRESH_TOKEN")
    email = os.environ.get("POSTCARD_FASTMAIL_EMAIL", "live@example.com")
    refresh_token = json.loads(stored).get("refresh_token")
    assert isinstance(refresh_token, str) and refresh_token
    tokens = refresh_access_token(refresh_token, client_id=client_id)
    assert tokens.refresh_token, "Fastmail always rotates the refresh token"
    credential = Credential(email, tokens.access_token, MECHANISM_XOAUTH2)
    session = discover(FASTMAIL_JMAP_SESSION_URL, credential)
    mailboxes = list_mailboxes(JmapSession(session, credential))
    assert any(mailbox.role is FolderRole.INBOX for mailbox in mailboxes)
