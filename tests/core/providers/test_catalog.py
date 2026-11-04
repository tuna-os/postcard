"""The picker offers every provider, gated only by shipped client ids."""

from postcard.core.providers.catalog import PROVIDERS, AuthMethod, availability


def test_every_provider_has_an_icon_and_a_sign_in_path() -> None:
    assert [provider.id for provider in PROVIDERS] == [
        "google",
        "microsoft",
        "fastmail",
        "proton",
        "tuta",
        "other",
    ]
    for provider in PROVIDERS:
        assert provider.name
        assert provider.subtitle
        assert provider.icon_name.startswith("postcard-provider-")
        assert isinstance(provider.auth, AuthMethod)


def test_gated_rows_need_a_flag_and_a_reason() -> None:
    gated = {"microsoft", "fastmail", "proton"}
    for provider in PROVIDERS:
        if provider.id in gated:
            assert provider.client_flag
            assert provider.unavailable_subtitle
        else:
            assert not provider.client_flag


def test_gates_default_to_unavailable() -> None:
    assert availability({}) == {
        "google": True,
        "microsoft": False,
        "fastmail": False,
        "proton": False,
        "tuta": True,
        "other": True,
    }
    assert availability(
        {"microsoft": True, "fastmail": True, "proton-helper": True}
    ) == {
        "google": True,
        "microsoft": True,
        "fastmail": True,
        "proton": True,
        "tuta": True,
        "other": True,
    }
    # An unknown flag never enables anything.
    assert availability({"google": True})["microsoft"] is False
