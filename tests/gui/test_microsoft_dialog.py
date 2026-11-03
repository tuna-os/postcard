"""Native Microsoft onboarding, exercised where the Adw typelib exists.

Skips on the host (no display) and in CI. Run inside the Flatpak under Xvfb:

    xvfb-run -a flatpak run --command=bash in.gxanshu.postcard \\
        -c "python3 -m pip install -q --user pytest && <this file> ..."
"""

import os

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk
except (ImportError, ValueError):
    pytest.skip("GTK/Adw typelibs unavailable", allow_module_level=True)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    pytest.skip("no display", allow_module_level=True)

os.environ.setdefault("GTK_A11Y", "none")
# The running app inits Adw via Adw.Application; a bare test does it here so
# the template builder knows the Adw types.
Adw.init()


def _register_resources():
    # The running app loads its GResource via resource_base_path; a bare test
    # interpreter registers the compiled bundle itself, or skips.
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

import time

from postcard.accounts_dialog import PostcardAccountsDialog
from postcard.core.net import ms_login
from postcard.core.net.ms_oauth import MsOAuthError, Tokens
from postcard.core.store.database import Database
from postcard.microsoft_dialog import PostcardMicrosoftDialog

_KEY = bytes(range(32))


LOGIN = ms_login.NativeLogin(
    Tokens("access-token", "refresh-token", 3600), '{"refresh_token": "r"}'
)


def pretend_browser_exists(monkeypatch):
    """The sandbox has no browser; the pre-check is covered by inspection."""
    monkeypatch.setattr(
        Gio.AppInfo,
        "get_default_for_uri_scheme",
        classmethod(lambda cls, scheme: object()),
    )


def pump_main_loop_until(predicate, timeout=10.0):
    """Run main-loop iterations until predicate passes or time runs out."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        context.iteration(True)
    assert predicate()


def test_sign_in_saves_a_graph_account(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    stored = {}
    monkeypatch.setattr(
        "postcard.microsoft_dialog.ms_login.log_in", lambda **kwargs: LOGIN
    )
    monkeypatch.setattr(
        "postcard.microsoft_dialog.secrets.store_password",
        lambda account_id, secret: stored.setdefault(account_id, secret),
    )
    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "test-client")
    pretend_browser_exists(monkeypatch)
    dialog = PostcardMicrosoftDialog(db)
    added = []
    dialog.connect("account-added", lambda *args: added.append(True))
    dialog.email_row.set_text("ada@outlook.com")
    assert dialog.sign_in_button.get_sensitive()

    dialog.sign_in_button.emit("clicked")
    assert dialog.dialog_stack.get_visible_child_name() == "waiting"
    pump_main_loop_until(lambda: added)

    [account] = db.accounts()
    assert (account.email, account.protocol, account.goa_id) == (
        "ada@outlook.com",
        "graph",
        "",
    )
    assert stored[account.id] == LOGIN.stored
    db.close()


def test_a_refused_login_returns_to_the_form(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fail(**kwargs):
        raise MsOAuthError("invalid_grant")

    monkeypatch.setattr("postcard.microsoft_dialog.ms_login.log_in", fail)
    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "test-client")
    pretend_browser_exists(monkeypatch)
    dialog = PostcardMicrosoftDialog(db)
    dialog.email_row.set_text("ada@outlook.com")

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.dialog_stack.get_visible_child_name() == "form")

    assert db.accounts() == []
    db.close()


def test_an_invalid_address_never_starts_login(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "test-client")
    dialog = PostcardMicrosoftDialog(db)
    dialog.email_row.set_text("not-an-address")

    assert not dialog.sign_in_button.get_sensitive()
    dialog.sign_in_button.emit("clicked")

    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert db.accounts() == []
    db.close()


def test_an_existing_address_is_not_added_twice(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    db.save_account("ada@outlook.com", "Ada", "h", 993, "h", 587)
    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "test-client")
    dialog = PostcardMicrosoftDialog(db)
    dialog.email_row.set_text("ADA@outlook.com")

    dialog.sign_in_button.emit("clicked")

    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert len(db.accounts()) == 1
    db.close()


def test_sign_in_without_a_browser_never_starts_login(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fail(**kwargs):
        pytest.fail("login started with no browser to complete it")

    monkeypatch.setattr("postcard.microsoft_dialog.ms_login.log_in", fail)
    monkeypatch.setattr(
        Gio.AppInfo,
        "get_default_for_uri_scheme",
        classmethod(lambda cls, scheme: None),
    )
    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "test-client")
    dialog = PostcardMicrosoftDialog(db)
    dialog.email_row.set_text("ada@outlook.com")

    dialog.sign_in_button.emit("clicked")

    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert db.accounts() == []
    db.close()


def test_the_entry_point_hides_until_an_app_is_registered(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "")
    assert not PostcardAccountsDialog(db).microsoft_button.get_visible()

    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "test-client")
    assert PostcardAccountsDialog(db).microsoft_button.get_visible()
    db.close()


def test_dialog_template_ids_match(monkeypatch, tmp_path):
    """Constructing the dialog proves every Template.Child id resolves."""
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    dialog = PostcardMicrosoftDialog(db)

    assert isinstance(dialog.toast_overlay, Gtk.Widget)
    assert isinstance(dialog.dialog_stack, Gtk.Stack)
    db.close()
