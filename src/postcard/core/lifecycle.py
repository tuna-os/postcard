"""In-memory generations that mark background jobs stale.

A sync, send, flag change, move or body fetch starts on the main thread, runs
on a worker, and lands back via GLib.idle_add -- by which time its account may
have been removed (possibly re-added under the same reused rowid), its folder
may have been pruned and recreated, or its window may be gone. Checking the
account id, as _is_stale once did, cannot tell the re-added account from the
removed one.

Instead every job mints an immutable JobToken when it starts, capturing the
current generation of its account (and folder, when it acts on one mailbox),
and every completion drops its result unless the token is still live. Removal
and reconfiguration bump the account's generation, a UIDVALIDITY change or a
prune bumps the folder's, and owner shutdown ends them all.

Main thread only, like every other piece of completion state: workers only
carry their token through to idle_add, which is safe because it is frozen.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JobToken:
    """What a job believed about its account and folder when it started.

    folder_name is None for jobs that span the account (a sync, an Outbox
    drain); folder_generation is then meaningless and always matches.
    """

    account_id: int
    account_generation: int
    folder_name: str | None
    folder_generation: int


class Lifecycle:
    """Generations for one owner's jobs: a main window, a composer.

    One instance per window, not one per application: closing a main window
    must not strand a composer's send, and a second main window keeps its own
    jobs. mail_sync broadcasts account removal to every registered instance.
    """

    def __init__(self) -> None:
        self._account_generations: dict[int, int] = {}
        self._folder_generations: dict[tuple[int, str], int] = {}
        self._is_shutdown = False

    @property
    def is_shutdown(self) -> bool:
        return self._is_shutdown

    def mint(self, account_id: int, folder_name: str | None = None) -> JobToken:
        """Capture this account's (and folder's) current generation for a job.

        Minting after shutdown hands back a token that is already dead, so a
        job started on the way out still completes to a dropped no-op.
        """
        return JobToken(
            account_id=account_id,
            account_generation=self._account_generations.get(account_id, 0),
            folder_name=folder_name,
            folder_generation=(
                self._folder_generations.get((account_id, folder_name), 0)
                if folder_name is not None
                else 0
            ),
        )

    def is_live(self, token: JobToken) -> bool:
        """Whether nothing has invalidated this token's job since it started."""
        if self._is_shutdown:
            return False
        if self._account_generations.get(token.account_id, 0) != (
            token.account_generation
        ):
            return False
        if token.folder_name is None:
            return True
        return (
            self._folder_generations.get((token.account_id, token.folder_name), 0)
            == token.folder_generation
        )

    def invalidate_account(self, account_id: int) -> None:
        """End every job for an account that was removed or reconfigured.

        Its folder generations go too: a re-added account starts from zero,
        never from the removed one's counts.
        """
        self._account_generations[account_id] = (
            self._account_generations.get(account_id, 0) + 1
        )
        for key in [key for key in self._folder_generations if key[0] == account_id]:
            del self._folder_generations[key]

    def invalidate_folder(self, account_id: int, folder_name: str) -> None:
        """End every job on a mailbox whose UIDs were renumbered or replaced."""
        key = (account_id, folder_name)
        self._folder_generations[key] = self._folder_generations.get(key, 0) + 1

    def shutdown(self) -> None:
        """End every job for good: its owner is gone."""
        self._is_shutdown = True
