"""GNOME Online Accounts stays optional.

An isolated profile has no org.gnome.OnlineAccounts service on the bus, and a
broken one answers with garbage. Either way only GOA-owned accounts may fail --
app-owned accounts and startup never touch this module -- and the failure reads
as "could not sign in", the same None the keyring branch returns when it has no
password. Workers call secrets.credential_for outside their try block, so these
entry points must never raise.

The bus is faked at the Gio.DBusProxy layer: no live D-Bus service needed.
"""

from types import SimpleNamespace

import pytest
from gi.repository import Gio, GLib

from postcard.core import goa
from postcard.core.net import NET_TIMEOUT_SECONDS
from postcard.core.net.auth import MECHANISM_XOAUTH2

_ACCOUNT = "org.gnome.OnlineAccounts.Account"
_MAIL = "org.gnome.OnlineAccounts.Mail"
_OAUTH2 = "org.gnome.OnlineAccounts.OAuth2Based"

_DBUS_QUARK = GLib.quark_from_string("g-dbus-error-quark")


def bus_error(message: str = "the name was not provided by any .service files"):
    return GLib.Error.new_literal(_DBUS_QUARK, message, 4)


class _FakeVariant:
    def __init__(self, values):
        self._values = values

    def unpack(self):
        if isinstance(self._values, Exception):
            raise self._values
        return self._values


class _FakeProxy:
    def __init__(self, bus, object_path, interface):
        self._bus = bus
        self._object_path = object_path
        self._interface = interface

    def call_sync(self, method, _parameters, _flags, timeout_ms, _cancellable):
        return self._bus._reply(self._object_path, self._interface, method, timeout_ms)


class FakeBus:
    """The Gio surface goa uses, scripted per test."""

    def __init__(self):
        self.objects = {}
        self.tokens = {}
        self.proxy_error = None
        self.method_errors = {}
        self.unpack_errors = {}
        self.proxies = []
        self.calls = []

    def new_for_bus_sync(
        self, _bus_type, flags, _info, name, object_path, interface, _cancellable
    ):
        self.proxies.append(
            {
                "flags": flags,
                "name": name,
                "object_path": object_path,
                "interface": interface,
            }
        )
        if self.proxy_error is not None:
            raise self.proxy_error
        return _FakeProxy(self, object_path, interface)

    def _reply(self, object_path, interface, method, timeout_ms):
        self.calls.append(
            {
                "object_path": object_path,
                "interface": interface,
                "method": method,
                "timeout_ms": timeout_ms,
            }
        )
        error = self.method_errors.get((interface, method))
        if error is not None:
            raise error
        if method == "GetManagedObjects":
            values = self.unpack_errors.get(method, (self.objects,))
            return _FakeVariant(values)
        if method == "GetAccessToken":
            if (interface, method) in self.unpack_errors or object_path in self.tokens:
                values = self.unpack_errors.get(
                    (interface, method), (self.tokens[object_path],)
                )
                return _FakeVariant(values)
            raise bus_error("no such object path")
        raise AssertionError(f"unexpected bus method {method}")


@pytest.fixture
def bus(monkeypatch):
    fake = FakeBus()
    gio = SimpleNamespace(
        BusType=Gio.BusType,
        DBusCallFlags=Gio.DBusCallFlags,
        DBusProxy=fake,
    )
    monkeypatch.setattr(goa, "Gio", gio)
    return fake


def google_object(goa_id="account_1", path="/org/gnome/OnlineAccounts/Accounts/1"):
    account = {
        "Id": goa_id,
        "ProviderType": "google",
        "ProviderName": "Google",
        "PresentationIdentity": "ada@gmail.com",
    }
    mail = {
        "EmailAddress": "ada@gmail.com",
        "ImapHost": "imap.gmail.com",
        "ImapUseSsl": True,
        "ImapUserName": "ada@gmail.com",
        "SmtpHost": "smtp.gmail.com",
        "SmtpUseSsl": True,
        "Name": "Ada Lovelace",
    }
    return path, {_ACCOUNT: account, _MAIL: mail, _OAUTH2: {}}


def timeouts(bus, method):
    return [call["timeout_ms"] for call in bus.calls if call["method"] == method]


def has_flag(recorded, flag):
    return recorded & flag == flag


# --- no service on the bus ----------------------------------------------------


def test_a_missing_service_lists_no_accounts(bus):
    bus.proxy_error = bus_error()

    assert goa.mail_accounts() == []


def test_a_missing_service_signs_no_goa_account_in(bus):
    bus.proxy_error = bus_error()

    assert goa.credential("account_1") is None


def test_the_proxy_never_starts_the_service_itself(bus):
    bus.proxy_error = bus_error()
    goa.mail_accounts()

    assert bus.proxies
    for proxy in bus.proxies:
        assert has_flag(proxy["flags"], Gio.DBusProxyFlags.DO_NOT_AUTO_START)


