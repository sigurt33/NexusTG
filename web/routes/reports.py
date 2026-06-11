"""Reports & dashboards: weekly / by-topic / by-chat / by-sender + CSV export."""
from __future__ import annotations

import csv
import io
from typing import Iterable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/reports")
async def reports_index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "reports.html", {"active": "reports"})


async def _rows(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


@router.get("/reports/weekly")
async def weekly(request: Request):
    conn = request.app.state.db
    days = await _rows(conn, """
        SELECT substr(date_utc,1,10) AS d, COUNT(*) AS n
        FROM messages
        WHERE is_context_only=0 AND date_utc >= date('now','-7 days')
        GROUP BY d ORDER BY d
    """)
    top_topics = await _rows(conn, """
        SELECT t.label_ru, COUNT(*) AS n FROM message_topics mt
        JOIN topics t ON t.id=mt.topic_id
        JOIN messages m ON m.id=mt.message_id
        WHERE m.date_utc >= date('now','-7 days') AND m.is_context_only=0
        GROUP BY t.id ORDER BY n DESC LIMIT 10
    """)
    top_chats = await _rows(conn, """
        SELECT chat_title, COUNT(*) AS n FROM messages
        WHERE is_context_only=0 AND date_utc >= date('now','-7 days')
        GROUP BY chat_id, chat_title ORDER BY n DESC LIMIT 5
    """)
    top_senders = await _rows(conn, """
        SELECT sender_name, COUNT(*) AS n FROM messages
        WHERE is_context_only=0 AND date_utc >= date('now','-7 days') AND sender_name IS NOT NULL
        GROUP BY sender_name ORDER BY n DESC LIMIT 5
    """)
    max_day = max((d["n"] for d in days), default=1) or 1
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "reports_weekly.html",
        {"days": days, "max_day": max_day,
         "top_topics": top_topics, "top_chats": top_chats, "top_senders": top_senders,
         "active": "reports"},
    )


@router.get("/reports/topics")
async def report_topics(request: Request, sort: str = "count_total"):
    conn = request.app.state.db
    valid = {"count_total", "count_7d", "count_30d", "avg_score", "last_seen", "label_ru"}
    if sort not in valid:
        sort = "count_total"
    rows = await _rows(conn, f"""
        SELECT t.id, t.label_ru, t.slug, t.message_count AS count_total,
               (SELECT COUNT(*) FROM message_topics mt JOIN messages m ON m.id=mt.message_id
                 WHERE mt.topic_id=t.id AND m.date_utc >= date('now','-7 days')) AS count_7d,
               (SELECT COUNT(*) FROM message_topics mt JOIN messages m ON m.id=mt.message_id
                 WHERE mt.topic_id=t.id AND m.date_utc >= date('now','-30 days')) AS count_30d,
               (SELECT AVG(p.score) FROM message_topics mt JOIN priorities p ON p.message_id=mt.message_id
                 WHERE mt.topic_id=t.id) AS avg_score,
               (SELECT MAX(m.date_utc) FROM message_topics mt JOIN messages m ON m.id=mt.message_id
                 WHERE mt.topic_id=t.id) AS last_seen
        FROM topics t WHERE t.hidden=0
        ORDER BY {sort} DESC NULLS LAST
        LIMIT 500
    """)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "reports_topics.html",
        {"rows": rows, "sort": sort, "active": "reports"},
    )


@router.get("/reports/chats")
async def report_chats(request: Request):
    conn = request.app.state.db
    rows = await _rows(conn, """
        SELECT c.chat_id, c.title,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id AND m.is_context_only=0) AS captured,
               (SELECT COUNT(*) FROM messages m JOIN priorities p ON p.message_id=m.id
                 WHERE m.chat_id=c.chat_id AND m.is_context_only=0) AS classified,
               (SELECT AVG(p.score) FROM messages m JOIN priorities p ON p.message_id=m.id
                 WHERE m.chat_id=c.chat_id) AS avg_score,
               (SELECT MAX(date_utc) FROM messages m WHERE m.chat_id=c.chat_id) AS last_msg
        FROM chats c ORDER BY captured DESC LIMIT 200
    """)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "reports_chats.html",
        {"rows": rows, "active": "reports"},
    )


@router.get("/reports/senders")
async def report_senders(request: Request):
    conn = request.app.state.db
    rows = await _rows(conn, """
        SELECT sender_name, COUNT(*) AS captured,
               AVG(COALESCE(p.score,0)) AS avg_score,
               MAX(m.date_utc) AS last_msg
        FROM messages m LEFT JOIN priorities p ON p.message_id=m.id
        WHERE m.is_context_only=0 AND m.sender_name IS NOT NULL
        GROUP BY sender_name ORDER BY captured DESC LIMIT 200
    """)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "reports_senders.html",
        {"rows": rows, "active": "reports"},
    )


# --- CSV export ---

def _csv_iter(rows: Iterable[dict], fieldnames: list[str]):
    # UTF-8 BOM для Excel
    yield "﻿"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter=";",
                            quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writeheader()
    yield buf.getvalue()
    for row in rows:
        buf.seek(0); buf.truncate(0)
        writer.writerow({k: ("" if row.get(k) is None else str(row.get(k))) for k in fieldnames})
        yield buf.getvalue()


@router.get("/export/messages.csv")
async def export_messages(request: Request,
                          **kwargs):
    conn = request.app.state.db
    qp = request.query_params
    where = ["m.is_context_only=0", "m.deleted_at IS NULL"]
    params: list = []
    if qp.get("from"):
        where.append("m.date_utc >= ?"); params.append(qp["from"])
    if qp.get("to"):
        where.append("m.date_utc <= ?"); params.append(qp["to"])
    if qp.get("chat"):
        where.append("m.chat_id = ?"); params.append(int(qp["chat"]))
    if qp.get("min_score"):
        where.append("COALESCE(p.score,0) >= ?"); params.append(float(qp["min_score"]))
    topic_join = ""
    if qp.get("topic"):
        topic_join = "JOIN message_topics mt ON mt.message_id=m.id JOIN topics t ON t.id=mt.topic_id"
        where.append("t.slug = ?"); params.append(qp["topic"])
    sql = f"""
        SELECT m.id, m.chat_id, m.chat_title, m.sender_name, m.text, m.date_utc,
               m.is_dm, m.is_mention, m.is_reply_to_me,
               p.urgency, p.importance, p.score, p.rationale,
               (SELECT GROUP_CONCAT(t2.label_ru, ', ') FROM message_topics mt2
                  JOIN topics t2 ON t2.id=mt2.topic_id WHERE mt2.message_id=m.id) AS topics
        FROM messages m
        LEFT JOIN priorities p ON p.message_id=m.id
        {topic_join}
        WHERE {' AND '.join(where)}
        ORDER BY m.date_utc DESC LIMIT 50000
    """
    rows = await _rows(conn, sql, params)
    fields = ["id","chat_id","chat_title","sender_name","date_utc","is_dm","is_mention",
              "is_reply_to_me","urgency","importance","score","topics","rationale","text"]
    return StreamingResponse(
        _csv_iter(rows, fields),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="messages.csv"'},
    )


@router.get("/export/topics.csv")
async def export_topics(request: Request):
    conn = request.app.state.db
    rows = await _rows(conn, """
        SELECT id, slug, label_ru, description, message_count, hidden, parent_id, created_at
        FROM topics ORDER BY message_count DESC
    """)
    fields = ["id","slug","label_ru","description","message_count","hidden","parent_id","created_at"]
    return StreamingResponse(
        _csv_iter(rows, fields),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="topics.csv"'},
    )
