"""Add-on discovery: trust before import, isolation after it."""

import importlib.metadata
from types import SimpleNamespace

import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import PROTOCOL_IMAP
from postcard.core.providers import (
    PROVIDER_API_VERSION,
    Capability,
    DisabledProviderError,
    IncompatibleProviderError,
    ProviderDescriptor,
    ProviderDriver,
    ProviderRegistry,
    UnknownProviderError,
    read_trust_list,
)


class StubDriver(ProviderDriver):
    """The smallest driver an add-on test can load. Its operations are never
    called -- discovery is what is under test."""

    def __init__(
        self,
        id="example",
        capabilities=frozenset(Capability),
        min_api_version=PROVIDER_API_VERSION,
        max_api_version=PROVIDER_API_VERSION,
    ):
        self._descriptor = ProviderDescriptor(
            id=id,
            display_name=id.title(),
            capabilities=capabilities,
            min_api_version=min_api_version,
            max_api_version=max_api_version,
        )

    @property
    def descriptor(self):
        return self._descriptor

    def fetch_mailbox(self, account, credential, request):
        raise AssertionError("not under test")

    def fetch_full_message(self, account, credential, request):
        raise AssertionError("not under test")

    def set_flags(self, account, credential, request):
        raise AssertionError("not under test")

    def move(self, account, credential, request):
        raise AssertionError("not under test")

    def send(self, account, credential, request):
        raise AssertionError("not under test")

    def sent_copy(self, account, credential, message_id):
        raise AssertionError("not under test")


class FailingDriver(StubDriver):
    @property
    def descriptor(self):
        raise RuntimeError("broken descriptor")


def entry_point(name, loaded=None, error=None):
    """An installed add-on's advertisement. load() records whether discovery
    executed it -- the tests' way of proving untrusted code never runs."""
    calls = []

    def load():
        calls.append(True)
        if error is not None:
            raise error
        return loaded

    point = SimpleNamespace(name=name, value=f"testdouble:{name}", load=load)
    point.load_calls = calls
    return point


def test_a_trusted_addon_serves_its_id():
    driver = StubDriver("example")
    point = entry_point("example", loaded=driver)
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset({"example"}), entry_points=[point])

    assert point.load_calls == [True]
    assert registry.driver_for("example") is driver
    assert registry.load_errors == {}


def test_a_driver_class_is_instantiated_on_load():
    point = entry_point("example", loaded=StubDriver)
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset({"example"}), entry_points=[point])

    assert isinstance(registry.driver_for("example"), StubDriver)


def test_an_untrusted_addon_is_listed_but_never_imported():
    point = entry_point("example", loaded=StubDriver("example"))
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset(), entry_points=[point])

    assert point.load_calls == []
    with pytest.raises(DisabledProviderError):
        registry.driver_for("example")
    assert registry.descriptor_for("example") is None


def test_a_duplicate_of_a_builtin_is_rejected_unimported():
    point = entry_point(PROTOCOL_IMAP, loaded=StubDriver(PROTOCOL_IMAP))
    registry = ProviderRegistry()
    mail_sync.register_builtin_drivers(registry)

    registry.discover(trusted_ids=frozenset({PROTOCOL_IMAP}), entry_points=[point])

    assert point.load_calls == []
    assert PROTOCOL_IMAP in registry.load_errors
    assert registry.driver_for(PROTOCOL_IMAP).descriptor.display_name == "IMAP/SMTP"


def test_a_second_addon_with_one_id_loses():
    winner = StubDriver("example")
    first = entry_point("example", loaded=winner)
    second = entry_point("example", loaded=StubDriver("example"))
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset({"example"}), entry_points=[first, second])

    assert "example" in registry.load_errors
    assert registry.driver_for("example") is winner


