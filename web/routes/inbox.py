"""Инбокс."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


async def _fetch_messages(conn, *, source: str | None, topic: str | None,
                         chat_id: str | None, q: str | None,
                         show_unclassified: bool, limit: int = 100):
    where = ["m.is_context_only = 0", "m.deleted_at IS NULL"]
    params: list = []
    # exclude done/archived/active-snoozed via NOT EXISTS
    where.append(
        "NOT EXISTS (SELECT 1 FROM user_actions ua WHERE ua.message_id=m.id "
        "AND (ua.action IN ('done','archived') OR (ua.action='snoozed' AND ua.snooze_until > datetime('now'))))"
    )

    if source == "dm":
        where.append("m.is_dm = 1")
    elif source == "mention":
        where.append("m.is_mention = 1")
    elif source == "reply":
        where.append("m.is_reply_to_me = 1")

    if chat_id:
        where.append("m.chat_id = ?")
        params.append(int(chat_id))

    if q:
        where.append("m.text LIKE ?")
        params.append(f"%{q}%")

    if topic:
        where.append(
            "EXISTS (SELECT 1 FROM message_topics mt JOIN topics t ON t.id=mt.topic_id "
            "WHERE mt.message_id=m.id AND t.slug=?)"
        )
        params.append(topic)

    if not show_unclassified:
        where.append("p.message_id IS NOT NULL")

    sql = f"""
    SELECT m.id, m.chat_id, m.chat_title, m.sender_name, m.text, m.date_utc,
           m.is_dm, m.is_mention, m.is_reply_to_me,
           COALESCE(p.score, 0) AS score, p.urgency, p.importance,
           (SELECT ua.action FROM user_actions ua WHERE ua.message_id=m.id
              ORDER BY ua.id DESC LIMIT 1) AS last_action,
           (SELECT ua.snooze_until FROM user_actions ua WHERE ua.message_id=m.id
              ORDER BY ua.id DESC LIMIT 1) AS last_snooze_until
    FROM messages m
    LEFT JOIN priorities p ON p.message_id = m.id
    LEFT JOIN chats c ON c.chat_id = m.chat_id
    WHERE COALESCE(c.archived, 0) = 0 AND COALESCE(c.processing,1) = 1 AND {' AND '.join(where)}
    ORDER BY COALESCE(p.score, 0) DESC, m.date_utc DESC
    LIMIT ?
    """
    params.append(limit)
    cur = await conn.execute(sql, params)
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()

    # подтянем темы
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
    return rows


async def _list_chats(conn):
    cur = await conn.execute(
        """SELECT m.chat_id, m.chat_title, COUNT(*) AS n
           FROM messages m
           LEFT JOIN chats c ON c.chat_id = m.chat_id
           WHERE m.is_context_only=0 AND COALESCE(c.archived,0)=0 AND COALESCE(c.processing,1)=1
           GROUP BY m.chat_id, m.chat_title
           ORDER BY n DESC LIMIT 50"""
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


async def _list_topics_top(conn, limit: int = 30):
    cur = await conn.execute(
        """SELECT id, slug, label_ru, message_count FROM topics
           WHERE hidden=0 AND message_count>0
           ORDER BY message_count DESC LIMIT ?""",
        (limit,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


@router.get("/")
async def inbox(request: Request,
                source: str | None = "mention",
                topic: str | None = None,
                chat: str | None = None,
                q: str | None = None,
                show_unclassified: int = 0):
    conn = request.app.state.db
    rows = await _fetch_messages(
        conn,
        source=source, topic=topic, chat_id=chat, q=q,
        show_unclassified=bool(show_unclassified),
    )
    chats = await _list_chats(conn)
    topics = await _list_topics_top(conn)

    templates = request.app.state.templates
    ctx = {
        "request": request, "messages": rows, "chats": chats, "topics": topics,
        "source": source or "", "topic": topic or "",
        "chat": chat or "", "q": q or "",
        "show_unclassified": bool(show_unclassified),
        "active": "inbox",
    }
    # HTMX-фрагмент при запросе хедера HX-Request
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/message_list.html", ctx)
    return templates.TemplateResponse(request, "inbox.html", ctx)
