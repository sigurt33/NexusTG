"""Синхронизация списка чатов и их archived-флага из Telegram в SQLite."""
from __future__ import annotations

import logging
from typing import Iterable

import aiosqlite
from telethon import TelegramClient

log = logging.getLogger(__name__)


async def sync_chats(client: TelegramClient, conn: aiosqlite.Connection) -> tuple[int, int]:
    """
    Прочитать все диалоги через Telethon и upsert в таблицу `chats`.
    Возвращает (total, archived).
    """
    await conn.execute("PRAGMA busy_timeout = 30000")
    total = 0
    archived = 0
    async for d in client.iter_dialogs():
        chat_id = int(d.id)
        title = (d.name or "").strip()
        is_archived = 1 if (getattr(d, "archived", False) or getattr(d, "folder_id", 0) == 1) else 0
        await conn.execute(
            """
            INSERT INTO chats (chat_id, title, archived, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                archived=excluded.archived,
                updated_at=datetime('now')
            """,
            (chat_id, title, is_archived),
        )
        total += 1
        archived += is_archived
    await conn.commit()
    log.info("chats sync: всего %s, в архиве %s", total, archived)
    return total, archived


async def archived_chat_ids(conn: aiosqlite.Connection) -> set[int]:
    cur = await conn.execute("SELECT chat_id FROM chats WHERE archived=1")
    rows = await cur.fetchall()
    await cur.close()
    return {int(r[0]) for r in rows}