def test_a_broken_import_does_not_stop_the_other_addons():
    broken = entry_point("broken", error=ImportError("no such module"))
    healthy = entry_point("healthy", loaded=StubDriver("healthy"))
    registry = ProviderRegistry()

    registry.discover(
        trusted_ids=frozenset({"broken", "healthy"}),
        entry_points=[broken, healthy],
    )

    assert "broken" in registry.load_errors
    assert registry.driver_for("healthy").descriptor.id == "healthy"
    with pytest.raises(UnknownProviderError):
        registry.driver_for("broken")


def test_a_broken_descriptor_does_not_stop_the_other_addons():
    broken = entry_point("broken", loaded=FailingDriver("broken"))
    healthy = entry_point("healthy", loaded=StubDriver("healthy"))
    registry = ProviderRegistry()

    registry.discover(
        trusted_ids=frozenset({"broken", "healthy"}),
        entry_points=[broken, healthy],
    )

    assert "broken" in registry.load_errors
    assert registry.driver_for("healthy").descriptor.id == "healthy"


def test_a_non_driver_object_is_rejected():
    point = entry_point("example", loaded=object())
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset({"example"}), entry_points=[point])

    assert "example" in registry.load_errors
    with pytest.raises(UnknownProviderError):
        registry.driver_for("example")


def test_a_driver_serving_another_id_is_rejected():
    """Trust is granted to the entry point's name; a driver claiming a
    different id cannot borrow it."""
    point = entry_point("claimed", loaded=StubDriver("other"))
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset({"claimed"}), entry_points=[point])

    assert "claimed" in registry.load_errors
    with pytest.raises(UnknownProviderError):
        registry.driver_for("claimed")
    with pytest.raises(UnknownProviderError):
        registry.driver_for("other")


def test_an_incompatible_addon_stays_unloaded():
    driver = StubDriver("example", min_api_version=PROVIDER_API_VERSION + 1)
    point = entry_point("example", loaded=driver)
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset({"example"}), entry_points=[point])

    assert "example" in registry.load_errors
    with pytest.raises(IncompatibleProviderError):
        registry.driver_for("example")
    assert registry.descriptor_for("example") is None


def test_a_real_entry_point_to_a_missing_module_is_isolated():
    point = importlib.metadata.EntryPoint(
        name="ghost", value="no_such_module_9z9z:Driver", group="postcard.providers"
    )
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset({"ghost"}), entry_points=[point])

    assert "ghost" in registry.load_errors
    with pytest.raises(UnknownProviderError):
        registry.driver_for("ghost")


def test_a_real_entry_point_to_a_non_driver_is_isolated():
    point = importlib.metadata.EntryPoint(
        name="plain", value="builtins:dict", group="postcard.providers"
    )
    registry = ProviderRegistry()

    registry.discover(trusted_ids=frozenset({"plain"}), entry_points=[point])

    assert "plain" in registry.load_errors


def test_a_broken_listing_keeps_the_builtins(monkeypatch):
    def listing(*args, **kwargs):
        raise RuntimeError("metadata is damaged")

    monkeypatch.setattr(importlib.metadata, "entry_points", listing)
    registry = ProviderRegistry()
    mail_sync.register_builtin_drivers(registry)

    registry.discover()

    assert registry.driver_for(PROTOCOL_IMAP).descriptor.id == PROTOCOL_IMAP


def test_the_trust_file_names_ids_around_comments_and_blanks(tmp_path):
    path = tmp_path / "trusted_providers"
    path.write_text("# add-ons the user trusted\n\nexample\n\n  other  \n")

    assert read_trust_list(str(path)) == frozenset({"example", "other"})


def test_a_missing_trust_file_trusts_nothing(tmp_path):
    assert read_trust_list(str(tmp_path / "trusted_providers")) == frozenset()


def test_an_unreadable_trust_file_trusts_nothing(tmp_path):
    binary = tmp_path / "trusted_providers"
    binary.write_bytes(b"\xff\xfe\x00bad")

    assert read_trust_list(str(binary)) == frozenset()
    assert read_trust_list(str(tmp_path)) == frozenset()
