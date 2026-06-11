"""Topic admin: rename / hide / unhide / merge / bulk-merge / messages-of-topic / parent / tree."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

router = APIRouter()


async def _all_topics(conn, include_hidden: bool = False):
    where = "" if include_hidden else "WHERE hidden=0"
    cur = await conn.execute(
        f"""SELECT id, slug, label_ru, description, message_count, created_at, hidden,
                   parent_id
            FROM topics {where} ORDER BY message_count DESC, label_ru"""
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    # подтянем labels родителей
    if rows:
        pmap = {r["id"]: r["label_ru"] for r in rows}
        for r in rows:
            r["parent_label"] = pmap.get(r["parent_id"]) if r.get("parent_id") else None
    return rows


async def _recount(conn, topic_id: int):
    cur = await conn.execute(
        "SELECT COUNT(*) FROM message_topics WHERE topic_id=?", (topic_id,)
    )
    (n,) = await cur.fetchone()
    await cur.close()
    await conn.execute("UPDATE topics SET message_count=? WHERE id=?", (n, topic_id))
    await conn.commit()


@router.get("/topics")
async def topics_page(request: Request, view: str = "list"):
    conn = request.app.state.db
    rows = await _all_topics(conn, include_hidden=(view == "all"))
    templates = request.app.state.templates
    tree = _build_tree(rows) if view == "tree" else None
    return templates.TemplateResponse(
        request, "topics.html",
        {"topics": rows, "view": view, "tree": tree, "active": "topics"},
    )


def _build_tree(rows: list[dict]):
    by_id = {r["id"]: dict(r, children=[]) for r in rows}
    roots = []
    for r in by_id.values():
        pid = r.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(r)
        else:
            roots.append(r)
    return roots


@router.post("/topics/{topic_id}/rename")
async def rename(request: Request, topic_id: int, label_ru: str = Form(...)):
    conn = request.app.state.db
    await conn.execute("UPDATE topics SET label_ru=? WHERE id=?", (label_ru.strip(), topic_id))
    await conn.commit()
    cur = await conn.execute(
        "SELECT id, slug, label_ru, description, message_count, created_at, hidden, parent_id FROM topics WHERE id=?",
        (topic_id,),
    )
    row = dict(await cur.fetchone())
    await cur.close()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "partials/topic_row.html", {"t": row})


@router.post("/topics/{topic_id}/hide")
async def hide(request: Request, topic_id: int):
    conn = request.app.state.db
    await conn.execute("UPDATE topics SET hidden=1 WHERE id=?", (topic_id,))
    await conn.commit()
    return ""


@router.post("/topics/{topic_id}/unhide")
async def unhide(request: Request, topic_id: int):
    conn = request.app.state.db
    await conn.execute("UPDATE topics SET hidden=0 WHERE id=?", (topic_id,))
    await conn.commit()
    return ""


async def _merge_pair(conn, from_id: int, to_id: int):
    if from_id == to_id:
        return
    await conn.execute(
        """INSERT OR IGNORE INTO message_topics(message_id, topic_id, confidence)
           SELECT message_id, ?, confidence FROM message_topics WHERE topic_id=?""",
        (to_id, from_id),
    )
    await conn.execute("DELETE FROM message_topics WHERE topic_id=?", (from_id,))
    await conn.execute("DELETE FROM topics WHERE id=?", (from_id,))


@router.post("/topics/merge")
async def merge(request: Request, from_id: int = Form(...), to_id: int = Form(...)):
    if from_id == to_id:
        raise HTTPException(400, "from и to совпадают")
    conn = request.app.state.db
    await _merge_pair(conn, from_id, to_id)
    await conn.commit()
    await _recount(conn, to_id)
    rows = await _all_topics(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "partials/topics_table.html", {"topics": rows})


@router.post("/topics/merge-bulk")
async def merge_bulk(request: Request):
    """Слить много тем в одну: form-data from_ids (несколько) + to_id."""
    conn = request.app.state.db
    form = await request.form()
    from_ids = [int(v) for v in form.getlist("from_ids") if str(v).isdigit()]
    to_id_raw = form.get("to_id")
    if not to_id_raw:
        raise HTTPException(400, "to_id обязателен")
    to_id = int(to_id_raw)
    for fid in from_ids:
        if fid != to_id:
            await _merge_pair(conn, fid, to_id)
    await conn.commit()
    await _recount(conn, to_id)
    rows = await _all_topics(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "partials/topics_table.html", {"topics": rows})


@router.post("/topics/{topic_id}/set-parent")
async def set_parent(request: Request, topic_id: int, parent_id: str = Form("")):
    conn = request.app.state.db
    pid_val: int | None
    parent_id = (parent_id or "").strip()
    pid_val = int(parent_id) if parent_id else None
    if pid_val == topic_id:
        raise HTTPException(400, "Тема не может быть родителем самой себя")
    await conn.execute("UPDATE topics SET parent_id=? WHERE id=?", (pid_val, topic_id))
    await conn.commit()
    cur = await conn.execute(
        "SELECT id, slug, label_ru, description, message_count, created_at, hidden, parent_id FROM topics WHERE id=?",
        (topic_id,),
    )
    row = dict(await cur.fetchone())
    await cur.close()
    if row.get("parent_id"):
        cur = await conn.execute("SELECT label_ru FROM topics WHERE id=?", (row["parent_id"],))
        prow = await cur.fetchone()
        await cur.close()
        row["parent_label"] = prow[0] if prow else None
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "partials/topic_row.html", {"t": row})


@router.get("/topics/{topic_id}/messages")
async def topic_messages(request: Request, topic_id: int, offset: int = 0):
    conn = request.app.state.db
    cur = await conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,))
    t = await cur.fetchone()
    await cur.close()
    if not t:
        raise HTTPException(404, "Тема не найдена")
    cur = await conn.execute(
        """SELECT m.id, m.chat_title, m.sender_name, m.text, m.date_utc,
                  p.score, p.urgency, p.importance
           FROM message_topics mt
           JOIN messages m ON m.id=mt.message_id
           LEFT JOIN priorities p ON p.message_id=m.id
           WHERE mt.topic_id=? AND m.is_context_only=0 AND m.deleted_at IS NULL
           ORDER BY m.date_utc DESC LIMIT 50 OFFSET ?""",
        (topic_id, offset),
    )
    msgs = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "topic_messages.html",
        {"topic": dict(t), "messages": msgs, "offset": offset,
         "next_offset": offset + 50 if len(msgs) == 50 else None,
         "prev_offset": max(0, offset - 50) if offset > 0 else None,
         "active": "topics"},
    )


@router.post("/topics/{topic_id}/unassign/{message_id:path}")
async def unassign(request: Request, topic_id: int, message_id: str):
    conn = request.app.state.db
    await conn.execute("DELETE FROM message_topics WHERE topic_id=? AND message_id=?",
                       (topic_id, message_id))
    await conn.commit()
    await _recount(conn, topic_id)
    return ""


@router.post("/topics/{topic_id}/assign-message")
async def assign_message(request: Request, topic_id: int, message_id: str = Form(...)):
    conn = request.app.state.db
    # проверим что сообщение существует
    cur = await conn.execute("SELECT 1 FROM messages WHERE id=?", (message_id,))
    ok = await cur.fetchone()
    await cur.close()
    if not ok:
        raise HTTPException(404, "Сообщение не найдено")
    await conn.execute(
        "INSERT OR IGNORE INTO message_topics(message_id, topic_id, confidence) VALUES (?,?,1.0)",
        (message_id, topic_id),
    )
    await conn.commit()
    await _recount(conn, topic_id)
    return await topic_messages(request, topic_id, offset=0)