def test_listing_fails_fast_while_a_token_may_take_its_time(bus):
    path, interfaces = google_object()
    bus.objects = {path: interfaces}
    bus.tokens = {path: "ya29.token"}
    goa.mail_accounts()
    goa.credential("account_1")

    # credential() lists too, so there are two listings to one token fetch.
    listings = timeouts(bus, "GetManagedObjects")
    (token,) = timeouts(bus, "GetAccessToken")
    assert len(listings) == 2
    assert all(listing < token for listing in listings)
    assert all(listing <= 5_000 for listing in listings)
    assert token == NET_TIMEOUT_SECONDS * 1000


# --- garbage on the bus fails closed ------------------------------------------


def test_a_malformed_listing_lists_nothing(bus):
    bus.objects = ["not", "a", "dict"]

    assert goa.mail_accounts() == []


def test_a_malformed_listing_signs_nothing_in(bus):
    bus.objects = ["not", "a", "dict"]

    assert goa.credential("account_1") is None


def test_one_unreadable_object_does_not_hide_the_rest(bus):
    path, interfaces = google_object()
    bus.objects = {
        "/org/gnome/OnlineAccounts/Accounts/0": ["not", "interfaces"],
        path: interfaces,
    }

    accounts = goa.mail_accounts()

    assert [online.goa_id for online in accounts] == ["account_1"]


def test_an_unpackable_token_signs_nothing_in(bus):
    path, interfaces = google_object()
    bus.objects = {path: interfaces}
    bus.unpack_errors[(_OAUTH2, "GetAccessToken")] = ()

    assert goa.credential("account_1") is None


def test_an_empty_token_signs_nothing_in(bus):
    path, interfaces = google_object()
    bus.objects = {path: interfaces}
    bus.tokens = {path: ""}

    assert goa.credential("account_1") is None


def test_a_non_string_token_signs_nothing_in(bus):
    path, interfaces = google_object()
    bus.objects = {path: interfaces}
    bus.tokens = {path: 12345}

    assert goa.credential("account_1") is None


def test_an_unexpected_bus_failure_still_fails_closed(bus):
    # Workers call credential_for outside their try block, so anything the bus
    # layer raises -- not just GLib.Error -- must read as failure, not escape.
    bus.proxy_error = RuntimeError("the binding exploded")

    assert goa.mail_accounts() == []
    assert goa.credential("account_1") is None


# --- ownership ----------------------------------------------------------------


def test_an_empty_goa_id_never_reaches_the_bus(bus):
    assert goa.credential("") is None

    assert bus.proxies == []
    assert bus.calls == []


def test_goa_reads_but_never_writes(bus):
    # No token copying at this layer: the only bus traffic is listing accounts
    # and reading the live token, both reads.
    path, interfaces = google_object()
    bus.objects = {path: interfaces}
    bus.tokens = {path: "ya29.token"}
    goa.mail_accounts()
    goa.credential("account_1")

    assert bus.calls
    assert {call["method"] for call in bus.calls} <= {
        "GetManagedObjects",
        "GetAccessToken",
    }


# --- pinned behaviour through the fake bus ------------------------------------


def test_a_working_bus_lists_accounts(bus):
    path, interfaces = google_object()
    bus.objects = {
        path: interfaces,
        "/org/gnome/OnlineAccounts/Accounts/2": {
            _ACCOUNT: {"Id": "temp", "IsTemporary": True},
            _MAIL: {},
            _OAUTH2: {},
        },
        "/org/gnome/OnlineAccounts/Accounts/3": {
            _ACCOUNT: {"Id": "off", "MailDisabled": True},
            _MAIL: {},
            _OAUTH2: {},
        },
    }

    (online,) = goa.mail_accounts()

    assert online.goa_id == "account_1"
    assert online.email == "ada@gmail.com"
    assert (online.imap_host, online.imap_port) == ("imap.gmail.com", 993)
    assert online.is_mail_supported


def test_a_working_bus_signs_a_goa_account_in(bus):
    path, interfaces = google_object()
    bus.objects = {path: interfaces}
    bus.tokens = {path: "ya29.token"}

    credential = goa.credential("account_1")

    assert credential is not None
    assert credential.user == "ada@gmail.com"
    assert credential.mechanism == MECHANISM_XOAUTH2


def test_a_gone_account_signs_nothing_in(bus):
    path, interfaces = google_object(goa_id="account_9")
    bus.objects = {path: interfaces}

    assert goa.credential("account_1") is None


def test_a_non_oauth_account_signs_nothing_in(bus):
    path, interfaces = google_object()
    del interfaces[_OAUTH2]
    bus.objects = {path: interfaces}

    assert goa.credential("account_1") is None


def test_a_token_timeout_signs_nothing_in(bus):
    path, interfaces = google_object()
    bus.objects = {path: interfaces}
    bus.tokens = {path: "ya29.token"}
    bus.method_errors[(_OAUTH2, "GetAccessToken")] = bus_error("timeout was reached")

    assert goa.credential("account_1") is None
