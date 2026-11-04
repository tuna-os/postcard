import json
import logging
import math
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesHeaderParser
from enum import StrEnum

from gi.repository import GLib

try:
    from sqlcipher3 import dbapi2 as _sqlcipher
except ImportError:
    _sqlcipher = None

from .. import threader
from ..compose import Envelope
from ..models.account import (
    PROTOCOL_IMAP,
    SECURITY_STARTTLS,
    SECURITY_TLS,
    Account,
)
from ..models.conversation import Conversation
from ..models.email import Email
from ..models.folder import Folder
from ..models.message_header import MessageHeader

logger = logging.getLogger(__name__)


class CacheError(Exception):
    """The mail cache cannot be opened safely.

    Codes come from a fixed vocabulary: keyring and database failures must
    never surface paths, keys, or SQL in user-facing text.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The mail cache could not be opened.")


# The first bytes of every plaintext SQLite file. Anything else on disk is
# either already encrypted or not a database at all.
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Every column _email_from_row reads
_EMAIL_COLUMNS = """
    id, folder_id, server_id, sender, sender_address, recipient,
    recipient_address, subject, preview, date, unread, starred, message_id,
    in_reply_to, reference_ids, conversation_id
"""

# What ordering and filtering a conversation page needs, without hydrating
# the mails themselves: the thread key, the arrival and date order inputs,
# and whether the thread reads as unread.
_LIGHT_COLUMNS = "id, conversation_id, server_id, date, unread"

# SQLite's bound-parameter ceiling (999 before 3.32): IN lists longer than
# this are sent in pieces.
_IN_CHUNK = 500


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: each word matched as a prefix."""
    # A literal quote inside an FTS5 string is escaped by doubling it.
    words = [word.replace('"', '""') for word in text.split()]
    return " ".join(f'"{word}"*' for word in words)


def _arrival_key(mail: Email) -> float:
    """A proxy for when a message arrived, for ordering threads/messages.

    The IMAP UID (server_id) is guaranteed by the protocol to increase with
    arrival order within a folder, unlike our local autoincrement id: once
    load-on-scroll backfills older mail in a later fetch, that older mail gets
    a *newer* local id, so sorting by id would put it first instead of last.
    A message with no UID yet (a Sent copy saved right after sending, before
    the next sync confirms it) sorts as the newest.
    """
    return _arrival_key_for(mail.server_id, mail.date)


def _arrival_key_for(server_id: str | None, date: str) -> float:
    # Split out so a paged query orders its lightweight rows with the same key
    # the full list orders its mails by, rather than a second implementation.
    if not server_id:
        return math.inf
    if server_id.isdigit():
        return int(server_id)
    # A Graph message id is opaque and says nothing about order, so fall back
    # to the date. A folder is all one kind, so the two scales never meet.
    return _sent_key_for(date)


def _sent_key(mail: Email) -> float:
    # A UID only counts up within one mailbox, so ordering a mixed list by
    # _arrival_key interleaves accounts by unrelated counters. Unlike a UID, a
    # Date header is written by the sender: an unreadable one sorts *last*, so
    # junk with "Date: whenever" can't pin itself to the top of every inbox.
    # astimezone matches format_date, which reads a zone-less stamp as local.
    return _sent_key_for(mail.date)


def _sent_key_for(date: str) -> float:
    try:
        return datetime.fromisoformat(date).astimezone().timestamp()
    except (TypeError, ValueError):
        return 0.0


