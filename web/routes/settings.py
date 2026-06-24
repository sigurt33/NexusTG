"""Вкладка «Настройки»: редактирование безопасных полей config.toml."""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Form, Request, Response

from app.settings_io import read_raw, write_config_values

router = APIRouter()

_HM = re.compile(r"^\d{1,2}:\d{2}$")


@router.get("/settings")
async def settings_page(request: Request):
    cfg = read_raw()
    secrets = {
        "TG_BOT_TOKEN": bool(os.getenv("TG_BOT_TOKEN")),
        "TG_API_ID": bool(os.getenv("TG_API_ID")),
        "TG_API_HASH": bool(os.getenv("TG_API_HASH")),
        "XAI_API_KEY": bool(os.getenv("XAI_API_KEY")),
        "TG_MY_ID": bool(os.getenv("TG_MY_ID")),
    }
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "settings.html", {"cfg": cfg, "secrets": secrets, "active": "settings"}
    )


@router.post("/settings")
async def settings_save(
    request: Request,
    task_reminder_hours_before: int = Form(3),
    active_hours_start: str = Form(""),
    active_hours_end: str = Form(""),
    grok_daily_token_budget: int = Form(0),
    grok_model: str = Form(""),
    notify_windows_toast: str | None = Form(None),
    notify_tg_self: str | None = Form(None),
    notify_tg_bot: str | None = Form(None),
):
    cur = read_raw()
    start = active_hours_start.strip()
    end = active_hours_end.strip()
    updates = {
        "task_reminder_hours_before": max(0, min(168, int(task_reminder_hours_before))),
        "active_hours_start": start if _HM.match(start) else cur.get("active_hours_start", "10:00"),
        "active_hours_end": end if _HM.match(end) else cur.get("active_hours_end", "18:30"),
        "grok_daily_token_budget": max(0, int(grok_daily_token_budget)),
        "grok_model": grok_model.strip() or cur.get("grok_model", ""),
        "notify_windows_toast": notify_windows_toast is not None,
        "notify_tg_self": notify_tg_self is not None,
        "notify_tg_bot": notify_tg_bot is not None,
    }
    write_config_values(updates)
    resp = Response(status_code=204)
    resp.headers["HX-Redirect"] = "/settings"
    return resp
