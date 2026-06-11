"""Действия пользователя: done / snooze / archive / unread."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request, Response

router = APIRouter()

def _tz() -> ZoneInfo:
    try:
        return ZoneInfo("Europe/Minsk")
    except Exception:
        from datetime import timezone as _tzm
        return _tzm(timedelta(hours=3))  # type: ignore


def _to_utc_iso(local_dt: datetime) -> str:
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=_tz())
    try:
        utc = ZoneInfo("UTC")
    except Exception:
        from datetime import timezone as _tzm
        utc = _tzm(timedelta(0))  # type: ignore
    return local_dt.astimezone(utc).strftime("%Y-%m-%d %H:%M:%S")


def _compute_snooze_until(preset: str, custom_until: str | None) -> str:
    now_local = datetime.now(_tz())
    if preset == "1h":
        return _to_utc_iso(now_local + timedelta(hours=1))
    if preset == "tomorrow10":
        tgt = (now_local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        return _to_utc_iso(tgt)
    if preset == "monday10":
        # next Monday 10:00 (if today is Mon and before 10:00 — today)
        days = (7 - now_local.weekday()) % 7  # Mon=0
        if days == 0 and now_local.time() >= time(10, 0):
            days = 7
        tgt = (now_local + timedelta(days=days)).replace(hour=10, minute=0, second=0, microsecond=0)
        return _to_utc_iso(tgt)
    if preset == "custom":
        if not custom_until:
            raise HTTPException(400, "custom_until обязателен для preset=custom")
        try:
            dt = datetime.fromisoformat(custom_until)
        except ValueError:
            raise HTTPException(400, "Неверный формат custom_until")
        return _to_utc_iso(dt)
    raise HTTPException(400, f"Неизвестный preset: {preset}")


async def _row_state(conn, message_id: str) -> str:
    cur = await conn.execute(
        """SELECT action, snooze_until FROM user_actions
           WHERE message_id=? ORDER BY id DESC LIMIT 1""",
        (message_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return "active"
    if row["action"] == "snoozed" and row["snooze_until"] and row["snooze_until"] > datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"):
        return "snoozed"
    if row["action"] in ("done", "archived"):
        return row["action"]
    return "active"


async def _insert_action(conn, message_id: str, action: str, snooze_until: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO user_actions(message_id, action, snooze_until) VALUES (?,?,?)",
        (message_id, action, snooze_until),
    )
    # remove from pending if any
    await conn.execute(
        "DELETE FROM pending_notifications WHERE message_id=?", (message_id,)
    )
    await conn.commit()


def _drop_response() -> Response:
    # пустой ответ + статус 200 — htmx удалит элемент через hx-swap="outerHTML" с пустотой
    return Response(status_code=200, content="")


@router.post("/actions/{message_id:path}/done")
async def action_done(request: Request, message_id: str):
    conn = request.app.state.db
    await _insert_action(conn, message_id, "done")
    return _drop_response()


@router.post("/actions/{message_id:path}/snooze")
async def action_snooze(
    request: Request,
    message_id: str,
    preset: str = Form(...),
    custom_until: str | None = Form(default=None),
):
    conn = request.app.state.db
    until = _compute_snooze_until(preset, custom_until)
    await _insert_action(conn, message_id, "snoozed", until)
    return _drop_response()


@router.post("/actions/{message_id:path}/archive")
async def action_archive(request: Request, message_id: str):
    conn = request.app.state.db
    await _insert_action(conn, message_id, "archived")
    return _drop_response()


@router.post("/actions/{message_id:path}/unread")
async def action_unread(request: Request, message_id: str):
    conn = request.app.state.db
    await _insert_action(conn, message_id, "unread")
    return _drop_response()
