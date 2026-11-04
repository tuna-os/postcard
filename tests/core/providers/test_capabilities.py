"""Capabilities: what each provider can do, as the UI reads it."""

import dataclasses

import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import (
    PROTOCOL_GRAPH,
    PROTOCOL_IMAP,
    PROTOCOL_JMAP,
    PROTOCOL_PROTON,
    PROTOCOL_TUTA,
)
from postcard.core.providers import (
    BUILTIN_IDS,
    PROVIDER_API_VERSION,
    Capability,
    FetchRequest,
    FlagRequest,
    MessageRequest,
    MoveRequest,
    PerIdOutcome,
    ProviderDescriptor,
    ProviderRegistry,
    SendRequest,
    SyncCompleteness,
    SyncResult,
)

ALL = frozenset(Capability)


@pytest.mark.parametrize(
    ("protocol", "capabilities"),
    [
        (PROTOCOL_IMAP, ALL),
        (PROTOCOL_GRAPH, ALL - {Capability.IDLE_PUSH}),
        (
            PROTOCOL_TUTA,
            frozenset(
                {
                    Capability.FETCH,
                    Capability.FETCH_BODY,
                    Capability.SET_FLAGS,
                    Capability.MOVE,
                }
            ),
        ),
        (PROTOCOL_PROTON, frozenset()),
        (PROTOCOL_JMAP, ALL - {Capability.IDLE_PUSH}),
    ],
)
def test_builtin_capabilities(protocol, capabilities):
    """The matrix the UI disables actions from. Proton has none: every one of
    its operations raises unsupported_operation. JMAP has all but push: its
    event source stays a part-2 primitive until a watcher owns it."""
    registry = ProviderRegistry()
    mail_sync.register_builtin_drivers(registry)

    assert registry.driver_for(protocol).descriptor.capabilities == capabilities


def test_builtin_ids_are_the_stored_protocols():
    assert sorted(BUILTIN_IDS) == sorted(
        [PROTOCOL_IMAP, PROTOCOL_GRAPH, PROTOCOL_TUTA, PROTOCOL_PROTON, PROTOCOL_JMAP]
    )


def test_nothing_is_supported_without_a_driver():
    for capability in Capability:
        assert not mail_sync.provider_supports("carrier-pigeon", capability)


def test_a_descriptor_supports_only_what_it_lists():
    descriptor = ProviderDescriptor(
        id="example", display_name="Example", capabilities=frozenset({Capability.SEND})
    )

    assert descriptor.supports(Capability.SEND)
    assert not descriptor.supports(Capability.MOVE)


def test_version_compatibility_is_a_closed_range():
    current = ProviderDescriptor(
        id="example",
        display_name="Example",
        capabilities=frozenset(),
        min_api_version=PROVIDER_API_VERSION,
        max_api_version=PROVIDER_API_VERSION,
    )
    assert current.is_compatible_with(PROVIDER_API_VERSION)

    future = dataclasses.replace(current, min_api_version=PROVIDER_API_VERSION + 1)
    assert not future.is_compatible_with(PROVIDER_API_VERSION)

    past = dataclasses.replace(current, max_api_version=PROVIDER_API_VERSION - 1)
    assert not past.is_compatible_with(PROVIDER_API_VERSION)


def test_default_descriptors_speak_this_build_api():
    descriptor = ProviderDescriptor(
        id="example", display_name="Example", capabilities=frozenset()
    )

    assert (descriptor.min_api_version, descriptor.max_api_version) == (
        PROVIDER_API_VERSION,
        PROVIDER_API_VERSION,
    )


@pytest.mark.parametrize(
    ("candidate", "field"),
    [
        (FetchRequest(None, 50, 0, True), "folder"),
        (MessageRequest("INBOX", "4", 1), "uid"),
        (FlagRequest("INBOX", ("4",), "\\Seen", True, 1), "flag"),
        (MoveRequest("INBOX", ("4",), "Archive", 1), "destination"),
        (SendRequest("ada@example.com", ("bob@example.com",), b"raw"), "raw"),
    ],
)
def test_driver_requests_are_immutable(candidate, field):
    """A worker thread's snapshot cannot be mutated from under it.

    Each candidate rebinds one of its own fields: assigning a name the
    class never declared raises a version-dependent TypeError instead of
    the FrozenInstanceError this proves.
    """
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(candidate, field, "other")


def test_flag_and_move_requests_carry_tuples_not_lists():
    """Frozen only blocks rebinding the field, so a list there would still be
    mutable from the main thread."""
    flag = FlagRequest("INBOX", ("4",), "\\Seen", True, 1)
    move = MoveRequest("INBOX", ("4",), "Archive", 1)

    assert isinstance(flag.uids, tuple)
    assert isinstance(move.uids, tuple)


def test_a_per_id_outcome_defaults_to_unexplained():
    assert PerIdOutcome("4", True).error_code is None


def test_a_sync_result_defaults_to_complete_with_no_outcomes():
    result = SyncResult()

    assert result.completeness is SyncCompleteness.COMPLETE
    assert result.outcomes == ()
