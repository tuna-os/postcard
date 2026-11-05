import json
import os
import stat

import pytest

from postcard.core.portal_secrets import (
    PORTAL_BUS_NAME,
    PORTAL_OBJECT_PATH,
    PORTAL_SECRET_VERSION,
    REQUEST_INTERFACE,
    SECRET_INTERFACE,
    CipherError,
    MasterSecret,
    PortalSecretError,
    PortalSecretStore,
)


class FakeTransport:
    """A portal that hands out a fixed master secret and records its tokens."""

    def __init__(self, master=b"master-secret", issued="token-1"):
        self.secret = master
        self.next_token = issued
        self.seen_tokens = []

    def retrieve_secret(self, token):
        self.seen_tokens.append(token)
        return MasterSecret(self.secret, self.next_token)


class FakeCipher:
    """A reversible marker transform; proves the store seals, not how."""

    def __init__(self):
        self.sealed_with = []

    def seal(self, key, plaintext):
        self.sealed_with.append(key)
        return b"sealed:" + plaintext

    def open(self, key, sealed):
        if not sealed.startswith(b"sealed:"):
            raise CipherError("foreign payload")
        return sealed.removeprefix(b"sealed:")


class ExplodingTransport:
    def retrieve_secret(self, token):
        raise RuntimeError("the portal is down")


def _store(tmp_path, transport=None, cipher=None):
    transport = transport or FakeTransport()
    cipher = cipher or FakeCipher()
    return PortalSecretStore(tmp_path / "secrets.json", transport, cipher)


def _envelope_bytes(*, salt="00" * 16, token=None, sealed=b"sealed:{}"):
    return json.dumps({"salt": salt, "token": token, "sealed": sealed.hex()}).encode()


def _rewrite_inner_value(path, name, value):
    # The sealed dict is only reachable through the cipher; FakeCipher
    # ignores its key, so any key opens it.
    cipher = FakeCipher()
    envelope = json.loads(path.read_text(encoding="utf-8"))
    inner = json.loads(cipher.open(b"", bytes.fromhex(envelope["sealed"])))
    inner[name] = value
    envelope["sealed"] = cipher.seal(b"", json.dumps(inner).encode()).hex()
    path.write_text(json.dumps(envelope), encoding="utf-8")


def test_the_portal_names_match_the_spec():
    assert PORTAL_BUS_NAME == "org.freedesktop.portal.Desktop"
    assert PORTAL_OBJECT_PATH == "/org/freedesktop/portal/desktop"
    assert SECRET_INTERFACE == "org.freedesktop.portal.Secret"
    assert REQUEST_INTERFACE == "org.freedesktop.portal.Request"
    assert PORTAL_SECRET_VERSION == 1


def test_a_stored_password_reads_back(tmp_path):
    store = _store(tmp_path)

    store.store_password(7, "s3cret")

    assert store.lookup_password(7) == "s3cret"


def test_passwords_are_isolated_per_account(tmp_path):
    store = _store(tmp_path)
    store.store_password(7, "seven")
    store.store_password(8, "eight")

    assert (store.lookup_password(7), store.lookup_password(8)) == ("seven", "eight")


def test_a_missing_password_reads_as_none_without_touching_the_disk(tmp_path):
    store = _store(tmp_path)

    assert store.lookup_password(7) is None
    assert not (tmp_path / "secrets.json").exists()


def test_storing_twice_replaces_the_password(tmp_path):
    store = _store(tmp_path)
    store.store_password(7, "old")

    store.store_password(7, "new")

    assert store.lookup_password(7) == "new"


def test_clear_removes_the_password_and_reports_true(tmp_path):
    store = _store(tmp_path)
    store.store_password(7, "s3cret")

    assert store.clear_password(7) is True
    assert store.lookup_password(7) is None


def test_clearing_a_missing_password_reports_false_and_writes_nothing(tmp_path):
    store = _store(tmp_path)

    assert store.clear_password(7) is False
    assert not (tmp_path / "secrets.json").exists()


def test_clearing_the_last_secret_leaves_a_valid_empty_file(tmp_path):
    store = _store(tmp_path)
    store.store_password(7, "s3cret")

    assert store.clear_password(7) is True
    assert _store(tmp_path).lookup_password(7) is None


def test_secrets_survive_a_fresh_store_instance(tmp_path):
    _store(tmp_path).store_password(7, "s3cret")

    assert _store(tmp_path).lookup_password(7) == "s3cret"


def test_the_sealed_file_holds_no_plaintext_password(tmp_path):
    store = _store(tmp_path)
    store.store_password(7, "s3cret")

    contents = (tmp_path / "secrets.json").read_text(encoding="utf-8")

    assert "s3cret" not in contents


def test_the_sealed_file_is_owner_only(tmp_path):
    store = _store(tmp_path)
    store.store_password(7, "s3cret")
    store.store_password(8, "other")

    mode = stat.S_IMODE(os.stat(tmp_path / "secrets.json").st_mode)

    assert mode == 0o600


