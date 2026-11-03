-- Frozen version-0 schema with representative mail. A historical artifact: it
-- captures the database as the first release left it, so it must NOT be
-- updated to match _create_tables. The upgrade tests load it, migrate it to
-- the current schema, and prove everything it holds survives.
--
-- Representative on purpose: two accounts, a Sent folder, an Outbox queue of
-- unsent mail (no server id), a draft, cached raw bodies, read/starred flags,
-- a threaded pair, and contacts that migration 3 backfills addresses from.

CREATE TABLE accounts (
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

CREATE TABLE folders (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    icon_name TEXT NOT NULL DEFAULT 'folder-symbolic'
);

CREATE TABLE emails (
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

CREATE UNIQUE INDEX idx_emails_uid
    ON emails (folder_id, server_id);

CREATE INDEX idx_emails_unread
    ON emails (folder_id, unread);

CREATE TABLE contacts (
    address TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE emails_fts USING fts5(
    sender, subject, preview,
    content='emails', content_rowid='id'
);

CREATE TRIGGER emails_fts_insert AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, sender, subject, preview)
    VALUES (new.id, new.sender, new.subject, new.preview);
END;

CREATE TRIGGER emails_fts_delete AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, sender, subject, preview)
    VALUES ('delete', old.id, old.sender, old.subject, old.preview);
END;

INSERT INTO accounts
    (id, email, display_name, imap_host, imap_port, smtp_host, smtp_port)
VALUES
    (1, 'ada@example.com', 'Ada', 'imap.example.com', 993,
     'smtp.example.com', 587),
    (2, 'grace@work.example', 'Grace', 'imap.work.example', 993,
     'smtp.work.example', 587);

INSERT INTO folders (id, account_id, name) VALUES
    (1, 1, 'INBOX'),
    (2, 1, 'Sent'),
    (3, 1, 'Outbox'),
    (4, 1, 'Drafts'),
    (5, 2, 'INBOX');

INSERT INTO contacts (address, name) VALUES
    ('ada@example.com', 'Ada Lovelace'),
    ('bob@example.com', 'Bob'),
    ('carol@example.com', '');

INSERT INTO emails
    (id, folder_id, server_id, sender, subject, preview, date, unread,
     starred, raw_message, message_id, in_reply_to, reference_ids,
     conversation_id)
VALUES
    (1, 1, '101', 'Ada Lovelace', 'Lunch plans', 'see you at one',
     '2026-07-16T12:00:00+00:00', 1, 0,
     CAST('From ada@example.com about lunch plans' AS BLOB),
     '<thread1@x>', '', '', 1),
    (2, 1, '102', 'Bob', 'Re: Lunch plans', 'bringing dessert',
     '2026-07-16T13:00:00+00:00', 1, 1,
     CAST('From bob@example.com bringing dessert' AS BLOB),
     '<thread1-r@x>', '<thread1@x>', '<thread1@x>', 1),
    (3, 1, '103', 'Stranger', 'Invoice 42', 'amount due soon',
     '2026-07-15T09:00:00+00:00', 0, 0,
     NULL,
     '<inv@x>', '', '', NULL),
    (4, 3, NULL, 'Ada Lovelace', 'Queued: trip report', '',
     '2026-07-16T14:00:00+00:00', 0, 0,
     CAST('Queued trip report body awaiting send' AS BLOB),
     '<queued1@x>', '', '', NULL),
    (5, 3, NULL, 'Ada Lovelace', 'Queued: no body yet', '',
     '2026-07-16T14:05:00+00:00', 0, 0,
     NULL,
     '<queued2@x>', '', '', NULL),
    (6, 4, NULL, '', 'Draft: hello', 'a few words',
     '2026-07-16T15:00:00+00:00', 0, 0,
     CAST('Draft hello body' AS BLOB),
     '', '', '', NULL),
    (7, 5, '7', 'Bob', 'Work: deploy', 'tonight at ten',
     '2026-07-16T16:00:00+00:00', 1, 0,
     CAST('Work deploy body' AS BLOB),
     '<w1@x>', '', '', NULL);

-- These rows predate the search index: mail synced before it arrived was
-- never indexed until migration 6 rebuilt it, so upgrading must make these
-- findable again.
DELETE FROM emails_fts;

PRAGMA user_version = 0;
