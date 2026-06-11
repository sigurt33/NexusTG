"""Управление списком чатов: allow/block (processing flag) и ручной sync."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.config import SESSION_PATH, load_config

log = logging.getLogger(__name__)
router = APIRouter()


async def _fetch_chats(conn, view: str = "active"):
    where = []
    if view == "active":
        where.append("COALESCE(archived,0)=0 AND COALESCE(processing,1)=1")
    elif view == "archived":
        where.append("(COALESCE(archived,0)=1 OR COALESCE(processing,1)=0)")
    # all → no filter
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    cur = await conn.execute(
        f"""
        SELECT c.chat_id, c.title, c.archived, COALESCE(c.processing,1) AS processing,
               c.updated_at,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id AND m.is_context_only=0) AS captured,
               (SELECT MAX(date_utc) FROM messages m WHERE m.chat_id=c.chat_id) AS last_msg
        FROM chats c
        {wsql}
        ORDER BY captured DESC, c.title
        """
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


async def _fetch_chat(conn, chat_id: int) -> dict | None:
    cur = await conn.execute(
        """
        SELECT c.chat_id, c.title, c.archived, COALESCE(c.processing,1) AS processing,
               c.updated_at,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id AND m.is_context_only=0) AS captured,
               (SELECT MAX(date_utc) FROM messages m WHERE m.chat_id=c.chat_id) AS last_msg
        FROM chats c WHERE c.chat_id=?
        """,
        (chat_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


@router.get("/chats")
async def chats_page(request: Request, view: str = "active"):
    conn = request.app.state.db
    rows = await _fetch_chats(conn, view=view)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "chats.html",
        {"chats": rows, "view": view, "active": "chats"},
    )


@router.post("/chats/{chat_id}/toggle")
async def toggle(request: Request, chat_id: int):
    conn = request.app.state.db
    cur = await conn.execute("SELECT COALESCE(processing,1) FROM chats WHERE chat_id=?", (chat_id,))
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return ""
    new_val = 0 if int(row[0]) == 1 else 1
    await conn.execute("UPDATE chats SET processing=?, updated_at=datetime('now') WHERE chat_id=?",
                       (new_val, chat_id))
    await conn.commit()
    chat = await _fetch_chat(conn, chat_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "partials/chat_row.html", {"c": chat})


@router.post("/chats/sync")
async def sync(request: Request):
    """Запустить sync_chats через user-сессию Telethon (если есть)."""
    from telethon import TelegramClient
    from ingestion.chats_sync import sync_chats

    conn = request.app.state.db
    cfg = load_config()
    try:
        if not SESSION_PATH.exists():
            raise RuntimeError("Сессия Telegram не найдена")
        client = TelegramClient(str(SESSION_PATH.with_suffix("")), cfg.tg_api_id, cfg.tg_api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Сессия не авторизована")
        try:
            await sync_chats(client, conn)
        finally:
            await client.disconnect()
    except Exception as e:
        log.warning("chats sync через web упал: %s", e)
        # вернём фрагмент с ошибкой
        templates = request.app.state.templates
        rows = await _fetch_chats(conn, view="active")
        return templates.TemplateResponse(
            request, "partials/chats_table.html",
            {"chats": rows, "sync_error": str(e)},
        )
    rows = await _fetch_chats(conn, view="active")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/chats_table.html",
        {"chats": rows, "sync_ok": True},
    )
