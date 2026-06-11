"""Health-страница."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    conn = request.app.state.db

    async def scalar(sql, params=()):
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    total_messages = await scalar("SELECT COUNT(*) FROM messages")
    captured = await scalar("SELECT COUNT(*) FROM messages WHERE is_context_only=0")
    classified = await scalar("SELECT COUNT(*) FROM priorities")
    last_classified = await scalar("SELECT MAX(classified_at) FROM priorities")
    topics_n = await scalar("SELECT COUNT(*) FROM topics WHERE hidden=0")

    cur = await conn.execute(
        "SELECT prompt_tokens, completion_tokens, cost_usd FROM grok_usage WHERE date = date('now')"
    )
    u = await cur.fetchone()
    await cur.close()
    usage = dict(u) if u else {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

    cur = await conn.execute(
        """SELECT COUNT(*) AS chats,
                  SUM(CASE WHEN backfill_done=1 THEN 1 ELSE 0 END) AS done
           FROM ingest_state"""
    )
    bf = dict(await cur.fetchone())
    await cur.close()

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "health.html",
        {
            "total_messages": total_messages or 0,
            "captured": captured or 0,
            "classified": classified or 0,
            "last_classified": last_classified or "",
            "topics_n": topics_n or 0,
            "usage": usage,
            "backfill": bf,
            "active": "health",
        },
    )
