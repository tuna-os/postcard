"""Composer delivery choice for Tuta accounts, where the widgets exist.

A Tuta composer shows secure delivery as visibly unavailable and enables
Send only after the ordinary-delivery opt-in; other providers see neither
the choice nor any gating. Runs via `just test-gui`: inside the installed
Flatpak under Xvfb. Skips anywhere without a display.
"""

import os

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio
except (ImportError, ValueError):
    pytest.skip("GTK/Adw typelibs unavailable", allow_module_level=True)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    pytest.skip("no display", allow_module_level=True)

os.environ.setdefault("GTK_A11Y", "none")
Adw.init()


def _register_resources():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        "/app/share/postcard/postcard.gresource",
        os.path.join(here, "..", "..", "build", "src", "postcard.gresource"),
    ):
        if os.path.exists(candidate):
            Gio.resources_register(Gio.Resource.load(candidate))
            return
    pytest.skip("no compiled gresource", allow_module_level=True)


_register_resources()

from postcard.composer_window import PostcardComposerWindow
from postcard.core.models.account import (
    PROTOCOL_IMAP,
    PROTOCOL_TUTA,
    TUTA_HOST,
    TUTA_PORT,
    Account,
)
from postcard.core.store.database import Database


def _account(protocol, email):
    return Account(
        id=9,
        email=email,
        display_name="Ada",
        protocol=protocol,
        imap_host=TUTA_HOST if protocol == PROTOCOL_TUTA else "imap.example.com",
        imap_port=TUTA_PORT if protocol == PROTOCOL_TUTA else 993,
        smtp_host=TUTA_HOST if protocol == PROTOCOL_TUTA else "smtp.example.com",
        smtp_port=TUTA_PORT if protocol == PROTOCOL_TUTA else 465,
    )


def _composer(account):
    return PostcardComposerWindow(
        None, Database(":memory:"), account, to="bo@tuta.com", subject="hello"
    )


def test_tuta_send_waits_for_the_explicit_ordinary_opt_in():
    window = _composer(_account(PROTOCOL_TUTA, "ada@tuta.com"))
    try:
        assert window._delivery_group.get_visible()
        assert not window._secure_radio.get_sensitive()
        assert not window._ordinary_radio.get_active()
        assert not window._actions["send"].get_enabled()
        window._ordinary_radio.set_active(True)
        assert window._actions["send"].get_enabled()
    finally:
        window.destroy()


def test_secure_delivery_never_enables_send():
    window = _composer(_account(PROTOCOL_TUTA, "ada@tuta.com"))
    try:
        # Even forced active, the secure option feeds no choice: there is no
        # path from it to an enabled Send.
        window._secure_radio.set_active(True)
        assert window._tuta_delivery is None
        assert not window._actions["send"].get_enabled()
    finally:
        window.destroy()


def test_other_providers_see_no_delivery_choice():
    window = _composer(_account(PROTOCOL_IMAP, "ada@example.com"))
    try:
        assert not window._delivery_group.get_visible()
        assert window._actions["send"].get_enabled()
    finally:
        window.destroy()
