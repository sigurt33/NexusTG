"""Деталь сообщения + reclassify/priority-edit/save-example."""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request

from app.config import load_config
from app.links import telegram_deep_link

router = APIRouter()


async def _load_detail(conn, message_id: str):
    cur = await conn.execute("SELECT * FROM messages WHERE id=?", (message_id,))
    t = await cur.fetchone()
    await cur.close()
    if not t:
        return None
    target = dict(t)

    cur = await conn.execute(
        """SELECT cl.position, m.* FROM context_links cl
           JOIN messages m ON m.id=cl.context_msg_id
           WHERE cl.message_id=? ORDER BY cl.position""",
        (message_id,),
    )
    ctx_rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    if not any(r["position"] == 0 for r in ctx_rows):
        ctx_rows.append({**target, "position": 0})
        ctx_rows.sort(key=lambda r: r["position"])

    cur = await conn.execute("SELECT * FROM priorities WHERE message_id=?", (message_id,))
    pr = await cur.fetchone(); await cur.close()
    priority = dict(pr) if pr else None

    cur = await conn.execute(
        """SELECT t.slug, t.label_ru, mt.confidence FROM message_topics mt
           JOIN topics t ON t.id=mt.topic_id WHERE mt.message_id=?""",
        (message_id,),
    )
    topics = [dict(r) for r in await cur.fetchall()]; await cur.close()

    cur = await conn.execute("SELECT 1 FROM classification_examples WHERE message_id=?", (message_id,))
    is_example = (await cur.fetchone()) is not None
    await cur.close()

    tg_link = telegram_deep_link(message_id, is_dm=bool(target.get("is_dm"))) or "#"

    return {
        "target": target, "context": ctx_rows,
        "priority": priority, "topics": topics, "tg_link": tg_link,
        "is_example": is_example,
    }


@router.get("/message/{message_id:path}")
async def detail(request: Request, message_id: str):
    conn = request.app.state.db
    data = await _load_detail(conn, message_id)
    if data is None:
        raise HTTPException(404, "Сообщение не найдено")
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "message.html", {**data, "active": ""})


@router.post("/message/{message_id:path}/reclassify")
async def reclassify(request: Request, message_id: str):
    from classifier.grok_worker import reclassify_one, _make_client
    cfg = load_config()
    if not cfg.xai_api_key:
        raise HTTPException(400, "XAI_API_KEY не задан")
    conn = request.app.state.db
    client = _make_client(cfg.xai_api_key, cfg.llm_base_url)
    client._input_per_m = cfg.llm_input_usd_per_m
    client._output_per_m = cfg.llm_output_usd_per_m
    ok = await reclassify_one(conn, cfg, client, message_id)
    if not ok:
        raise HTTPException(500, "Переклассификация не удалась")
    data = await _load_detail(conn, message_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "message.html", {**data, "active": ""})


@router.post("/message/{message_id:path}/priority")
async def edit_priority(request: Request, message_id: str,
                        urgency: int = Form(...), importance: int = Form(...)):
    if not (1 <= urgency <= 5 and 1 <= importance <= 5):
        raise HTTPException(400, "urgency/importance должны быть 1..5")
    conn = request.app.state.db
    score = urgency * 0.6 + importance * 0.4
    cur = await conn.execute("SELECT 1 FROM priorities WHERE message_id=?", (message_id,))
    exists = await cur.fetchone(); await cur.close()
    if exists:
        await conn.execute(
            "UPDATE priorities SET urgency=?, importance=?, score=?, classified_at=datetime('now') WHERE message_id=?",
            (urgency, importance, score, message_id),
        )
    else:
        await conn.execute(
            "INSERT INTO priorities(message_id, urgency, importance, score, rationale, model_version) "
            "VALUES (?,?,?,?,?,?)",
            (message_id, urgency, importance, score, "manual", "manual"),
        )
    await conn.commit()
    data = await _load_detail(conn, message_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "partials/message_priority.html", data)


@router.post("/message/{message_id:path}/save-example")
async def save_example(request: Request, message_id: str, note: str = Form("")):
    conn = request.app.state.db
    cur = await conn.execute("SELECT urgency, importance FROM priorities WHERE message_id=?", (message_id,))
    pr = await cur.fetchone(); await cur.close()
    if not pr:
        raise HTTPException(400, "Сначала классифицируйте сообщение")
    cur = await conn.execute(
        """SELECT t.slug, t.label_ru FROM message_topics mt
           JOIN topics t ON t.id=mt.topic_id WHERE mt.message_id=?""",
        (message_id,),
    )
    topics = [{"slug": r[0], "label_ru": r[1]} for r in await cur.fetchall()]
    await cur.close()
    await conn.execute(
        """INSERT OR REPLACE INTO classification_examples
           (message_id, urgency, importance, topics_json, note, created_at)
           VALUES (?,?,?,?,?,datetime('now'))""",
        (message_id, int(pr[0]), int(pr[1]), json.dumps(topics, ensure_ascii=False), note.strip()),
    )
    await conn.commit()
    data = await _load_detail(conn, message_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "message.html", {**data, "active": ""})


@router.get("/examples")
async def examples_page(request: Request):
    conn = request.app.state.db
    cur = await conn.execute(
        """SELECT e.message_id, e.urgency, e.importance, e.topics_json, e.note, e.created_at,
                  m.chat_title, m.sender_name, m.text
           FROM classification_examples e
           LEFT JOIN messages m ON m.id=e.message_id
           ORDER BY e.created_at DESC"""
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    for r in rows:
        try:
            r["topics_parsed"] = json.loads(r["topics_json"] or "[]")
        except Exception:
            r["topics_parsed"] = []
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "examples.html",
        {"rows": rows, "active": "examples"},
    )


@router.post("/examples/{message_id:path}/delete")
async def delete_example(request: Request, message_id: str):
    conn = request.app.state.db
    await conn.execute("DELETE FROM classification_examples WHERE message_id=?", (message_id,))
    await conn.commit()
    return ""


@router.post("/examples/{message_id:path}/note")
async def edit_note(request: Request, message_id: str, note: str = Form("")):
    conn = request.app.state.db
    await conn.execute("UPDATE classification_examples SET note=? WHERE message_id=?",
                       (note.strip(), message_id))
    await conn.commit()
    return note
