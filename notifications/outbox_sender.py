"""Outbox: вэб ставит ответы в БД, мы отправляем их через user-сессию Telethon."""
from __future__ import annotations

import asyncio
import logging

import aiosqlite
from telethon import TelegramClient

log = logging.getLogger(__name__)

POLL = 3


async def _send_one(client: TelegramClient, message_id: str, text: str) -> None:
    chat_id_s, msg_id_s = message_id.split(":", 1)
    chat_id = int(chat_id_s)
    reply_to = int(msg_id_s)
    await client.send_message(entity=chat_id, message=text, reply_to=reply_to)


async def run_outbox_sender(client: TelegramClient, conn: aiosqlite.Connection) -> None:
    log.info("Outbox-sender запущен.")
    while True:
        try:
            cur = await conn.execute(
                "SELECT id, message_id, text FROM outbox WHERE status='pending' ORDER BY id LIMIT 10"
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                oid, mid, text = int(r[0]), r[1], r[2]
                try:
                    await _send_one(client, mid, text)
                    await conn.execute(
                        "UPDATE outbox SET status='sent', sent_at=datetime('now') WHERE id=?",
                        (oid,),
                    )
                    log.info("Outbox -> отправлен ответ на %s (#%s)", mid, oid)
                except Exception as e:
                    await conn.execute(
                        "UPDATE outbox SET status='failed', error=? WHERE id=?",
                        (str(e)[:500], oid),
                    )
                    log.warning("Outbox: не удалось отправить #%s: %s", oid, e)
            await conn.commit()
        except asyncio.CancelledError:
            log.info("Outbox-sender остановлен.")
            return
        except Exception as e:
            log.exception("Outbox loop error: %s", e)
        await asyncio.sleep(POLL)
