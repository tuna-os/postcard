import dataclasses

import pytest

from postcard.core.lifecycle import Lifecycle


@pytest.fixture
def lifecycle():
    return Lifecycle()


def test_a_fresh_token_is_live(lifecycle):
    assert lifecycle.is_live(lifecycle.mint(1))


def test_tokens_are_immutable():
    token = Lifecycle().mint(1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        token.account_generation = 99  # type: ignore[misc]


def test_removing_an_account_kills_its_tokens_but_not_others(lifecycle):
    doomed = lifecycle.mint(1)
    spared = lifecycle.mint(2)

    lifecycle.invalidate_account(1)

    assert not lifecycle.is_live(doomed)
    assert lifecycle.is_live(spared)


def test_a_token_minted_after_removal_is_live_again(lifecycle):
    lifecycle.invalidate_account(1)

    assert lifecycle.is_live(lifecycle.mint(1))


def test_removal_kills_folder_tokens_too(lifecycle):
    token = lifecycle.mint(1, "INBOX")

    lifecycle.invalidate_account(1)

    assert not lifecycle.is_live(token)


def test_replacement_folder_tokens_do_not_reuse_old_generations(lifecycle):
    lifecycle.mint(1, "INBOX")
    lifecycle.invalidate_account(1)

    assert lifecycle.is_live(lifecycle.mint(1, "INBOX"))


def test_pruning_a_folder_kills_only_that_folders_tokens(lifecycle):
    inbox = lifecycle.mint(1, "INBOX")
    archive = lifecycle.mint(1, "Archive")
    account_wide = lifecycle.mint(1)

    lifecycle.invalidate_folder(1, "INBOX")

    assert not lifecycle.is_live(inbox)
    assert lifecycle.is_live(archive)
    assert lifecycle.is_live(account_wide)


def test_same_named_folders_on_other_accounts_are_unaffected(lifecycle):
    other = lifecycle.mint(2, "INBOX")

    lifecycle.invalidate_folder(1, "INBOX")

    assert lifecycle.is_live(other)


def test_an_account_wide_token_survives_a_folder_prune(lifecycle):
    token = lifecycle.mint(1)

    lifecycle.invalidate_folder(1, "INBOX")

    assert lifecycle.is_live(token)


def test_shutdown_kills_every_token(lifecycle):
    account = lifecycle.mint(1)
    folder = lifecycle.mint(2, "INBOX")

    lifecycle.shutdown()

    assert not lifecycle.is_live(account)
    assert not lifecycle.is_live(folder)
    assert lifecycle.is_shutdown


def test_minting_after_shutdown_hands_back_a_dead_token(lifecycle):
    lifecycle.shutdown()

    assert not lifecycle.is_live(lifecycle.mint(1))


def test_invalidation_does_not_cross_owners(lifecycle):
    other = Lifecycle()
    token = lifecycle.mint(1)

    other.invalidate_account(1)
    other.shutdown()

    assert lifecycle.is_live(token)


def test_repeated_invalidations_keep_working(lifecycle):
    for _ in range(3):
        token = lifecycle.mint(1)
        assert lifecycle.is_live(token)
        lifecycle.invalidate_account(1)
        assert not lifecycle.is_live(token)
