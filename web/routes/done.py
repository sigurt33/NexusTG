"""Страница «Выполненные» — все сообщения, помеченные ✓ Готово."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/done")
async def done_page(request: Request, limit: int = 200):
    conn = request.app.state.db
    sql = """
    SELECT m.id, m.chat_id, m.chat_title, m.sender_name, m.text, m.date_utc,
           m.is_dm, m.is_mention, m.is_reply_to_me,
           COALESCE(p.score, 0) AS score, p.urgency, p.importance,
           ua.created_at AS done_at
    FROM messages m
    JOIN (
        SELECT message_id, MAX(id) AS last_id
        FROM user_actions GROUP BY message_id
    ) lu ON lu.message_id = m.id
    JOIN user_actions ua ON ua.id = lu.last_id
    LEFT JOIN priorities p ON p.message_id = m.id
    WHERE ua.action = 'done'
    ORDER BY ua.created_at DESC
    LIMIT ?
    """
    cur = await conn.execute(sql, (limit,))
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()

    if rows:
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        cur = await conn.execute(
            f"""SELECT mt.message_id, t.slug, t.label_ru FROM message_topics mt
                JOIN topics t ON t.id=mt.topic_id
                WHERE mt.message_id IN ({placeholders}) AND t.hidden=0""",
            ids,
        )
        topics_map: dict[str, list[dict]] = {}
        for r in await cur.fetchall():
            topics_map.setdefault(r["message_id"], []).append(
                {"slug": r["slug"], "label_ru": r["label_ru"]}
            )
        await cur.close()
        for r in rows:
            r["topics"] = topics_map.get(r["id"], [])

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "done.html",
        {"messages": rows, "active": "done", "limit": limit},
    )
