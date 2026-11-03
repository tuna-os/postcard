"""Native Microsoft refresh against Entra, then one read-only Graph call.

Needs POSTCARD_MS_CLIENT_ID and POSTCARD_MS_REFRESH_TOKEN (the stored form
from ms_oauth.encode_stored, a JSON object with a refresh_token member).
There is no browser here: this covers the refresh path the app uses on every
operation, not the first interactive login. It reads one folder page and
writes nothing.
"""

import os

import pytest

# tests/live has no __init__.py, so pytest prepends it to sys.path and the
# helper module imports by its bare name.
from harness import require_env

from postcard.core.net.auth import MECHANISM_XOAUTH2
from postcard.core.net.graph_session import GraphSession
from postcard.core.net.ms_oauth import native_credential

pytestmark = pytest.mark.live


def test_refresh_mints_a_token_that_reads_folders():
    client_id = require_env("POSTCARD_MS_CLIENT_ID")
    stored = require_env("POSTCARD_MS_REFRESH_TOKEN")
    email = os.environ.get("POSTCARD_MS_EMAIL", "live@example.com")
    credential = native_credential(email, stored, client_id=client_id)
    assert credential.mechanism == MECHANISM_XOAUTH2
    response = GraphSession(credential).request("GET", "/me/mailFolders?$top=1")
    assert isinstance(response.body.get("value"), list)
