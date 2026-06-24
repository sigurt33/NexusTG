"""Задачник: список, создание, конвертация из сообщения, смена статуса, редактирование."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request, Response

from app.links import telegram_deep_link
from app.tasks import create_task_from_message

router = APIRouter()

STATUSES = ["todo", "doing", "waiting", "done", "cancelled"]
PRIORITIES = ["low", "normal", "high"]
STATUS_LABELS = {
    "todo": "📥 К работе",
    "doing": "🛠 В процессе",
    "waiting": "⏳ Жду",
    "done": "✅ Готово",
    "cancelled": "⛔ Отменено",
}
STATUS_BTN_LABELS = {
    "todo": "К работе",
    "doing": "В процессе",
    "waiting": "Жду",
    "done": "Готово",
    "cancelled": "Отменено",
}
PRIORITY_LABELS = {"low": "низкий", "normal": "обычный", "high": "высокий"}


def _due_local(due: str | None) -> str:
    """due_at из БД → значение для <input type=datetime-local> (YYYY-MM-DDTHH:MM)."""
    if not due:
        return ""
    return due.strip().replace(" ", "T")[:16]


async def _list_tasks(conn) -> list[dict]:
    cur = await conn.execute(
        """SELECT t.*, m.chat_title, m.sender_name, m.is_dm
           FROM tasks t LEFT JOIN messages m ON m.id=t.source_message_id
           ORDER BY
             CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
             t.due_at IS NULL, t.due_at,
             t.created_at DESC"""
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    for r in rows:
        sid = r.get("source_message_id")
        r["tg_link"] = telegram_deep_link(sid, is_dm=bool(r.get("is_dm"))) if sid else None
    return rows


async def _render_row(request, conn, task_id: int):
    cur = await conn.execute(
        """SELECT t.*, m.chat_title, m.sender_name, m.is_dm
           FROM tasks t LEFT JOIN messages m ON m.id=t.source_message_id
           WHERE t.id=?""",
        (task_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        raise HTTPException(404, "Задача не найдена")
    r = dict(row)
    sid = r.get("source_message_id")
    r["tg_link"] = telegram_deep_link(sid, is_dm=bool(r.get("is_dm"))) if sid else None
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/task_row.html",
        {"t": r, "statuses": STATUSES, "status_labels": STATUS_LABELS,
         "status_btn_labels": STATUS_BTN_LABELS, "priority_labels": PRIORITY_LABELS},
    )


@router.get("/tasks")
async def tasks_page(request: Request):
    conn = request.app.state.db
    rows = await _list_tasks(conn)
    by_status: dict[str, list[dict]] = {s: [] for s in STATUSES}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "tasks.html",
        {
            "by_status": by_status,
            "statuses": STATUSES,
            "priorities": PRIORITIES,
            "status_labels": STATUS_LABELS,
            "status_btn_labels": STATUS_BTN_LABELS,
            "priority_labels": PRIORITY_LABELS,
            "active": "tasks",
        },
    )


@router.post("/tasks/create")
async def tasks_create(
    request: Request,
    title: str = Form(...),
    priority: str = Form("normal"),
    due_at: str = Form(""),
    notes: str = Form(""),
):
    title = (title or "").strip()
    if not title:
        raise HTTPException(400, "title пустой")
    if priority not in PRIORITIES:
        priority = "normal"
    conn = request.app.state.db
    await conn.execute(
        "INSERT INTO tasks(title, priority, due_at, notes) VALUES (?,?,?,?)",
        (title, priority, (due_at.strip() or None), (notes.strip() or None)),
    )
    await conn.commit()
    resp = Response(status_code=204)
    resp.headers["HX-Redirect"] = "/tasks"
    return resp


@router.post("/tasks/from-message/{message_id:path}")
async def tasks_from_message(request: Request, message_id: str):
    conn = request.app.state.db
    task_id = await create_task_from_message(conn, message_id)
    if task_id is None:
        raise HTTPException(404, "Сообщение не найдено")
    html = (
        f'<a class="task-created" href="/tasks#task-{task_id}" '
        f'title="Перейти к задаче в задачнике">✓ Задача #{task_id} создана</a>'
    )
    return Response(status_code=200, content=html, media_type="text/html")


@router.post("/tasks/{task_id}/status")
async def tasks_set_status(request: Request, task_id: int, status: str = Form(...)):
    if status not in STATUSES:
        raise HTTPException(400, "bad status")
    conn = request.app.state.db
    if status in ("done", "cancelled"):
        await conn.execute(
            "UPDATE tasks SET status=?, updated_at=datetime('now'), completed_at=datetime('now') WHERE id=?",
            (status, task_id),
        )
    else:
        await conn.execute(
            "UPDATE tasks SET status=?, updated_at=datetime('now'), completed_at=NULL WHERE id=?",
            (status, task_id),
        )
    await conn.commit()
    return await _render_row(request, conn, task_id)


@router.get("/tasks/{task_id}/row")
async def tasks_row(request: Request, task_id: int):
    conn = request.app.state.db
    return await _render_row(request, conn, task_id)


@router.get("/tasks/{task_id}/edit-form")
async def tasks_edit_form(request: Request, task_id: int):
    conn = request.app.state.db
    cur = await conn.execute(
        """SELECT t.*, m.chat_title, m.sender_name, m.is_dm
           FROM tasks t LEFT JOIN messages m ON m.id=t.source_message_id
           WHERE t.id=?""",
        (task_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        raise HTTPException(404, "Задача не найдена")
    r = dict(row)
    r["due_local"] = _due_local(r.get("due_at"))
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/task_edit.html",
        {"t": r, "priorities": PRIORITIES, "priority_labels": PRIORITY_LABELS},
    )


@router.post("/tasks/{task_id}/edit")
async def tasks_edit(
    request: Request, task_id: int,
    title: str = Form(...),
    priority: str = Form("normal"),
    due_at: str = Form(""),
    notes: str = Form(""),
):
    if priority not in PRIORITIES:
        priority = "normal"
    conn = request.app.state.db
    new_due = due_at.strip() or None
    cur = await conn.execute("SELECT due_at FROM tasks WHERE id=?", (task_id,))
    old = await cur.fetchone()
    await cur.close()
    old_due = old["due_at"] if old else None
    if new_due != old_due:
        await conn.execute(
            "UPDATE tasks SET title=?, priority=?, due_at=?, notes=?, reminder_stage=0, updated_at=datetime('now') WHERE id=?",
            (title.strip(), priority, new_due, (notes.strip() or None), task_id),
        )
    else:
        await conn.execute(
            "UPDATE tasks SET title=?, priority=?, due_at=?, notes=?, updated_at=datetime('now') WHERE id=?",
            (title.strip(), priority, new_due, (notes.strip() or None), task_id),
        )
    await conn.commit()
    return await _render_row(request, conn, task_id)


@router.post("/tasks/{task_id}/delete")
async def tasks_delete(request: Request, task_id: int):
    conn = request.app.state.db
    await conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    await conn.commit()
    return Response(status_code=200, content="")
