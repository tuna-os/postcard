"""Shared gating for the tests that touch real accounts.

Every live test skips unless its credentials are present, and the `live`
marker keeps the whole directory out of a bare `pytest` run even when they
are. Run them explicitly:

    pytest -m live tests/live/
"""

import os
import shutil

import pytest


def require_env(name: str) -> str:
    """The credential, or skip naming the variable that would enable it."""
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"set {name} to run this live test")
    return value


def require_helper(env_name: str, helper_name: str) -> str:
    """The helper binary: an explicit path, else whatever is on PATH."""
    override = os.environ.get(env_name, "")
    if override:
        return override
    found = shutil.which(helper_name)
    if found is None:
        pytest.skip(f"set {env_name} or put {helper_name} on PATH")
    return found
