"""Дайджест за вчера."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/digest")
async def digest(request: Request):
    conn = request.app.state.db
    # вчерашняя дата в Europe/Minsk (UTC+3)
    now_local = datetime.now(timezone.utc) + timedelta(hours=3)
    yesterday = (now_local - timedelta(days=1)).date().isoformat()

    cur = await conn.execute(
        """SELECT m.id, m.chat_title, m.sender_name, m.text, m.date_utc,
                  m.is_dm, m.is_mention, m.is_reply_to_me,
                  p.score, p.urgency, p.importance, p.rationale
           FROM priorities p
           JOIN messages m ON m.id = p.message_id
           WHERE date(p.classified_at, '+3 hours') = ?
             AND m.is_context_only = 0 AND m.deleted_at IS NULL
           ORDER BY p.score DESC
           LIMIT 20""",
        (yesterday,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()

    if rows:
        ids = [r["id"] for r in rows]
        ph = ",".join("?" for _ in ids)
        cur = await conn.execute(
            f"""SELECT mt.message_id, t.label_ru, t.slug FROM message_topics mt
                JOIN topics t ON t.id=mt.topic_id
                WHERE mt.message_id IN ({ph})""",
            ids,
        )
        tmap: dict[str, list[dict]] = {}
        for r in await cur.fetchall():
            tmap.setdefault(r["message_id"], []).append(
                {"label_ru": r["label_ru"], "slug": r["slug"]}
            )
        await cur.close()
        for r in rows:
            r["topics"] = tmap.get(r["id"], [])

    # группировка по первой теме
    groups: dict[str, list[dict]] = {}
    for r in rows:
        primary = r.get("topics", [{}])[0].get("label_ru") if r.get("topics") else "Без темы"
        groups.setdefault(primary, []).append(r)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "digest.html",
        {"date": yesterday, "groups": groups,
         "total": len(rows), "active": "digest"},
    )