def test_no_tmp_file_is_left_behind(tmp_path):
    store = _store(tmp_path)

    store.store_password(7, "s3cret")

    assert list(tmp_path.glob("*.tmp")) == []


def test_the_envelope_carries_salt_token_and_sealed(tmp_path):
    store = _store(tmp_path, transport=FakeTransport(issued="token-9"))

    store.store_password(7, "s3cret")
    envelope = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))

    assert set(envelope) == {"salt", "token", "sealed"}
    assert envelope["token"] == "token-9"
    assert len(bytes.fromhex(envelope["salt"])) == 16


def test_the_portal_token_is_replayed_on_the_next_call(tmp_path):
    path = tmp_path / "secrets.json"
    first = PortalSecretStore(path, FakeTransport(issued="token-1"), FakeCipher())
    first.store_password(1, "s3cret")

    second = FakeTransport(issued="token-2")
    PortalSecretStore(path, second, FakeCipher()).lookup_password(1)

    assert second.seen_tokens == ["token-1"]


def test_the_file_key_is_derived_not_the_master_secret_itself(tmp_path):
    cipher = FakeCipher()
    store = _store(tmp_path, cipher=cipher)

    store.store_password(7, "s3cret")

    (key,) = cipher.sealed_with
    assert key != b"master-secret"
    assert len(key) == 32


def test_each_file_gets_its_own_salt_and_key(tmp_path):
    first_cipher = FakeCipher()
    second_cipher = FakeCipher()
    first = PortalSecretStore(tmp_path / "one.json", FakeTransport(), first_cipher)
    second = PortalSecretStore(tmp_path / "two.json", FakeTransport(), second_cipher)

    first.store_password(7, "s3cret")
    second.store_password(7, "s3cret")

    assert first_cipher.sealed_with != second_cipher.sealed_with


def test_the_database_key_is_32_stable_bytes(tmp_path):
    store = _store(tmp_path)

    key = store.database_key()

    assert len(key) == 32
    assert store.database_key() == key
    assert _store(tmp_path).database_key() == key


def test_the_database_key_coexists_with_passwords(tmp_path):
    store = _store(tmp_path)

    key = store.database_key()
    store.store_password(7, "s3cret")

    assert store.database_key() == key
    assert store.lookup_password(7) == "s3cret"


@pytest.mark.parametrize("stored", ["not-hex", "", "ab" * 31, "ab" * 33])
def test_a_malformed_cache_key_fails_closed_and_keeps_the_file(tmp_path, stored):
    path = tmp_path / "secrets.json"
    store = PortalSecretStore(path, FakeTransport(), FakeCipher())
    store.database_key()
    _rewrite_inner_value(path, "mail-cache", stored)
    before = path.read_bytes()

    with pytest.raises(PortalSecretError):
        store.database_key()

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "contents",
    [
        b"this is not json",
        b"[1, 2]",
        _envelope_bytes(salt="zz"),
        _envelope_bytes(salt="0011"),
        _envelope_bytes(token=7),
        json.dumps({"salt": "00" * 16, "token": None}).encode(),
        _envelope_bytes(sealed=b"bogus"),
        _envelope_bytes(sealed=b"sealed:[1]"),
        _envelope_bytes(sealed=b'sealed:{"a": 1}'),
    ],
)
def test_damaged_files_fail_closed(tmp_path, contents):
    path = tmp_path / "secrets.json"
    path.write_bytes(contents)
    store = PortalSecretStore(path, FakeTransport(), FakeCipher())

    with pytest.raises(PortalSecretError):
        store.lookup_password(1)


def test_a_dead_portal_fails_closed_for_every_operation(tmp_path):
    store = _store(tmp_path, transport=ExplodingTransport())

    with pytest.raises(PortalSecretError):
        store.lookup_password(1)
    with pytest.raises(PortalSecretError):
        store.store_password(1, "s3cret")
    with pytest.raises(PortalSecretError):
        store.database_key()


def test_unexpected_transport_errors_are_wrapped_as_portal_errors(tmp_path):
    store = _store(tmp_path, transport=ExplodingTransport())

    with pytest.raises(PortalSecretError) as raised:
        store.lookup_password(1)

    assert isinstance(raised.value.__cause__, RuntimeError)


def test_portal_errors_from_the_transport_pass_through_unwrapped(tmp_path):
    class FailingTransport:
        def retrieve_secret(self, token):
            raise PortalSecretError("denied")

    store = _store(tmp_path, transport=FailingTransport())

    with pytest.raises(PortalSecretError) as raised:
        store.lookup_password(1)

    assert raised.value.__cause__ is None


def test_an_unwritable_path_fails_closed(tmp_path):
    store = PortalSecretStore(
        tmp_path / "no-such-dir" / "secrets.json", FakeTransport(), FakeCipher()
    )

    with pytest.raises(PortalSecretError):
        store.store_password(1, "s3cret")
