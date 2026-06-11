"""FTS5-поиск."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


def _sanitize_fts_query(q: str) -> str:
    # простая защита: обрезаем спецсимволы FTS, оставляем слова
    safe = []
    for tok in q.split():
        tok = "".join(ch for ch in tok if ch.isalnum() or ch in "_-")
        if tok:
            safe.append(tok + "*")
    return " ".join(safe)


@router.get("/search")
async def search(request: Request, q: str = ""):
    conn = request.app.state.db
    templates = request.app.state.templates
    rows: list[dict] = []
    if q.strip():
        fts_q = _sanitize_fts_query(q)
        if fts_q:
            try:
                cur = await conn.execute(
                    """
                    SELECT m.id, m.chat_id, m.chat_title, m.sender_name, m.date_utc,
                           m.is_dm, m.is_mention, m.is_reply_to_me,
                           snippet(messages_fts, 0, '<mark>', '</mark>', '…', 16) AS snip,
                           COALESCE(p.score, 0) AS score
                    FROM messages_fts
                    JOIN messages m ON m.rowid = messages_fts.rowid
                    LEFT JOIN priorities p ON p.message_id = m.id
                    WHERE messages_fts MATCH ?
                      AND m.is_context_only = 0
                    ORDER BY bm25(messages_fts)
                    LIMIT 100
                    """,
                    (fts_q,),
                )
                rows = [dict(r) for r in await cur.fetchall()]
                await cur.close()
            except Exception as e:
                rows = []
                request.app.state.last_search_error = str(e)

    ctx = {"request": request, "results": rows, "q": q, "active": "search"}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/search_results.html", ctx)
    return templates.TemplateResponse(request, "search.html", ctx)
