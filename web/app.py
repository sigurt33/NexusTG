"""FastAPI app factory для веб-дашборда."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import connect, init_db

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


def _parse_utc(s: str) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _relative_ru(dt_str: str) -> str:
    if not dt_str:
        return ""
    dt = _parse_utc(dt_str)
    now = datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    # local for "вчера в HH:MM" — используем минское смещение +3
    local = dt + timedelta(hours=3)
    if secs < 60:
        return "только что"
    if secs < 3600:
        m = secs // 60
        return f"{m} мин назад"
    if secs < 86400:
        h = secs // 3600
        return f"{h} ч назад"
    if secs < 86400 * 2:
        return f"вчера в {local.strftime('%H:%M')}"
    if secs < 86400 * 7:
        d = secs // 86400
        return f"{d} дн назад"
    return local.strftime("%d.%m %H:%M")


def _local_dt(dt_str: str) -> str:
    if not dt_str:
        return ""
    dt = _parse_utc(dt_str) + timedelta(hours=3)
    return dt.strftime("%d.%m.%Y %H:%M")


def _score_class(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "low"
    if s >= 4.0:
        return "high"
    if s >= 3.0:
        return "mid"
    return "low"


def _snippet(text: str, n: int = 200) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db()
        conn = await connect()
        app.state.db = conn
        try:
            yield
        finally:
            await conn.close()

    app = FastAPI(title="NexusTG", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["rel_ru"] = _relative_ru
    templates.env.filters["local_dt"] = _local_dt
    templates.env.filters["score_class"] = _score_class
    templates.env.filters["snippet"] = _snippet
    # глобал «me» — мини-профиль пользователя для шапки
    from app.me import load_cached_me
    templates.env.globals["me"] = load_cached_me() or {}
    app.state.templates = templates

    from web.routes import inbox, message, search, topics, digest, health, actions, chats, reports, rules, done, templates_route
    app.include_router(done.router)
    app.include_router(templates_route.router)
    app.include_router(inbox.router)
    app.include_router(message.router)
    app.include_router(search.router)
    app.include_router(topics.router)
    app.include_router(digest.router)
    app.include_router(health.router)
    app.include_router(actions.router)
    app.include_router(chats.router)
    app.include_router(reports.router)
    app.include_router(rules.router)

    return app
