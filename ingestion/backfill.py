"""Backfill 30 дней. Устойчив к перезапуску через ingest_state.last_backfilled_id."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import User, Chat, Channel

from ingestion.telegram_listener import State, evaluate_and_capture

log = logging.getLogger(__name__)


def _is_broadcast(entity: Any) -> bool:
    # Channel.broadcast=True → канал-вещание; megagroup=True → группа
    if isinstance(entity, Channel) and getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
        return True
    return False


async def _get_state(conn: aiosqlite.Connection, chat_id: int) -> tuple[int | None, int]:
    cur = await conn.execute(
        "SELECT last_backfilled_id, backfill_done FROM ingest_state WHERE chat_id = ?",
        (chat_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return None, 0
    return row[0], row[1]


async def _set_state(conn: aiosqlite.Connection, chat_id: int, last_id: int, done: bool = False) -> None:
    await conn.execute(
        """
        INSERT INTO ingest_state (chat_id, last_backfilled_id, backfill_done, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(chat_id) DO UPDATE SET
            last_backfilled_id = MAX(COALESCE(last_backfilled_id, 0), excluded.last_backfilled_id),
            backfill_done = MAX(backfill_done, excluded.backfill_done),
            updated_at = excluded.updated_at
        """,
        (chat_id, last_id, 1 if done else 0),
    )
    await conn.commit()


async def backfill_chat(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    state: State,
    dialog: Any,
    cutoff: datetime,
    context_window: int,
) -> int:
    """Backfill одного чата от cutoff до now. Возвращает кол-во захваченных."""
    entity = dialog.entity
    chat_id = entity.id
    chat_title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or getattr(entity, "username", None)
    if chat_id in getattr(state, "processing_off", set()):
        log.info("Backfill чата '%s' (id=%s) пропущен: processing=0", chat_title, chat_id)
        return 0
    last_backfilled, done = await _get_state(conn, chat_id)
    if done:
        return 0

    # Идём вперёд по времени от cutoff (reverse=True), пропуская уже обработанные через min_id.
    min_id = last_backfilled or 0
    captured = 0
    max_seen_id = min_id

    log.info("Backfill чата '%s' (id=%s), min_id=%s", chat_title, chat_id, min_id)

    while True:
        try:
            async for message in client.iter_messages(
                entity,
                offset_date=cutoff,
                reverse=True,
                min_id=min_id,
                limit=200,
            ):
                if message is None:
                    continue
                max_seen_id = max(max_seen_id, message.id)
                try:
                    captured_now = await evaluate_and_capture(
                        client, conn, state, message, entity, context_window
                    )
                    if captured_now:
                        captured += 1
                except Exception as e:
                    log.warning("capture error на %s:%s: %s", chat_id, message.id, e)
                # чекпойнт каждые 50 сообщений
                if max_seen_id and max_seen_id % 50 == 0:
                    await _set_state(conn, chat_id, max_seen_id, done=False)
            break
        except FloodWaitError as e:
            log.warning("FloodWait %ss на чате %s — спим", e.seconds, chat_id)
            await _set_state(conn, chat_id, max_seen_id, done=False)
            await asyncio.sleep(e.seconds + 1)
            min_id = max_seen_id
            continue
        except Exception as e:
            log.exception("Backfill чата %s упал: %s", chat_id, e)
            await _set_state(conn, chat_id, max_seen_id, done=False)
            return captured

    await _set_state(conn, chat_id, max_seen_id, done=True)
    log.info("Backfill '%s' завершён, захвачено %s", chat_title, captured)
    return captured


async def run_backfill(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    state: State,
    backfill_days: int,
    context_window: int,
) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=backfill_days)
    log.info("Старт backfill с cutoff=%s", cutoff.isoformat())

    dialogs: list[Any] = []
    skipped_archived = 0
    async for d in client.iter_dialogs():
        ent = d.entity
        if _is_broadcast(ent):
            continue
        if getattr(d, "archived", False) or getattr(d, "folder_id", 0) == 1:
            skipped_archived += 1
            continue
        dialogs.append(d)
    log.info("Пропущено архивных чатов: %s", skipped_archived)

    log.info("Чатов для backfill: %s", len(dialogs))
    total = 0
    for d in dialogs:
        try:
            n = await backfill_chat(client, conn, state, d, cutoff, context_window)
            total += n
        except Exception as e:
            log.exception("Чат %s упал: %s", getattr(d.entity, "id", "?"), e)
        await asyncio.sleep(1)

    log.info("Backfill завершён. Всего захвачено: %s", total)
