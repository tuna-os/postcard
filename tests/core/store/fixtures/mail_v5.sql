-- Frozen version-5 schema with representative mail. A historical artifact: it
-- captures the database after migration 5 (parent_id, delimiter,
-- sender_address, recipient columns, goa_id) and before migration 6, so it
-- must NOT be updated to match the current schema. Column order mirrors what
-- the migrations appended, oldest first. The upgrade tests load it, resume
-- from the middle of the migration history, and prove everything it holds
-- survives -- including the folder hierarchy and addresses earlier migrations
-- established.

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    smtp_host TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    imap_security TEXT NOT NULL DEFAULT 'tls',
    smtp_security TEXT NOT NULL DEFAULT 'starttls',
    goa_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE folders (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    icon_name TEXT NOT NULL DEFAULT 'folder-symbolic',
    parent_id INTEGER REFERENCES folders(id),
    delimiter TEXT NOT NULL DEFAULT '/'
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
    conversation_id INTEGER,
    sender_address TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    recipient_address TEXT NOT NULL DEFAULT ''
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
    (id, email, display_name, imap_host, imap_port, smtp_host, smtp_port,
     goa_id)
VALUES
    (1, 'ada@example.com', 'Ada', 'imap.example.com', 993,
     'smtp.example.com', 587, ''),
    (2, 'grace@work.example', 'Grace', 'imap.work.example', 993,
     'smtp.work.example', 587, 'account_1700_0');

INSERT INTO folders (id, account_id, name, parent_id, delimiter) VALUES
    (1, 1, 'INBOX', NULL, '/'),
    (2, 1, 'Sent', NULL, '/'),
    (3, 1, 'Outbox', NULL, '/'),
    (4, 2, 'INBOX', NULL, '/'),
    (5, 1, 'Work', NULL, '.'),
    (6, 1, 'Work.Projects', 5, '.');

INSERT INTO contacts (address, name) VALUES
    ('ada@example.com', 'Ada Lovelace'),
    ('bob@example.com', 'Bob');

INSERT INTO emails
    (id, folder_id, server_id, sender, subject, preview, date, unread,
     starred, raw_message, message_id, in_reply_to, reference_ids,
     conversation_id, sender_address, recipient, recipient_address)
VALUES
    (1, 1, '101', 'Ada Lovelace', 'Lunch plans', 'see you at one',
     '2026-07-16T12:00:00+00:00', 1, 0,
     CAST('From ada@example.com about lunch plans' AS BLOB),
     '<thread1@x>', '', '', 1,
     'ada@example.com', 'Bob', 'bob@example.com'),
    (2, 1, '102', 'Bob', 'Re: Lunch plans', 'bringing dessert',
     '2026-07-16T13:00:00+00:00', 1, 1,
     CAST('From bob@example.com bringing dessert' AS BLOB),
     '<thread1-r@x>', '<thread1@x>', '<thread1@x>', 1,
     'bob@example.com', 'Ada Lovelace', 'ada@example.com'),
    (3, 6, '201', 'Bob', 'Project notes', 'milestones draft',
     '2026-07-15T09:00:00+00:00', 0, 0,
     CAST('Project notes body' AS BLOB),
     '<notes@x>', '', '', NULL,
     'bob@example.com', '', ''),
    (4, 3, NULL, 'Ada Lovelace', 'Queued: trip report', '',
     '2026-07-16T14:00:00+00:00', 0, 0,
     CAST('Queued trip report body awaiting send' AS BLOB),
     '<queued1@x>', '', '', NULL,
     'ada@example.com', 'Bob', 'bob@example.com'),
    (5, 4, '7', 'Bob', 'Work: deploy', 'tonight at ten',
     '2026-07-16T16:00:00+00:00', 1, 0,
     CAST('Work deploy body' AS BLOB),
     '<w1@x>', '', '', NULL,
     'bob@example.com', '', '');

PRAGMA user_version = 5;
