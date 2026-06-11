"""Шаблоны быстрых ответов + outbox-очередь для отправки."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

router = APIRouter()


async def _all_templates(conn):
    cur = await conn.execute(
        "SELECT id, title, text, sort_order FROM reply_templates ORDER BY sort_order, id"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


@router.get("/templates")
async def templates_page(request: Request):
    conn = request.app.state.db
    rows = await _all_templates(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "templates.html",
        {"templates_list": rows, "active": "templates"},
    )


@router.post("/templates/add")
async def add_template(request: Request, title: str = Form(...), text: str = Form(...)):
    if not title.strip() or not text.strip():
        raise HTTPException(400, "Заполни title и text")
    conn = request.app.state.db
    await conn.execute(
        "INSERT INTO reply_templates(title, text) VALUES (?, ?)",
        (title.strip(), text.strip()),
    )
    await conn.commit()
    rows = await _all_templates(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/templates_list.html", {"templates_list": rows},
    )


@router.post("/templates/{tpl_id}/edit")
async def edit_template(request: Request, tpl_id: int, title: str = Form(...), text: str = Form(...)):
    conn = request.app.state.db
    await conn.execute(
        "UPDATE reply_templates SET title=?, text=? WHERE id=?",
        (title.strip(), text.strip(), tpl_id),
    )
    await conn.commit()
    rows = await _all_templates(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/templates_list.html", {"templates_list": rows},
    )


@router.post("/templates/{tpl_id}/delete")
async def delete_template(request: Request, tpl_id: int):
    conn = request.app.state.db
    await conn.execute("DELETE FROM reply_templates WHERE id=?", (tpl_id,))
    await conn.commit()
    rows = await _all_templates(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/templates_list.html", {"templates_list": rows},
    )


@router.get("/api/templates")
async def api_templates(request: Request):
    """JSON для дропдауна на карточке сообщения."""
    conn = request.app.state.db
    rows = await _all_templates(conn)
    return rows


@router.post("/message/{message_id:path}/reply")
async def queue_reply(request: Request, message_id: str, text: str = Form(...)):
    """Положить ответ в outbox — scheduler в run.ps1 отправит его в Telegram."""
    if not text.strip():
        raise HTTPException(400, "Пустой ответ")
    conn = request.app.state.db
    # проверим что message_id существует
    cur = await conn.execute("SELECT 1 FROM messages WHERE id=?", (message_id,))
    exists = await cur.fetchone(); await cur.close()
    if not exists:
        raise HTTPException(404, "Сообщение не найдено")
    await conn.execute(
        "INSERT INTO outbox(message_id, text) VALUES (?, ?)",
        (message_id, text.strip()),
    )
    await conn.commit()
    # вернём короткий статус-фрагмент
    return {"status": "queued", "message": "Ответ поставлен в очередь — обычно отправляется за 3-5 секунд"}