def _chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    """Yield the items in pieces small enough for one IN list."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


# Schema changes since the first release, applied in order. How many have run
# is stored in PRAGMA user_version. Only ever append -- editing or reordering
# these would give databases in the wild a different schema to new ones. Each
# entry runs inside one transaction together with its version bump, so an
# entry holds plain schema/data statements only, never BEGIN or COMMIT.
MIGRATIONS = [
    "ALTER TABLE folders ADD COLUMN parent_id INTEGER REFERENCES folders(id)",
    "ALTER TABLE folders ADD COLUMN delimiter TEXT NOT NULL DEFAULT '/'",
    # Backfill by display name is best effort -- two people can share one.
    """
    ALTER TABLE emails ADD COLUMN sender_address TEXT NOT NULL DEFAULT '';
    UPDATE emails SET sender_address = COALESCE(
        (SELECT address FROM contacts WHERE contacts.name = emails.sender), '');
    """,
    # No backfill: the To header of mail synced before this is not stored
    # anywhere, so old sent mail keeps showing the sender until it re-syncs.
    """
    ALTER TABLE emails ADD COLUMN recipient TEXT NOT NULL DEFAULT '';
    ALTER TABLE emails ADD COLUMN recipient_address TEXT NOT NULL DEFAULT '';
    """,
    "ALTER TABLE accounts ADD COLUMN goa_id TEXT NOT NULL DEFAULT ''",
    # Re-index rows that predate emails_fts. A migration rather than part of
    # _create_tables, which runs on every launch: no indexed column is ever
    # updated, so once is enough and a rebuild costs the whole table.
    "INSERT INTO emails_fts(emails_fts) VALUES ('rebuild')",
    # No backfill: empty already means "sign in as the email address", which is
    # what every account predating this column was doing.
    "ALTER TABLE accounts ADD COLUMN username TEXT NOT NULL DEFAULT ''",
    # No backfill: every account before this one was IMAP.
    "ALTER TABLE accounts ADD COLUMN protocol TEXT NOT NULL DEFAULT 'imap'",
    # Empty means "infer from the name", which is what every IMAP folder still
    # does; Graph folders are named by opaque id and state both outright.
    """
    ALTER TABLE folders ADD COLUMN role TEXT NOT NULL DEFAULT '';
    ALTER TABLE folders ADD COLUMN label TEXT NOT NULL DEFAULT '';
    """,
    # NULL until the next sync reads it; see set_folder_uidvalidity.
    "ALTER TABLE folders ADD COLUMN uidvalidity INTEGER",
    # The Outbox queue: every mail waiting to go out gets a row holding its
    # send state, attempt count, last error and full envelope -- including Bcc
    # recipients, which the stored message itself must never name. It goes with
    # its email row; a terminal 'failed' row stays until the user deletes it.
    """
    CREATE TABLE IF NOT EXISTS outbox_queue (
        email_id INTEGER PRIMARY KEY REFERENCES emails(id) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'ready',
        attempts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        envelope TEXT NOT NULL DEFAULT '[]'
    );
    """,
    # The envelope a draft was addressed with -- To/Cc/Bcc as JSON -- so a
    # reopen restores Bcc, which the stored message must never name. Empty
    # for received mail and for drafts saved before this column existed.
    "ALTER TABLE emails ADD COLUMN envelope TEXT NOT NULL DEFAULT ''",
]


def _split_statements(script: str) -> list[str]:
    """Split one migration script into the single statements it holds.

    A migration runs inside an explicit transaction, which rules out
    executescript (it commits first). Splitting on ";" alone would break a
    string literal holding one, so a semicolon only counts where
    sqlite3.complete_statement agrees the statement is finished.
    """
    statements: list[str] = []
    pending = ""
    for char in script:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    tail = pending.strip()
    if any(
        line.strip() and not line.strip().startswith("--") for line in tail.splitlines()
    ):
        statements.append(tail)
    return statements


def _is_plaintext(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except FileNotFoundError:
        return False


def _remove_quietly(path: str) -> None:
    with suppress(FileNotFoundError):
        os.remove(path)


def _migrate_plaintext_to_encrypted(path: str, key: bytes) -> None:
    """Encrypt a pre-encryption cache via staging file plus atomic replace.

    The original is only read; any failure leaves it untouched and raises.
    Stale sidecars hold plaintext too, so they are removed either way.
    """
    if _sqlcipher is None:
        raise CacheError("crypto_unavailable")
    staging = path + ".migrating"
    sidecars = ("-wal", "-shm", "-journal")
    for leftover in [staging, *(staging + suffix for suffix in sidecars)]:
        _remove_quietly(leftover)
    try:
        with sqlite3.connect(path) as plain:
            version = plain.execute("PRAGMA user_version").fetchone()[0]
        source = _sqlcipher.connect(path)
        try:
            quoted = "'" + staging.replace("'", "''") + "'"
            source.execute(
                f"ATTACH DATABASE {quoted} AS encrypted KEY \"x'{key.hex()}'\""
            )
            source.execute("SELECT sqlcipher_export('encrypted')")
            source.execute(f"PRAGMA encrypted.user_version = {version:d}")
            source.execute("DETACH DATABASE encrypted")
        finally:
            source.close()
        _verify_encrypted(staging, key, version)
        os.replace(staging, path)
    except Exception as error:
        for leftover in [staging, *(staging + suffix for suffix in sidecars)]:
            _remove_quietly(leftover)
        if isinstance(error, CacheError):
            raise
        raise CacheError("migration_failed") from error
    finally:
        for suffix in sidecars:
            _remove_quietly(path + suffix)


def _verify_encrypted(path: str, key: bytes, version: int) -> None:
    conn = _open_encrypted(path, key)
    try:
        found = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    if found != version:
        raise CacheError("migration_failed")


def _open_encrypted(path: str, key: bytes):
    """Open an encrypted database, failing closed on a wrong key or damage."""
    if _sqlcipher is None:
        raise CacheError("crypto_unavailable")
    conn = _sqlcipher.connect(path)
    conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception as error:
        conn.close()
        raise CacheError("unlock_failed") from error
    conn.row_factory = _sqlcipher.Row
    return conn


def restrict_cache_permissions(path: str) -> None:
    """Make the cache directory and database files owner-only.

    Covers caches created before the process umask was restricted, and the
    sidecars SQLite manages itself (-wal, -shm, -journal), which do not
    inherit the database file's mode. Missing files are normal; anything else
    is logged rather than fatal, since permissions never matter more than the
    mail itself.
    """
    try:
        os.chmod(os.path.dirname(os.path.abspath(path)), 0o700)
    except OSError:
        logger.warning("could not restrict the cache directory", exc_info=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.chmod(path + suffix, 0o600)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not restrict %s", path + suffix, exc_info=True)


class OutboxState(StrEnum):
    """Where a queued mail stands. 'sent' and 'draft' never reach the table:
    a sent mail's row is deleted, and a draft has none yet."""

    READY = "ready"
    SENDING = "sending"
    RETRY = "retry"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class QueuedSend:
    """One mail waiting to go out, as plain values for a worker snapshot.

    envelope is None only for mail queued before the queue table existed; its
    recipients are derived from the raw message instead, which loses Bcc.
    """

    email_id: int
    subject: str
    raw: bytes
    message_id: str
    state: OutboxState
    attempts: int
    envelope: list[str] | None
    last_error: str = ""
    updated_at: str = ""


