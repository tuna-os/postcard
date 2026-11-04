"""The provider picker offers every provider with its sign-in path.

Rows render from the catalog in order, gated rows stay visible but
insensitive with their reason, and activating a row opens the mapped
dialog. No test touches a live server: gates are monkeypatched flags.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display.
"""

import os

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, Gtk
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

from postcard.account_dialog import PostcardAccountDialog  # noqa: E402
from postcard.core.providers.catalog import PROVIDERS  # noqa: E402
from postcard.core.store.database import Database  # noqa: E402
from postcard.fastmail_dialog import PostcardFastmailDialog  # noqa: E402
from postcard.microsoft_dialog import PostcardMicrosoftDialog  # noqa: E402
from postcard.online_accounts_dialog import PostcardOnlineAccountsDialog  # noqa: E402
from postcard.proton_dialog import PostcardProtonDialog  # noqa: E402
from postcard.provider_dialog import PostcardProviderDialog  # noqa: E402
from postcard.tuta_dialog import PostcardTutaDialog  # noqa: E402

_KEY = bytes(range(32))


def _rows(dialog):
    # The group nests rows inside an internal list box, so walk down.
    rows = []

    def walk(widget):
        if isinstance(widget, Adw.ActionRow):
            rows.append(widget)
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(dialog.providers_group)
    return rows


def _row_icons(row):
    names = []

    def walk(widget):
        if isinstance(widget, Gtk.Image):
            names.append(widget.get_icon_name())
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(row)
    return names


def _row_by_icon(rows, icon_name):
    for row in rows:
        if icon_name in _row_icons(row):
            return row
    raise AssertionError(f"no row with icon {icon_name}")


def test_every_provider_gets_a_row_with_its_badge(tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    dialog = PostcardProviderDialog(db)
    rows = _rows(dialog)

    assert len(rows) == len(PROVIDERS)
    for row, provider in zip(rows, PROVIDERS, strict=True):
        assert provider.icon_name in _row_icons(row)
        assert row.get_title()
        assert row.get_subtitle()

    db.close()


def test_oauth_rows_carry_the_browser_mark(tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    rows = _rows(PostcardProviderDialog(db))

    for icon in ("postcard-provider-microsoft", "postcard-provider-fastmail"):
        assert "web-browser-symbolic" in _row_icons(_row_by_icon(rows, icon))
    for icon in ("postcard-provider-tuta", "postcard-provider-other"):
        assert "web-browser-symbolic" not in _row_icons(_row_by_icon(rows, icon))

    db.close()


def test_gated_rows_stay_visible_but_insensitive(tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    rows = _rows(PostcardProviderDialog(db))

    # No client ids ship and no Proton helper is installed, so the gated
    # rows explain themselves instead of hiding; the rest are live.
    for icon in (
        "postcard-provider-microsoft",
        "postcard-provider-fastmail",
        "postcard-provider-proton",
    ):
        assert not _row_by_icon(rows, icon).get_sensitive()
    for icon in (
        "postcard-provider-google",
        "postcard-provider-tuta",
        "postcard-provider-other",
    ):
        assert _row_by_icon(rows, icon).get_sensitive()

    db.close()


def test_a_shipped_client_id_enables_its_row(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "test-client")
    rows = _rows(PostcardProviderDialog(db))
    assert _row_by_icon(rows, "postcard-provider-microsoft").get_sensitive()

    monkeypatch.setattr("postcard.core.net.ms_oauth.MS_CLIENT_ID", "")
    monkeypatch.setattr(
        "postcard.core.net.fastmail_oauth.FASTMAIL_CLIENT_ID", "test-client"
    )
    rows = _rows(PostcardProviderDialog(db))
    assert _row_by_icon(rows, "postcard-provider-fastmail").get_sensitive()

    db.close()


def test_an_installed_helper_enables_the_proton_row(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    monkeypatch.setattr(
        "postcard.core.net.proton_session.helper_available", lambda: True
    )
    rows = _rows(PostcardProviderDialog(db))
    assert _row_by_icon(rows, "postcard-provider-proton").get_sensitive()

    db.close()


def test_each_row_opens_its_dialog(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    assert {
        "google": PostcardOnlineAccountsDialog,
        "microsoft": PostcardMicrosoftDialog,
        "fastmail": PostcardFastmailDialog,
        "proton": PostcardProtonDialog,
        "tuta": PostcardTutaDialog,
        "other": PostcardAccountDialog,
    } == PostcardProviderDialog._DIALOGS

    presented = []

    class FakeDialog:
        def __init__(self, db):
            presented.append(db)

        def connect(self, _signal, _handler):
            pass

        def present(self, _parent):
            presented.append("presented")

    monkeypatch.setitem(PostcardProviderDialog._DIALOGS, "tuta", FakeDialog)
    rows = _rows(PostcardProviderDialog(db))
    _row_by_icon(rows, "postcard-provider-tuta").emit("activated")

    assert presented == [db, "presented"]
    db.close()
