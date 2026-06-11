"""Вытягивание ±5 контекстных сообщений вокруг захваченного."""
from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite
from telethon import TelegramClient
from telethon.errors import FloodWaitError
import asyncio

log = logging.getLogger(__name__)


def _msg_id(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


def _serialize(msg: Any) -> str:
    try:
        return json.dumps(msg.to_dict(), default=str, ensure_ascii=False)
    except Exception:
        return "{}"


async def _sender_name(msg: Any) -> str | None:
    try:
        sender = await msg.get_sender()
        if sender is None:
            return None
        if getattr(sender, "username", None):
            return sender.username
        first = getattr(sender, "first_name", "") or ""
        last = getattr(sender, "last_name", "") or ""
        full = (first + " " + last).strip()
        return full or getattr(sender, "title", None)
    except Exception:
        return None


async def _insert_context_message(conn: aiosqlite.Connection, msg: Any, chat_id: int, chat_title: str | None) -> str:
    """Вставить контекстное сообщение если его ещё нет. Возвращает id."""
    cid = _msg_id(chat_id, msg.id)
    cur = await conn.execute("SELECT 1 FROM messages WHERE id = ?", (cid,))
    if await cur.fetchone():
        await cur.close()
        return cid
    await cur.close()

    sender_name = await _sender_name(msg)
    sender_id = getattr(msg, "sender_id", None)
    date_utc = msg.date.isoformat() if msg.date else None
    reply_to = None
    if getattr(msg, "reply_to_msg_id", None):
        reply_to = _msg_id(chat_id, msg.reply_to_msg_id)

    await conn.execute(
        """
        INSERT OR IGNORE INTO messages
        (id, chat_id, chat_title, sender_id, sender_name, text, date_utc, reply_to_id,
         is_dm, is_mention, is_reply_to_me, is_context_only, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 1, ?)
        """,
        (cid, chat_id, chat_title, sender_id, sender_name, msg.message or "", date_utc,
         reply_to, _serialize(msg)),
    )
    return cid


async def fetch_and_store(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    chat_id: int,
    chat_title: str | None,
    msg_id: int,
    window: int = 5,
) -> None:
    """Тянем ±window сообщений вокруг msg_id; кладём в messages + context_links."""
    self_id = _msg_id(chat_id, msg_id)

    # self link (position 0)
    await conn.execute(
        "INSERT OR IGNORE INTO context_links (message_id, context_msg_id, position) VALUES (?, ?, 0)",
        (self_id, self_id),
    )

    try:
        # older: offset_id=msg_id, limit=window — идём в прошлое
        older = []
        async for m in client.iter_messages(chat_id, offset_id=msg_id, limit=window):
            older.append(m)
        # newer: reverse=True, min_id=msg_id, limit=window
        newer = []
        async for m in client.iter_messages(chat_id, min_id=msg_id, limit=window, reverse=True):
            newer.append(m)
    except FloodWaitError as e:
        log.warning("FloodWait в контексте: %ss", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return
    except Exception as e:
        log.warning("Не удалось получить контекст для %s: %s", self_id, e)
        return

    # older[0] — ближайшее прошлое → position -1; older[1] → -2 и т.д.
    for idx, m in enumerate(older, start=1):
        cid = await _insert_context_message(conn, m, chat_id, chat_title)
        await conn.execute(
            "INSERT OR IGNORE INTO context_links (message_id, context_msg_id, position) VALUES (?, ?, ?)",
            (self_id, cid, -idx),
        )
    for idx, m in enumerate(newer, start=1):
        cid = await _insert_context_message(conn, m, chat_id, chat_title)
        await conn.execute(
            "INSERT OR IGNORE INTO context_links (message_id, context_msg_id, position) VALUES (?, ?, ?)",
            (self_id, cid, idx),
        )
    await conn.commit()