class Database:
    def __init__(self, path: str | None = None, *, key: bytes | None = None) -> None:
        if path is None:
            data_dir = os.path.join(GLib.get_user_data_dir(), "postcard")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, "postcard.db")

        if path == ":memory:":
            self._conn = sqlite3.connect(path)
            self._conn.row_factory = sqlite3.Row
        else:
            if not key:
                raise CacheError("key_missing")
            if _is_plaintext(path):
                logger.info("encrypting the plaintext mail cache")
                _migrate_plaintext_to_encrypted(path, key)
            self._conn = _open_encrypted(path, key)
        # A sync commits once per message it stores, and the default journal
        # fsyncs on every one of those. Under WAL a commit is an append, and
        # NORMAL leaves the fsync to the checkpoint -- the durability given up
        # is the last few seconds of a mailbox the server still has.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")

        # What reassign_conversations last threaded, per folder: the row count,
        # the highest id, and how many rows still want a conversation id or a
        # Message-ID. Any insert or delete moves the count, and the one write
        # that fills a threading header in place (save_raw_message) moves the
        # Message-ID count; in-place flag and recipient updates never touch
        # threading, so they read as unchanged. Equal to the stored state
        # means the stored conversation ids are still right and the rethread
        # is skipped.
        self._thread_fingerprints: dict[int, tuple[int, int, int, int]] = {}

        self._create_tables()
        self._migrate_schema()
        if path != ":memory:":
            restrict_cache_permissions(path)

    def close(self) -> None:
        self._conn.close()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                display_name TEXT NOT NULL,
                imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL,
                smtp_host TEXT NOT NULL,
                smtp_port INTEGER NOT NULL,
                imap_security TEXT NOT NULL DEFAULT 'tls',
                smtp_security TEXT NOT NULL DEFAULT 'starttls'
            );

            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                name TEXT NOT NULL,
                icon_name TEXT NOT NULL DEFAULT 'folder-symbolic'
            );

            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY,
                folder_id INTEGER NOT NULL REFERENCES folders(id),
                server_id TEXT,
                sender TEXT NOT NULL,
                subject TEXT NOT NULL,
                preview TEXT NOT NULL,
                date TEXT NOT NULL,
                unread INTEGER NOT NULL DEFAULT 1,
                starred INTEGER NOT NULL DEFAULT 0,
                raw_message BLOB,
                message_id TEXT,
                in_reply_to TEXT,
                reference_ids TEXT,
                conversation_id INTEGER
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_uid
                ON emails (folder_id, server_id);

            -- Every sidebar badge is a COUNT over one folder's unread mail;
            -- with only idx_emails_uid that reads every row of the folder.
            CREATE INDEX IF NOT EXISTS idx_emails_unread
                ON emails (folder_id, unread);

            -- Every address we've seen, for composer autocomplete.
            CREATE TABLE IF NOT EXISTS contacts (
                address TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT ''
            );

            -- Full-text search index over the searchable columns. It mirrors
            -- the emails table (content='emails'), so triggers keep it in sync.
            CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
                sender, subject, preview,
                content='emails', content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS emails_fts_insert AFTER INSERT ON emails BEGIN
                INSERT INTO emails_fts(rowid, sender, subject, preview)
                VALUES (new.id, new.sender, new.subject, new.preview);
            END;

            CREATE TRIGGER IF NOT EXISTS emails_fts_delete AFTER DELETE ON emails BEGIN
                INSERT INTO emails_fts(emails_fts, rowid, sender, subject, preview)
                VALUES ('delete', old.id, old.sender, old.subject, old.preview);
            END;
            """
        )

        self._conn.commit()

    def _migrate_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            logger.warning(
                "mail cache schema version %d is newer than this Postcard",
                version,
            )
            raise CacheError("schema_too_new")
        for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self._apply_migration(number, sql)

    def _apply_migration(self, number: int, sql: str) -> None:
        """Apply one migration and record its version as a single transaction.

        The statements and the user_version bump share one transaction, so a
        failure anywhere -- a bad statement, a lost version write, a crash --
        leaves the previous version's schema and mail intact, and reopening
        resumes from the last completed migration.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for statement in _split_statements(sql):
                self._conn.execute(statement)
            self._conn.execute(f"PRAGMA user_version = {number:d}")
            self._conn.execute("COMMIT")
        except Exception as error:
            with suppress(Exception):
                self._conn.execute("ROLLBACK")
            logger.warning("could not apply schema migration %d", number, exc_info=True)
            raise CacheError("schema_upgrade_failed") from error

    def _account_from_row(self, row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            imap_host=row["imap_host"],
            imap_port=row["imap_port"],
            smtp_host=row["smtp_host"],
            smtp_port=row["smtp_port"],
            imap_security=row["imap_security"],
            smtp_security=row["smtp_security"],
            username=row["username"],
            goa_id=row["goa_id"],
            protocol=row["protocol"],
        )

    def accounts(self) -> list[Account]:
        rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [self._account_from_row(row) for row in rows]

    def save_account(
        self,
        email: str,
        display_name: str,
        imap_host: str,
        imap_port: int,
        smtp_host: str,
        smtp_port: int,
        imap_security: str = SECURITY_TLS,
        smtp_security: str = SECURITY_STARTTLS,
        username: str = "",
        goa_id: str = "",
        protocol: str = PROTOCOL_IMAP,
    ) -> Account:
        cursor = self._conn.execute(
            """
            INSERT INTO accounts
                (email, display_name, imap_host, imap_port, smtp_host, smtp_port,
                 imap_security, smtp_security, username, goa_id, protocol)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email,
                display_name,
                imap_host,
                imap_port,
                smtp_host,
                smtp_port,
                imap_security,
                smtp_security,
                username,
                goa_id,
                protocol,
            ),
        )
        self._conn.commit()

        row = self._conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._account_from_row(row)

    def rename_account(self, account_id: int, display_name: str) -> None:
        self._conn.execute(
            "UPDATE accounts SET display_name = ? WHERE id = ?",
            (display_name, account_id),
        )
        self._conn.commit()

    def delete_account(self, account_id: int) -> None:
        # Flatten the tree first: the parent_id FK rejects deleting a parent
        # while a child still points at it.
        self._conn.execute(
            "UPDATE folders SET parent_id = NULL WHERE account_id = ?", (account_id,)
        )
        self._conn.execute(
            """
            DELETE FROM emails WHERE folder_id IN (
                SELECT id FROM folders WHERE account_id = ?
            )
            """,
            (account_id,),
        )
        rows = self._conn.execute(
            "SELECT id FROM folders WHERE account_id = ?", (account_id,)
        ).fetchall()
        self._conn.execute("DELETE FROM folders WHERE account_id = ?", (account_id,))
        self._conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self._conn.commit()
        for row in rows:
            self._forget_thread_state(row["id"])

    def _folder_from_row(self, row: sqlite3.Row) -> Folder:
        return Folder(
            id=row["id"],
            account_id=row["account_id"],
            name=row["name"],
            icon_name=row["icon_name"],
            parent_id=row["parent_id"],
            delimiter=row["delimiter"],
            role=row["role"],
            label=row["label"],
            uidvalidity=row["uidvalidity"],
        )

    def folders_for_account(self, account_id: int) -> list[Folder]:
        rows = self._conn.execute(
            "SELECT * FROM folders WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
        return [self._folder_from_row(row) for row in rows]

    def get_folder_by_name(self, account_id: int, name: str) -> Folder | None:
        row = self._conn.execute(
            "SELECT * FROM folders WHERE account_id = ? AND name = ?",
            (account_id, name),
        ).fetchone()
        return self._folder_from_row(row) if row else None

    def get_or_create_folder(
        self, account_id: int, name: str, icon_name: str = "folder-symbolic"
    ) -> Folder:
        existing = self.get_folder_by_name(account_id, name)
        if existing is not None:
            return existing

        cursor = self._conn.execute(
            "INSERT INTO folders (account_id, name, icon_name) VALUES (?, ?, ?)",
            (account_id, name, icon_name),
        )
        self._conn.commit()
        assert cursor.lastrowid is not None
        return Folder(
            id=cursor.lastrowid,
            account_id=account_id,
            name=name,
            icon_name=icon_name,
        )

    # Only the sync knows a folder's real place in the server's hierarchy, so
    # it's set explicitly here rather than defaulted by get_or_create_folder.
    def set_folder_parent(
        self, folder_id: int, parent_id: int | None, delimiter: str
    ) -> None:
        self._conn.execute(
            "UPDATE folders SET parent_id = ?, delimiter = ? WHERE id = ?",
            (parent_id, delimiter, folder_id),
        )
        self._conn.commit()

    # Written on every sync, since a folder can be renamed on the server
    # without its Graph id changing.
    def set_folder_identity(self, folder_id: int, role: str, label: str) -> None:
        self._conn.execute(
            "UPDATE folders SET role = ?, label = ? WHERE id = ?",
            (role, label, folder_id),
        )
        self._conn.commit()

    def set_folder_uidvalidity(self, folder_id: int, uidvalidity: int | None) -> bool:
        """Record the mailbox's UIDVALIDITY; return True if it had changed.

        A change means every cached UID (and body) may now belong to a
        different message, so drop the folder's server-backed rows and let the
        sync refill them. Locally saved copies have no UID and stay. The first
        value recorded counts as a change: rows cached before it -- under a
        version that never checked -- can't be shown to belong to it. A server
        that sends no UIDVALIDITY leaves everything as it is.
        """
        row = self._conn.execute(
            "SELECT uidvalidity FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        stored = row["uidvalidity"] if row else None
        if uidvalidity is None or stored == uidvalidity:
            return False
        with self._conn:
            self._conn.execute(
                """
                DELETE FROM emails
                WHERE folder_id = ? AND server_id IS NOT NULL AND server_id != ''
                """,
                (folder_id,),
            )
            self._conn.execute(
                "UPDATE folders SET uidvalidity = ? WHERE id = ?",
                (uidvalidity, folder_id),
            )
        return True

    def _delete_folder_tree(self, folder_id: int) -> None:
        # Deepest first, for the same FK reason as delete_account.
        children = self._conn.execute(
            "SELECT id FROM folders WHERE parent_id = ?", (folder_id,)
        ).fetchall()
        for child in children:
            self._delete_folder_tree(child["id"])
        self._conn.execute("DELETE FROM emails WHERE folder_id = ?", (folder_id,))
        self._conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        self._forget_thread_state(folder_id)

    def _forget_thread_state(self, folder_id: int) -> None:
        # A deleted folder's id can be reused: a fingerprint left behind could
        # match the new folder's counts and skip a rethread it still needs.
        self._thread_fingerprints.pop(folder_id, None)

    def prune_folders(self, account_id: int, keep_names: set[str]) -> None:
        """Delete an account's folders (and their emails) whose names aren't in
        keep_names. Used to mirror the server's folder list and clear stale rows
        such as a duplicate "INBOX" left by an earlier version."""
        rows = self._conn.execute(
            "SELECT id, name FROM folders WHERE account_id = ?", (account_id,)
        ).fetchall()
        for row in rows:
            if row["name"] in keep_names:
                continue
            self._delete_folder_tree(row["id"])
        self._conn.commit()

    def emails_in_folder(self, folder_id: int) -> list[Email]:
        rows = self._conn.execute(
            f"SELECT {_EMAIL_COLUMNS} FROM emails WHERE folder_id = ? ORDER BY id DESC",
            (folder_id,),
        ).fetchall()
        return [self._email_from_row(row) for row in rows]

    def prune_stale_emails(self, folder_id: int, active_server_uids: set[str]) -> int:
        """Remove server-backed emails no longer present in the authoritative set."""
        with self._conn:
            self._conn.execute(
                "CREATE TEMP TABLE prune_active_uids (uid TEXT PRIMARY KEY)"
            )
            try:
                self._conn.executemany(
                    "INSERT INTO prune_active_uids (uid) VALUES (?)",
                    ((uid,) for uid in active_server_uids),
                )
                cursor = self._conn.execute(
                    """
                    DELETE FROM emails
                    WHERE folder_id = ?
                      AND server_id IS NOT NULL
                      AND server_id != ''
                      AND NOT EXISTS (
                          SELECT 1 FROM prune_active_uids
                          WHERE prune_active_uids.uid = emails.server_id
                      )
                    """,
                    (folder_id,),
                )
                return cursor.rowcount
            finally:
                self._conn.execute("DROP TABLE prune_active_uids")

    def reassign_conversations(self, folder_id: int) -> bool:
        """Recompute the thread grouping for a folder and store it on each row.

        Returns True when it rethreaded. A sync that changed nothing -- no new
        mail, nothing pruned, no UIDVALIDITY reset -- leaves the threading
        inputs exactly as the last run saw them, so the stored ids are still
        right and the whole-folder regroup is skipped. Rows that already hold
        the right id are left alone either way.
        """
        if self._thread_fingerprints.get(folder_id) == self._thread_fingerprint(
            folder_id
        ):
            return False
        rows = self._conn.execute(
            """
            SELECT id, message_id, in_reply_to, reference_ids, subject,
                conversation_id
            FROM emails WHERE folder_id = ?
            """,
            (folder_id,),
        ).fetchall()
        groups = threader.group_keys(
            [
                threader.ThreadingKey(
                    id=row["id"],
                    message_id=row["message_id"] or "",
                    in_reply_to=row["in_reply_to"] or "",
                    references=row["reference_ids"] or "",
                    subject=row["subject"],
                )
                for row in rows
            ]
        )
        with self._conn:
            self._conn.executemany(
                "UPDATE emails SET conversation_id = ? WHERE id = ?",
                [
                    (groups[row["id"]], row["id"])
                    for row in rows
                    if groups[row["id"]] != row["conversation_id"]
                ],
            )
        self._thread_fingerprints[folder_id] = self._thread_fingerprint(folder_id)
        return True

    def _thread_fingerprint(self, folder_id: int) -> tuple[int, int, int, int]:
        """What the folder's threading inputs looked like: the row count, the
        highest id, and how many rows still want a conversation id or a
        Message-ID.

        Every insert moves the count and lands with a NULL conversation_id; a
        delete moves the count too. Filling a row's Message-ID in place (a
        body fetch for a mail synced without one) moves the Message-ID count.
        In-place flag and recipient updates never touch threading, so they
        read as unchanged.
        """
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS total, COALESCE(MAX(id), 0) AS top,
                COALESCE(SUM(conversation_id IS NULL), 0) AS unassigned,
                COALESCE(SUM(message_id IS NULL OR message_id = ''), 0)
                    AS missing_mid
            FROM emails WHERE folder_id = ?
            """,
            (folder_id,),
        ).fetchone()
        return (row["total"], row["top"], row["unassigned"], row["missing_mid"])

    def conversations_in_folders(
        self,
        folder_ids: Sequence[int],
        *,
        limit: int | None = None,
        offset: int = 0,
        unread_only: bool = False,
        keep_id: int | None = None,
    ) -> list[Conversation]:
        """Group the folders' emails into threads, newest thread first.

        A limit builds only that many threads, starting at offset -- opening a
        50k-mail folder costs one page, not the folder. unread_only keeps the
        threads with unread mail, plus keep_id (the conversation being read),
        and still returns each kept thread whole.
        """
        if not folder_ids:
            return []

        ids = tuple(folder_ids)
        places = ",".join("?" * len(ids))
        if limit is None and offset == 0 and not unread_only:
            rows = self._conn.execute(
                f"""SELECT {_EMAIL_COLUMNS} FROM emails
                WHERE folder_id IN ({places}) ORDER BY id""",
                ids,
            ).fetchall()
            return self._conversations_from_rows(rows, is_multi_folder=len(ids) > 1)
        light = self._conn.execute(
            f"""SELECT {_LIGHT_COLUMNS} FROM emails
            WHERE folder_id IN ({places}) ORDER BY id""",
            ids,
        ).fetchall()
        return self._conversation_page(
            light,
            ids,
            limit=limit,
            offset=offset,
            unread_only=unread_only,
            keep_id=keep_id,
        )

    def search_conversations(
        self,
        folder_ids: Sequence[int],
        query: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        unread_only: bool = False,
        keep_id: int | None = None,
    ) -> list[Conversation]:
        """Full-text search the folders; return each matching conversation whole.

        The subquery narrows to the threads a message matched in, so a search
        builds only those. Selecting the folders and filtering afterwards made
        every keystroke cost the same as opening them.
        """
        if not folder_ids:
            return []

        match = _fts_query(query)
        if not match:
            return self.conversations_in_folders(
                folder_ids,
                limit=limit,
                offset=offset,
                unread_only=unread_only,
                keep_id=keep_id,
            )

        ids = tuple(folder_ids)
        places = ",".join("?" * len(ids))
        narrow = f"""COALESCE(conversation_id, id) IN (
                SELECT COALESCE(e.conversation_id, e.id)
                FROM emails_fts f
                JOIN emails e ON e.id = f.rowid
                WHERE e.folder_id IN ({places}) AND emails_fts MATCH ?
            )"""
        if limit is None and offset == 0 and not unread_only:
            rows = self._conn.execute(
                f"""SELECT {_EMAIL_COLUMNS} FROM emails
                WHERE folder_id IN ({places}) AND {narrow}
                ORDER BY id""",
                (*ids, *ids, match),
            ).fetchall()
            return self._conversations_from_rows(rows, is_multi_folder=len(ids) > 1)
        light = self._conn.execute(
            f"""SELECT {_LIGHT_COLUMNS} FROM emails
            WHERE folder_id IN ({places}) AND {narrow}
            ORDER BY id""",
            (*ids, *ids, match),
        ).fetchall()
        return self._conversation_page(
            light,
            ids,
            limit=limit,
            offset=offset,
            unread_only=unread_only,
            keep_id=keep_id,
        )

    def _conversation_page(
        self,
        light_rows: Sequence[sqlite3.Row],
        folder_ids: tuple[int, ...],
        *,
        limit: int | None,
        offset: int,
        unread_only: bool,
        keep_id: int | None,
    ) -> list[Conversation]:
        """Build one page of threads from lightweight rows.

        The rows say which threads exist and how they order; only the page's
        threads are then read in full. Grouping, filtering and ordering match
        _conversations_from_rows, so a page is the full list cut to size.
        """
        groups: dict[int, list[sqlite3.Row]] = {}
        for row in light_rows:
            key = row["conversation_id"] or row["id"]
            groups.setdefault(key, []).append(row)
        is_multi_folder = len(folder_ids) > 1
        latests = []
        for key, mails in groups.items():
            if (
                unread_only
                and key != keep_id
                and not any(mail["unread"] for mail in mails)
            ):
                continue
            latest = max(
                mails,
                key=lambda mail: _arrival_key_for(mail["server_id"], mail["date"]),
            )
            latests.append((key, latest))
        if is_multi_folder:
            latests.sort(key=lambda item: _sent_key_for(item[1]["date"]), reverse=True)
        else:
            latests.sort(
                key=lambda item: _arrival_key_for(
                    item[1]["server_id"], item[1]["date"]
                ),
                reverse=True,
            )
        end = None if limit is None else offset + limit
        wanted = [key for key, _ in latests[offset:end]]
        if not wanted:
            return []
        # In id order, in pieces: a fully scrolled folder names more threads
        # than one IN list holds, and ascending chunks read back in the same
        # row order the unpaged query returns.
        places = ",".join("?" * len(folder_ids))
        rows = []
        for chunk in _chunks(sorted(wanted), _IN_CHUNK):
            slots = ",".join("?" * len(chunk))
            rows += self._conn.execute(
                f"""SELECT {_EMAIL_COLUMNS} FROM emails
                WHERE folder_id IN ({places})
                    AND COALESCE(conversation_id, id) IN ({slots})
                ORDER BY id""",
                (*folder_ids, *chunk),
            ).fetchall()
        return self._conversations_from_rows(rows, is_multi_folder=is_multi_folder)

    def _conversations_from_rows(
        self, rows: Sequence[sqlite3.Row], *, is_multi_folder: bool
    ) -> list[Conversation]:
        groups: dict[int, list[Email]] = {}
        for row in rows:
            email = self._email_from_row(row)
            key = email.conversation_id or email.id
            groups.setdefault(key, []).append(email)

        conversations = []
        for mails in groups.values():
            mails.sort(key=_arrival_key)  # oldest first, so .latest is right
            conversations.append(Conversation(mails))
        thread_key = _sent_key if is_multi_folder else _arrival_key
        conversations.sort(key=lambda c: thread_key(c.latest), reverse=True)
        return conversations

    def unread_count_in_folder(self, folder_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM emails WHERE folder_id = ? AND unread = 1",
            (folder_id,),
        ).fetchone()
        return row["n"]

    def set_email_unread(self, email_id: int, is_unread: bool) -> None:
        self._conn.execute(
            "UPDATE emails SET unread = ? WHERE id = ?", (int(is_unread), email_id)
        )
        self._conn.commit()

    def set_email_starred(self, email_id: int, is_starred: bool) -> None:
        self._conn.execute(
            "UPDATE emails SET starred = ? WHERE id = ?", (int(is_starred), email_id)
        )
        self._conn.commit()

    def move_emails(self, email_ids: list[int], folder_id: int) -> None:
        """Atomically move emails as UID-less destination placeholders.

        UIDs are only unique within an IMAP mailbox. Clear the UID while the
        move is pending so changing mailboxes cannot collide with a cached
        destination row.
        """
        with self._conn:
            self._conn.executemany(
                "UPDATE emails SET folder_id = ?, server_id = NULL WHERE id = ?",
                [(folder_id, email_id) for email_id in email_ids],
            )

    def reconcile_moved_emails(
        self, moves: Sequence[tuple[int, int, str | None]]
    ) -> None:
        """Atomically place each moved email at a (folder, UID) the server gave.

        A missing UID means the server has accepted the move but did not tell
        us how to identify its new row. Remove that placeholder so a later
        mailbox sync can insert the authoritative row without a duplicate.
        """
        with self._conn:
            for email_id, folder_id, server_id in moves:
                if server_id is None:
                    self._conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
                    continue

                existing = self._conn.execute(
                    """
                    SELECT id FROM emails
                    WHERE folder_id = ? AND server_id = ? AND id != ?
                    """,
                    (folder_id, server_id, email_id),
                ).fetchone()
                if existing is not None:
                    # The destination sync already has the authoritative row.
                    # Never delete or replace it; remove only our placeholder.
                    self._conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
                else:
                    self._conn.execute(
                        """
                        UPDATE emails
                        SET folder_id = ?, server_id = ?
                        WHERE id = ?
                        """,
                        (folder_id, server_id, email_id),
                    )

    def get_raw_message(self, email_id: int) -> bytes | None:
        row = self._conn.execute(
            "SELECT raw_message FROM emails WHERE id = ?", (email_id,)
        ).fetchone()

        return row["raw_message"] if row else None

    def save_raw_message(self, email_id: int, raw: bytes) -> None:
        # A row saved locally (a Sent copy, a draft) has no Message-ID of its own
        # until now; save_incoming_email needs it to spot the server's copy.
        message_id = BytesHeaderParser().parsebytes(raw)["Message-ID"] or ""
        self._conn.execute(
            """
            UPDATE emails
            SET raw_message = ?, message_id = COALESCE(NULLIF(message_id, ''), ?)
            WHERE id = ?
            """,
            (raw, str(message_id).strip(), email_id),
        )
        self._conn.commit()

    def save_email(
        self,
        folder_id: int,
        sender: str,
        subject: str,
        preview: str,
        date: str,
        is_unread: bool,
        server_id: str | None = None,
        sender_address: str = "",
        recipient: str = "",
        recipient_address: str = "",
        envelope: Envelope | None = None,
    ) -> Email:
        cursor = self._conn.execute(
            """
            INSERT INTO emails
                (folder_id, server_id, sender, subject, preview, date, unread,
                 sender_address, recipient, recipient_address, envelope)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                folder_id,
                server_id,
                sender,
                subject,
                preview,
                date,
                int(is_unread),
                sender_address,
                recipient,
                recipient_address,
                json.dumps(envelope) if envelope is not None else "",
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM emails WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._email_from_row(row)

    def draft_envelope(self, email_id: int) -> Envelope | None:
        """The envelope a draft was addressed with, or None when none was kept.

        Received mail and drafts saved before the envelope column carry no
        envelope; their Bcc is unrecoverable, since the MIME never names it.
        A malformed value reads as missing rather than breaking the reopen.
        """
        row = self._conn.execute(
            "SELECT envelope FROM emails WHERE id = ?", (email_id,)
        ).fetchone()
        if row is None or not row["envelope"]:
            return None
        try:
            stored = json.loads(row["envelope"])
        except ValueError:
            return None
        if not isinstance(stored, dict):
            return None
        lists: dict[str, list[str]] = {}
        for key in ("to", "cc", "bcc"):
            values = stored.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(addr, str) for addr in values
            ):
                return None
            lists[key] = values
        return Envelope(to=lists["to"], cc=lists["cc"], bcc=lists["bcc"])

    def save_incoming_email(self, folder_id: int, header: MessageHeader) -> bool:
        """Insert one fetched email, or update the flags of one we already have.

        Every local toggle is pushed to the server, so on a message we've seen
        before its \\Seen and \\Flagged are what another client did to it since.
        The recipient is rewritten too, not because it can change but because a
        row stored before those columns existed has none and would never get
        one. Returns True when the row was new, which is how a sync tells which
        messages to notify about.
        """
        return self.save_incoming_emails(folder_id, [header])[0]

    def save_incoming_emails(
        self, folder_id: int, headers: Sequence[MessageHeader]
    ) -> list[bool]:
        """Insert a sync page's fetched emails in a single transaction.

        Each header is stored exactly as save_incoming_email would store it --
        a UID holding another message's headers is replaced, a UID-less copy
        with the same Message-ID is dropped, and a known UID only takes new
        flags -- and the returned flags say per header, in order, whether its
        row was new. One commit for the page instead of one per message.
        """
        headers = list(headers)
        if not headers:
            return []
        stored = self._stored_message_ids(folder_id, {header.uid for header in headers})
        # Walked in order so a UID named twice reads the way two sequential
        # saves would: the first insert wins the row, a later Message-ID wins
        # the replace, and every header still reports its own outcome.
        is_new_flags: list[bool] = []
        replaced: list[str] = []
        effective: dict[str, int] = {}
        for index, header in enumerate(headers):
            message_id = header.message_id or ""
            if header.uid in stored and stored[header.uid] != message_id:
                # Another message at the same UID (a mailbox that reset its
                # UIDs). The upsert never rewrites the headers, so the row is
                # replaced -- which also drops its cached body, now the wrong
                # message's.
                replaced.append(header.uid)
                stored[header.uid] = message_id
                effective[header.uid] = index
                is_new_flags.append(True)
            else:
                is_new_flags.append(header.uid not in stored)
                stored.setdefault(header.uid, message_id)
                effective.setdefault(header.uid, index)
        # The UID-less copies saved when a message was sent (or moved) are now
        # duplicates, and ones that sort as newest forever.
        uidless = {header.message_id for header in headers if header.message_id}
        positions: dict[str, list[int]] = {}
        for index, header in enumerate(headers):
            positions.setdefault(header.uid, []).append(index)
        with self._conn:
            for uid in dict.fromkeys(replaced):
                self._conn.execute(
                    "DELETE FROM emails WHERE folder_id = ? AND server_id = ?",
                    (folder_id, uid),
                )
            for chunk in _chunks(sorted(uidless), _IN_CHUNK):
                places = ",".join("?" * len(chunk))
                self._conn.execute(
                    f"""DELETE FROM emails
                    WHERE folder_id = ? AND server_id IS NULL
                        AND message_id IN ({places})""",
                    (folder_id, *chunk),
                )
            self._conn.executemany(
                """
                INSERT INTO emails
                    (folder_id, server_id, sender, subject, preview, date, unread,
                     starred, message_id, in_reply_to, reference_ids,
                     sender_address, recipient, recipient_address)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (folder_id, server_id) DO UPDATE SET
                    unread = excluded.unread, starred = excluded.starred,
                    recipient = excluded.recipient,
                    recipient_address = excluded.recipient_address
                """,
                [
                    self._incoming_params(
                        folder_id, headers[effective[uid]], headers[indexes[-1]]
                    )
                    for uid, indexes in positions.items()
                ],
            )
        return is_new_flags

    @staticmethod
    def _incoming_params(
        folder_id: int, filed: MessageHeader, latest: MessageHeader
    ) -> tuple:
        """One upsert row: the headers of the message the row keeps, with the
        flags of the last header seen for its UID.

        A page names each UID once, so the two are usually the same header. A
        UID named twice keeps the last header that replaced it (or the first,
        when none did) while still taking the newest flags -- the row sequential
        saves would have left behind.
        """
        return (
            folder_id,
            filed.uid,
            filed.sender,
            filed.subject,
            filed.preview,
            filed.date,
            int(latest.is_unread),
            int(latest.is_starred),
            filed.message_id,
            filed.in_reply_to,
            filed.references,
            filed.sender_address,
            latest.recipient,
            latest.recipient_address,
        )

    def _stored_message_ids(self, folder_id: int, uids: set[str]) -> dict[str, str]:
        """The Message-IDs the folder already holds for these UIDs."""
        stored: dict[str, str] = {}
        for chunk in _chunks(sorted(uids), _IN_CHUNK):
            places = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"""SELECT server_id, message_id FROM emails
                WHERE folder_id = ? AND server_id IN ({places})""",
                (folder_id, *chunk),
            ).fetchall()
            for row in rows:
                stored[row["server_id"]] = row["message_id"] or ""
        return stored

    def delete_email(self, email_id: int) -> None:
        self._conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
        self._conn.commit()

    def _email_from_row(self, row: sqlite3.Row) -> Email:
        return Email(
            id=row["id"],
            folder_id=row["folder_id"],
            server_id=row["server_id"],
            sender=row["sender"],
            sender_address=row["sender_address"],
            recipient=row["recipient"],
            recipient_address=row["recipient_address"],
            subject=row["subject"],
            preview=row["preview"],
            date=row["date"],
            is_unread=bool(row["unread"]),
            is_starred=bool(row["starred"]),
            message_id=row["message_id"] or "",
            in_reply_to=row["in_reply_to"] or "",
            references=row["reference_ids"] or "",
            conversation_id=row["conversation_id"],
        )

    def save_contacts(self, addresses: list[tuple[str, str]]) -> None:
        """Remember (name, address) pairs -- the order email.utils.getaddresses
        returns them in. A later sighting fills in a missing display name, but
        an anonymous one never wipes a name we already have."""
        self._conn.executemany(
            """
            INSERT INTO contacts (address, name) VALUES (?, ?)
            ON CONFLICT(address) DO UPDATE SET name = excluded.name
                WHERE excluded.name != ''
            """,
            [(addr.lower(), name) for name, addr in addresses if addr],
        )
        self._conn.commit()

    def contact_addresses(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name, address FROM contacts ORDER BY name, address"
        ).fetchall()
        return [
            f"{row['name']} <{row['address']}>" if row["name"] else row["address"]
            for row in rows
        ]

    # --- Outbox queue: one row per mail waiting to go out -------------------
    # The queued-send state machine is draft -> ready -> sending -> sent, with
    # failed attempts landing on retry (or terminal failed). 'sent' deletes the
    # row; 'draft' never has one. Main thread only, like every other method:
    # the main thread claims a row before its worker starts and completes or
    # fails it when the worker reports back.

    def queue_outbox_email(self, email_id: int, recipients: list[str]) -> None:
        """Mark a mail ready to send, keeping its full envelope.

        The envelope carries every SMTP recipient including Bcc, which the
        stored message must never name -- extract_recipients cannot recover it
        from the raw bytes, so a retry without this would silently drop Bcc.
        """
        self._conn.execute(
            """
            INSERT INTO outbox_queue (email_id, state, envelope)
            VALUES (?, ?, ?)
            ON CONFLICT(email_id) DO UPDATE SET
                state = excluded.state, envelope = excluded.envelope
                WHERE outbox_queue.state IN ('ready', 'retry')
            """,
            (email_id, OutboxState.READY, json.dumps(recipients)),
        )
        self._conn.commit()

    def claim_outbox_email(self, email_id: int) -> QueuedSend | None:
        """Take a queued mail into sending, counting the attempt. None when
        another send already holds it, or it reached terminal failed."""
        cursor = self._conn.execute(
            """
            UPDATE outbox_queue
            SET state = ?, attempts = attempts + 1, updated_at = ?
            WHERE email_id = ? AND state IN ('ready', 'retry')
            """,
            (OutboxState.SENDING, _now_iso(), email_id),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.queued_send(email_id)

    def queued_send(self, email_id: int) -> QueuedSend | None:
        """The queued send for a mail, or None when it has no queue row."""
        row = self._conn.execute(
            """
            SELECT e.subject, e.raw_message, e.message_id,
                q.state, q.attempts, q.envelope, q.last_error, q.updated_at
            FROM emails e JOIN outbox_queue q ON q.email_id = e.id
            WHERE e.id = ?
            """,
            (email_id,),
        ).fetchone()
        if row is None:
            return None
        return QueuedSend(
            email_id=email_id,
            subject=row["subject"],
            raw=row["raw_message"],
            message_id=row["message_id"] or "",
            state=OutboxState(row["state"]),
            attempts=row["attempts"],
            envelope=json.loads(row["envelope"]),
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )

    def complete_outbox_email(self, email_id: int) -> None:
        """Forget a queued send that went out. The caller files the Sent copy
        and deletes the Outbox row, which would cascade here anyway."""
        self._conn.execute("DELETE FROM outbox_queue WHERE email_id = ?", (email_id,))
        self._conn.commit()

    def fail_outbox_email(self, email_id: int, error: str, state: OutboxState) -> None:
        """Record a failed attempt as retry or terminal failed, with the reason
        the next drain shows. Only a claimed (sending) row can fail."""
        self._conn.execute(
            """
            UPDATE outbox_queue
            SET state = ?, last_error = ?, updated_at = ?
            WHERE email_id = ? AND state = ?
            """,
            (state, error, _now_iso(), email_id, OutboxState.SENDING),
        )
        self._conn.commit()

    def fail_refused_outbox_email(
        self, email_id: int, refused: list[str], error: str, state: OutboxState
    ) -> None:
        """Record a refused-recipient attempt, narrowing the envelope to them.

        The retry sends to the refused recipients only, never again to the
        ones that already got it. No new column: the narrowed envelope is the
        refused set, readable from queued_send like any other envelope.
        """
        self._conn.execute(
            """
            UPDATE outbox_queue
            SET envelope = ?, state = ?, last_error = ?, updated_at = ?
            WHERE email_id = ? AND state = ?
            """,
            (
                json.dumps(refused),
                state,
                error,
                _now_iso(),
                email_id,
                OutboxState.SENDING,
            ),
        )
        self._conn.commit()

    def recover_interrupted_sends(self, except_ids: set[int] | None = None) -> int:
        """Ready every send left sending by a crash, so the drain retries it.

        A crash between SMTP acceptance and completion leaves exactly this: a
        sending row whose mail may or may not have left. The retry proves which
        by looking for its own copy in Sent before touching SMTP. except_ids
        are claims a live send in this process still holds -- recovering one
        would let a second worker send it too.
        """
        if except_ids:
            placeholders = ", ".join("?" for _ in except_ids)
            cursor = self._conn.execute(
                "UPDATE outbox_queue SET state = ? WHERE state = ?"
                f" AND email_id NOT IN ({placeholders})",
                (OutboxState.READY, OutboxState.SENDING, *except_ids),
            )
        else:
            cursor = self._conn.execute(
                "UPDATE outbox_queue SET state = ? WHERE state = ?",
                (OutboxState.READY, OutboxState.SENDING),
            )
        self._conn.commit()
        return cursor.rowcount

    def pending_outbox_sends(self, outbox_folder_id: int) -> list[QueuedSend]:
        """Every Outbox mail with something to send, oldest first.

        Terminal failed rows are excluded -- they stay visible in the Outbox
        until the user deletes them. A mail with no queue row yet (queued
        before this table existed) reads back as ready with no envelope, and
        the drain derives its recipients from the raw message instead.
        """
        rows = self._conn.execute(
            """
            SELECT e.id, e.subject, e.raw_message, e.message_id,
                q.state, q.attempts, q.envelope, q.last_error, q.updated_at
            FROM emails e LEFT JOIN outbox_queue q ON q.email_id = e.id
            WHERE e.folder_id = ?
                AND e.raw_message IS NOT NULL
                AND (q.state IS NULL OR q.state IN ('ready', 'retry', 'sending'))
            ORDER BY e.id
            """,
            (outbox_folder_id,),
        ).fetchall()
        sends = []
        for row in rows:
            envelope = json.loads(row["envelope"]) if row["envelope"] else None
            sends.append(
                QueuedSend(
                    email_id=row["id"],
                    subject=row["subject"],
                    raw=row["raw_message"],
                    message_id=row["message_id"] or "",
                    state=(
                        OutboxState(row["state"]) if row["state"] else OutboxState.READY
                    ),
                    attempts=row["attempts"] or 0,
                    envelope=envelope,
                    last_error=row["last_error"] or "",
                    updated_at=row["updated_at"] or "",
                )
            )
        return sends

    def delete_emails_by_message_id(self, folder_id: int, message_id: str) -> None:
        """Drop a folder's rows for one Message-ID, before filing its copy.

        Filing is not always the first attempt: a retry that finds its mail
        already in Sent files the local copy without resending, and a crash
        between an earlier filing and its Outbox cleanup files it twice. The
        second insert would collide with the first under idx_emails_uid, so
        the earlier copy goes first. Never call with an empty id: rows filed
        before Message-IDs were recorded would all match.
        """
        self._conn.execute(
            "DELETE FROM emails WHERE folder_id = ? AND message_id = ?",
            (folder_id, message_id),
        )
        self._conn.commit()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()
