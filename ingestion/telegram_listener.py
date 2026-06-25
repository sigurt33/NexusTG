"""Realtime ingest через Telethon events."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageEntityMention,
    MessageEntityMentionName,
    User,
)

from ingestion import context as ctx_mod

log = logging.getLogger(__name__)


def msg_id(chat_id: int, mid: int) -> str:
    return f"{chat_id}:{mid}"


def _serialize(obj: Any) -> str:
    try:
        return json.dumps(obj.to_dict(), default=str, ensure_ascii=False)
    except Exception:
        return "{}"


def _media_info(message: Any) -> tuple[str | None, int | None]:
    """Определить тип медиа и длительность (сек). Порядок важен: voice — это тоже document."""
    kind = None
    if getattr(message, "voice", None):
        kind = "voice"
    elif getattr(message, "video_note", None):
        kind = "video_note"
    elif getattr(message, "audio", None):
        kind = "audio"
    elif getattr(message, "sticker", None):
        kind = "sticker"
    elif getattr(message, "gif", None):
        kind = "gif"
    elif getattr(message, "photo", None):
        kind = "photo"
    elif getattr(message, "video", None):
        kind = "video"
    elif getattr(message, "document", None):
        kind = "document"
    duration = None
    if kind in ("voice", "video_note", "audio", "video"):
        f = getattr(message, "file", None)
        if f is not None and getattr(f, "duration", None):
            duration = int(f.duration)
    return kind, duration


class State:
    """Состояние сервиса: я, мой username, кеш entity, чат-broadcast флаги."""

    def __init__(self) -> None:
        self.me_id: int = 0
        self.me_username: str | None = None
        self.entity_cache: dict[int, Any] = {}
        self.broadcast_cache: dict[int, bool] = {}
        self.archived_chat_ids: set[int] = set()
        self.processing_off: set[int] = set()
        self._username_refresh_task: asyncio.Task | None = None
        self._chats_refresh_task: asyncio.Task | None = None

    async def refresh_me(self, client: TelegramClient) -> None:
        me = await client.get_me()
        self.me_id = me.id
        self.me_username = (me.username or "").lower() or None
        log.info("Я: id=%s username=@%s", self.me_id, self.me_username)

    async def username_refresher(self, client: TelegramClient) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                await self.refresh_me(client)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("Не удалось обновить me: %s", e)

    async def chats_refresher(self, client: TelegramClient, conn: aiosqlite.Connection) -> None:
        """Раз в 5 минут пересинхронизируем диалоги и перечитываем archived/processing,
        чтобы раз-архивирование чата или включение processing подхватывалось без рестарта."""
        from ingestion.chats_sync import sync_chats, archived_chat_ids
        while True:
            try:
                await asyncio.sleep(300)
                await sync_chats(client, conn)
                self.archived_chat_ids = await archived_chat_ids(conn)
                cur = await conn.execute("SELECT chat_id FROM chats WHERE COALESCE(processing,1)=0")
                self.processing_off = {int(r[0]) for r in await cur.fetchall()}
                await cur.close()
                log.info("chats refresh: архивных %s, выключенных %s",
                         len(self.archived_chat_ids), len(self.processing_off))
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("chats_refresher tick failed: %s", e)

    async def get_entity_cached(self, client: TelegramClient, eid: int) -> Any:
        if eid in self.entity_cache:
            return self.entity_cache[eid]
        try:
            ent = await client.get_entity(eid)
            self.entity_cache[eid] = ent
            self.broadcast_cache[eid] = bool(getattr(ent, "broadcast", False))
            return ent
        except Exception as e:
            log.debug("get_entity(%s) failed: %s", eid, e)
            return None


def _is_broadcast(chat: Any) -> bool:
    return bool(getattr(chat, "broadcast", False))


def _mentions_me(message: Any, me_id: int, me_username: str | None) -> bool:
    if getattr(message, "mentioned", False):
        return True
    entities = getattr(message, "entities", None) or []
    text = message.message or ""
    for ent in entities:
        if isinstance(ent, MessageEntityMentionName) and getattr(ent, "user_id", None) == me_id:
            return True
        if isinstance(ent, MessageEntityMention) and me_username:
            try:
                frag = text[ent.offset: ent.offset + ent.length].lstrip("@").lower()
                if frag == me_username:
                    return True
            except Exception:
                pass
    if me_username and ("@" + me_username) in text.lower():
        return True
    return False


async def _capture_message(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    state: State,
    message: Any,
    chat: Any,
    is_dm: bool,
    is_mention: bool,
    is_reply_to_me: bool,
    context_window: int,
) -> None:
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return
    chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or getattr(chat, "username", None)
    mid = msg_id(chat_id, message.id)

    sender_id = getattr(message, "sender_id", None)
    sender_name = None
    try:
        sender = await message.get_sender()
        if sender is not None:
            if getattr(sender, "username", None):
                sender_name = sender.username
            else:
                first = getattr(sender, "first_name", "") or ""
                last = getattr(sender, "last_name", "") or ""
                sender_name = (first + " " + last).strip() or getattr(sender, "title", None)
    except Exception:
        pass

    date_utc = message.date.isoformat() if message.date else datetime.now(timezone.utc).isoformat()
    reply_to_id = None
    if getattr(message, "reply_to_msg_id", None):
        reply_to_id = msg_id(chat_id, message.reply_to_msg_id)

    media_kind, media_duration = _media_info(message)
    media_status = "pending" if media_kind else None
    await conn.execute(
        """
        INSERT INTO messages
        (id, chat_id, chat_title, sender_id, sender_name, text, date_utc, reply_to_id,
         is_dm, is_mention, is_reply_to_me, is_context_only, raw_json,
         media_kind, media_status, media_duration)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            text=excluded.text,
            is_dm=excluded.is_dm,
            is_mention=excluded.is_mention,
            is_reply_to_me=excluded.is_reply_to_me,
            is_context_only=0,
            raw_json=excluded.raw_json,
            media_kind=excluded.media_kind,
            media_duration=excluded.media_duration
        """,
        (mid, chat_id, chat_title, sender_id, sender_name, message.message or "",
         date_utc, reply_to_id, int(is_dm), int(is_mention), int(is_reply_to_me),
         _serialize(message), media_kind, media_status, media_duration),
    )
    await conn.execute(
        """
        INSERT INTO ingest_state (chat_id, last_seen_id, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(chat_id) DO UPDATE SET
            last_seen_id = MAX(COALESCE(last_seen_id, 0), excluded.last_seen_id),
            updated_at = excluded.updated_at
        """,
        (chat_id, message.id),
    )
    await conn.commit()

    # ±context_window
    try:
        await ctx_mod.fetch_and_store(client, conn, chat_id, chat_title, message.id, window=context_window)
    except Exception as e:
        log.warning("Контекст для %s не сохранён: %s", mid, e)


async def evaluate_and_capture(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    state: State,
    message: Any,
    chat: Any,
    context_window: int,
) -> bool:
    """Проверка 3-х правил захвата. Возвращает True если сообщение захвачено."""
    if _is_broadcast(chat):
        return False
    chat_id_check = getattr(chat, "id", None)
    if chat_id_check is not None and chat_id_check in state.archived_chat_ids:
        return False
    if chat_id_check is not None and chat_id_check in state.processing_off:
        return False
    is_dm = isinstance(chat, User)
    is_mention = False
    is_reply_to_me = False

    if not is_dm:
        is_mention = _mentions_me(message, state.me_id, state.me_username)
        if getattr(message, "is_reply", False) or getattr(message, "reply_to_msg_id", None):
            try:
                reply_msg = await message.get_reply_message()
                if reply_msg is not None and getattr(reply_msg, "sender_id", None) == state.me_id:
                    is_reply_to_me = True
            except Exception as e:
                log.warning("get_reply_message failed for chat=%s msg=%s: %s — возможный пропуск reply-to-me",
                            getattr(chat, "id", None), getattr(message, "id", None), e)

    if not (is_dm or is_mention or is_reply_to_me):
        return False

    # пропустить собственные сообщения
    if getattr(message, "sender_id", None) == state.me_id:
        return False

    # пропустить любых ботов
    try:
        sender = await message.get_sender()
        if sender is not None and getattr(sender, "bot", False):
            return False
    except Exception:
        pass

    await _capture_message(client, conn, state, message, chat,
                          is_dm, is_mention, is_reply_to_me, context_window)
    return True


def register_handlers(
    client: TelegramClient,
    conn: aiosqlite.Connection,
    state: State,
    context_window: int,
) -> None:
    @client.on(events.NewMessage(incoming=True))
    async def _on_new(event: events.NewMessage.Event) -> None:
        try:
            chat = await event.get_chat()
            await evaluate_and_capture(client, conn, state, event.message, chat, context_window)
        except Exception as e:
            log.exception("NewMessage handler error: %s", e)

    @client.on(events.MessageEdited())
    async def _on_edit(event: events.MessageEdited.Event) -> None:
        try:
            chat = await event.get_chat()
            chat_id = getattr(chat, "id", None)
            if chat_id is None:
                return
            mid = msg_id(chat_id, event.message.id)
            cur = await conn.execute("SELECT raw_json FROM messages WHERE id = ?", (mid,))
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                # ещё не захвачено — может, теперь подходит под правила
                await evaluate_and_capture(client, conn, state, event.message, chat, context_window)
                return
            try:
                raw = json.loads(row[0] or "{}")
            except Exception:
                raw = {}
            edits = raw.get("edits") or []
            edits.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "text": event.message.message or "",
            })
            raw["edits"] = edits
            await conn.execute(
                "UPDATE messages SET text = ?, edited_at = ?, raw_json = ? WHERE id = ?",
                (event.message.message or "", datetime.now(timezone.utc).isoformat(),
                 json.dumps(raw, ensure_ascii=False), mid),
            )
            await conn.commit()
        except Exception as e:
            log.exception("MessageEdited handler error: %s", e)

    @client.on(events.MessageDeleted())
    async def _on_delete(event: events.MessageDeleted.Event) -> None:
        try:
            chat_id = event.chat_id
            if chat_id is None:
                # глобальный delete (личка) — пометим все совпадающие msg_id во всех чатах? нет, пропустим
                return
            now_iso = datetime.now(timezone.utc).isoformat()
            for did in event.deleted_ids:
                mid = msg_id(chat_id, did)
                await conn.execute(
                    "UPDATE messages SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (now_iso, mid),
                )
            await conn.commit()
        except Exception as e:
            log.exception("MessageDeleted handler error: %s", e)


async def run_listener(client: TelegramClient, conn: aiosqlite.Connection, state: State, context_window: int) -> None:
    register_handlers(client, conn, state, context_window)
    state._username_refresh_task = asyncio.create_task(state.username_refresher(client))
    state._chats_refresh_task = asyncio.create_task(state.chats_refresher(client, conn))
    log.info("Listener запущен, ждём события…")
    await client.run_until_disconnected()
