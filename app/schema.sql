-- aichatpom schema (Phase 1+)
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,           -- "{chat_id}:{msg_id}"
    chat_id         INTEGER NOT NULL,
    chat_title      TEXT,
    sender_id       INTEGER,
    sender_name     TEXT,
    text            TEXT,
    date_utc        TEXT NOT NULL,              -- ISO 8601
    reply_to_id     TEXT,
    is_dm           INTEGER NOT NULL DEFAULT 0,
    is_mention      INTEGER NOT NULL DEFAULT 0,
    is_reply_to_me  INTEGER NOT NULL DEFAULT 0,
    is_context_only INTEGER NOT NULL DEFAULT 0,
    edited_at       TEXT,
    deleted_at      TEXT,
    raw_json        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_date         ON messages(date_utc DESC);
CREATE INDEX IF NOT EXISTS idx_messages_chat_date    ON messages(chat_id, date_utc);
CREATE INDEX IF NOT EXISTS idx_messages_is_dm        ON messages(is_dm);
CREATE INDEX IF NOT EXISTS idx_messages_is_mention   ON messages(is_mention);
CREATE INDEX IF NOT EXISTS idx_messages_is_ctx_only  ON messages(is_context_only);

CREATE TABLE IF NOT EXISTS context_links (
    message_id      TEXT NOT NULL,
    context_msg_id  TEXT NOT NULL,
    position        INTEGER NOT NULL,           -- -5..+5, 0 = self
    PRIMARY KEY (message_id, context_msg_id),
    FOREIGN KEY (message_id)     REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY (context_msg_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ctx_msg ON context_links(message_id);

CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,
    label_ru      TEXT NOT NULL,
    description   TEXT,
    embedding     BLOB,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    message_count INTEGER NOT NULL DEFAULT 0,
    hidden        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS message_topics (
    message_id  TEXT NOT NULL,
    topic_id    INTEGER NOT NULL,
    confidence  REAL,
    PRIMARY KEY (message_id, topic_id),
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY (topic_id)   REFERENCES topics(id)   ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS priorities (
    message_id     TEXT PRIMARY KEY,
    urgency        INTEGER NOT NULL CHECK(urgency BETWEEN 1 AND 5),
    importance     INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
    score          REAL NOT NULL,
    rationale      TEXT,
    classified_at  TEXT NOT NULL DEFAULT (datetime('now')),
    model_version  TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT NOT NULL,
    action        TEXT NOT NULL CHECK(action IN ('done','snoozed','archived','unread')),
    snooze_until  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_actions_msg ON user_actions(message_id);

CREATE TABLE IF NOT EXISTS ingest_state (
    chat_id              INTEGER PRIMARY KEY,
    last_backfilled_id   INTEGER,
    last_seen_id         INTEGER,
    backfill_done        INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS grok_usage (
    date              TEXT PRIMARY KEY,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS pending_notifications (
    message_id TEXT PRIMARY KEY,
    queued_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

-- FTS5 full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, chat_title, sender_name,
    content='messages', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, chat_title, sender_name)
    VALUES (new.rowid, new.text, new.chat_title, new.sender_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, chat_title, sender_name)
    VALUES ('delete', old.rowid, old.text, old.chat_title, old.sender_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, chat_title, sender_name)
    VALUES ('delete', old.rowid, old.text, old.chat_title, old.sender_name);
    INSERT INTO messages_fts(rowid, text, chat_title, sender_name)
    VALUES (new.rowid, new.text, new.chat_title, new.sender_name);
END;
