"""SQLite (aiosqlite) — соединение, WAL, применение schema.sql + аддитивные миграции."""
from __future__ import annotations

import aiosqlite

from app.config import DB_PATH, SCHEMA_PATH, DATA_DIR


async def connect() -> aiosqlite.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA busy_timeout=30000;")
    return conn


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return {r[1] for r in rows}


async def ensure_columns(conn: aiosqlite.Connection) -> None:
    """Аддитивные миграции: добавить новые колонки и таблицы, если их нет."""
    # chats.processing
    cols = await _table_columns(conn, "chats")
    if "processing" not in cols:
        await conn.execute("ALTER TABLE chats ADD COLUMN processing INTEGER NOT NULL DEFAULT 1")
    # topics.parent_id
    cols = await _table_columns(conn, "topics")
    if "parent_id" not in cols:
        await conn.execute("ALTER TABLE topics ADD COLUMN parent_id INTEGER")
    # user_rules — «Алина-режим»: правила свободным текстом, подмешиваются в промт
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_text TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # reply_templates — заготовки ответов
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS reply_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    # outbox — очередь исходящих ответов из веба → отправит scheduler в run.ps1
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            sent_at TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    # classification_examples
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS classification_examples (
            message_id TEXT PRIMARY KEY,
            urgency INTEGER NOT NULL,
            importance INTEGER NOT NULL,
            topics_json TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
        """
    )
    await conn.commit()


async def init_db() -> None:
    """Применить schema.sql (идемпотентно) + ensure_columns()."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = await connect()
    try:
        await conn.executescript(sql)
        await ensure_columns(conn)
        await conn.commit()
    finally:
        await conn.close()
