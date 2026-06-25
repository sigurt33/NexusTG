"""Медиа-воркер: транскрипция голосовых (Gemini) + форс-приоритет для остального медиа."""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import aiosqlite
from openai import AsyncOpenAI
from telethon import TelegramClient

from classifier.grok_worker import _record_usage, _trivial_topic_id

log = logging.getLogger(__name__)

POLL_SLEEP = 15
BATCH = 20
TRANSCRIBE_CONCURRENCY = 2

VOICE_KINDS = {"voice", "video_note", "audio"}
TRIVIAL_KINDS = {"sticker", "gif"}
KIND_LABELS = {
    "voice": "Голосовое", "video_note": "Видео-кружок", "audio": "Аудио",
    "photo": "Фото", "video": "Видео", "document": "Документ",
    "sticker": "Стикер", "gif": "GIF",
}

_MEDIA_TOPIC_ID: int | None = None


def _make_client(cfg) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=cfg.xai_api_key, base_url=cfg.llm_base_url)


async def _media_topic_id(conn: aiosqlite.Connection) -> int:
    global _MEDIA_TOPIC_ID
    if _MEDIA_TOPIC_ID is not None:
        return _MEDIA_TOPIC_ID
    cur = await conn.execute("SELECT id FROM topics WHERE slug='media'")
    row = await cur.fetchone(); await cur.close()
    if row:
        _MEDIA_TOPIC_ID = int(row[0]); return _MEDIA_TOPIC_ID
    await conn.execute(
        "INSERT INTO topics(slug, label_ru, description, hidden) "
        "VALUES ('media', 'Медиа', 'Голосовые/фото/видео/документы — требуют просмотра в Telegram.', 0)"
    )
    await conn.commit()
    cur = await conn.execute("SELECT id FROM topics WHERE slug='media'")
    row = await cur.fetchone(); await cur.close()
    _MEDIA_TOPIC_ID = int(row[0]); return _MEDIA_TOPIC_ID


def _decide(kind: str, duration: int | None, caption: str, max_minutes: int) -> str:
    """Маршрутизация (чистая функция). Возвращает action."""
    cap = (caption or "").strip()
    max_sec = max_minutes * 60
    if kind in VOICE_KINDS:
        if duration is not None and duration > max_sec:
            return "force_long_voice"
        return "transcribe"
    if kind in TRIVIAL_KINDS:
        return "trivial"
    if cap:
        return "caption"
    return "force_media"


async def _set_done_text(conn, mid: str, text: str, status: str = "done") -> None:
    """Текст готов — пусть классификатор обработает обычным путём (не скорим тут)."""
    await conn.execute("UPDATE messages SET text=?, media_status=? WHERE id=?", (text, status, mid))
    await conn.commit()


async def _force_priority(conn, mid: str, text: str, urgency: int, importance: int,
                          score: float, rationale: str, slug: str, status: str = "done") -> None:
    await conn.execute("UPDATE messages SET text=?, media_status=? WHERE id=?", (text, status, mid))
    tid = await (_media_topic_id(conn) if slug == "media" else _trivial_topic_id(conn))
    await conn.execute(
        "INSERT OR IGNORE INTO message_topics(message_id, topic_id, confidence) VALUES (?,?,1.0)",
        (mid, tid),
    )
    await conn.execute("UPDATE topics SET message_count = message_count + 1 WHERE id=?", (tid,))
    await conn.execute(
        """INSERT OR REPLACE INTO priorities
           (message_id, urgency, importance, score, rationale, classified_at, model_version)
           VALUES (?,?,?,?,?, datetime('now'), 'media-prefilter')""",
        (mid, urgency, importance, score, rationale),
    )
    await conn.commit()


_TRANSCRIBE_PROMPT = "Расшифруй это голосовое сообщение дословно, на языке оригинала. Верни только текст расшифровки, без комментариев."


async def transcribe_audio(openai_client: AsyncOpenAI, cfg, data: bytes, conn) -> str:
    """Транскрипция OGG/Opus. Сначала OpenAI-совместимый input_audio, при ошибке — нативный generateContent."""
    b64 = base64.b64encode(data).decode("ascii")
    try:
        resp = await openai_client.chat.completions.create(
            model=cfg.grok_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _TRANSCRIBE_PROMPT},
                    {"type": "input_audio", "input_audio": {"data": b64, "format": "ogg"}},
                ],
            }],
            temperature=0.0,
        )
        try:
            u = resp.usage
            if u:
                await _record_usage(conn, u.prompt_tokens or 0, u.completion_tokens or 0)
        except Exception:
            pass
        return resp.choices[0].message.content or ""
    except Exception as e:
        log.warning("input_audio транскрипция не удалась (%s) — пробую нативный generateContent", e)
        return await _transcribe_native(cfg, b64)


async def _transcribe_native(cfg, b64: str) -> str:
    import httpx
    root = cfg.llm_base_url.split("/openai")[0].rstrip("/")
    url = f"{root}/models/{cfg.grok_model}:generateContent?key={cfg.xai_api_key}"
    payload = {"contents": [{"parts": [
        {"text": _TRANSCRIBE_PROMPT},
        {"inline_data": {"mime_type": "audio/ogg", "data": b64}},
    ]}]}
    async with httpx.AsyncClient(timeout=120) as cli:
        rr = await cli.post(url, json=payload)
        rr.raise_for_status()
        j = rr.json()
    return j["candidates"][0]["content"]["parts"][0]["text"]


async def _process_one(client: TelegramClient, openai_client: AsyncOpenAI, conn, cfg,
                       row: dict, max_minutes: int, sem: asyncio.Semaphore) -> None:
    mid = row["id"]
    kind = row["media_kind"] or ""
    dur = row["media_duration"]
    caption = row["text"] or ""
    label = KIND_LABELS.get(kind, "Медиа")
    try:
        action = _decide(kind, dur, caption, max_minutes)
        if action == "transcribe":
            chat_id = int(mid.split(":")[0]); msg_id = int(mid.split(":")[1])
            async with sem:
                msg = await client.get_messages(chat_id, ids=msg_id)
                if msg is None:
                    raise RuntimeError("сообщение не найдено для скачивания")
                data = await client.download_media(msg, file=bytes)
            if not data:
                raise RuntimeError("пустое аудио")
            transcript = (await transcribe_audio(openai_client, cfg, data, conn) or "").strip()
            if not transcript:
                raise RuntimeError("пустая транскрипция")
            text = "🎤 Транскрипция голосового сообщения:\n\n" + transcript
            cap = caption.strip()
            if cap:
                text += "\n\nПодпись: " + cap
            await _set_done_text(conn, mid, text)
        elif action == "force_long_voice":
            await _force_priority(
                conn, mid, f"🎤 Голосовое сообщение длиннее {max_minutes} минут — прослушай в Telegram",
                5, 5, 5.0, f"media: {kind} >max", "media")
        elif action == "trivial":
            await _force_priority(conn, mid, caption.strip() or f"[{label}]",
                                  1, 1, 1.0, f"media: {kind}", "trivial")
        elif action == "caption":
            await _set_done_text(conn, mid, f"📎 [медиа: {label}] " + caption.strip())
        else:  # force_media
            await _force_priority(conn, mid, f"📎 {label} — открой в Telegram, чтобы посмотреть",
                                  5, 5, 5.0, f"media: {kind}", "media")
    except Exception as e:
        log.warning("media %s (%s) failed: %s", mid, kind, e)
        try:
            txt = ("🎤 Не удалось расшифровать — прослушай в Telegram"
                   if kind in VOICE_KINDS else f"📎 {label} — открой в Telegram")
            await _force_priority(conn, mid, txt, 5, 5, 5.0, f"media error: {kind}", "media", status="error")
        except Exception as e2:
            log.warning("media %s error-fallback failed: %s", mid, e2)


async def run_media_worker(client: TelegramClient, conn: aiosqlite.Connection, cfg) -> None:
    openai_client = _make_client(cfg)
    sem = asyncio.Semaphore(TRANSCRIBE_CONCURRENCY)
    max_minutes = getattr(cfg, "voice_transcribe_max_minutes", 3)
    log.info("Media worker запущен (порог транскрипции %s мин).", max_minutes)
    while True:
        try:
            cur = await conn.execute(
                """SELECT m.id, m.media_kind, m.media_duration, m.text
                   FROM messages m LEFT JOIN chats c ON c.chat_id=m.chat_id
                   WHERE m.media_status='pending' AND m.deleted_at IS NULL
                     AND COALESCE(c.archived,0)=0 AND COALESCE(c.processing,1)=1
                   ORDER BY m.date_utc DESC LIMIT ?""",
                (BATCH,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            await cur.close()
            for r in rows:
                await _process_one(client, openai_client, conn, cfg, r, max_minutes, sem)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("media worker round failed: %s", e)
        await asyncio.sleep(POLL_SLEEP)
