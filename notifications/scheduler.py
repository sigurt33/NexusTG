"""Notification scheduler: snooze sweeper + new-task watcher + active-hours opener."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from notifications.toast import notify_toast
from notifications.tg_self import notify_tg

log = logging.getLogger(__name__)

WEB_BASE = "http://127.0.0.1:8000"


def _parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _now_local(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def _in_active_hours(cfg) -> bool:
    tz = ZoneInfo(cfg.timezone)
    now = _now_local(tz).time()
    start = _parse_hm(cfg.active_hours_start)
    end = _parse_hm(cfg.active_hours_end)
    if start <= end:
        return start <= now <= end
    # cross midnight
    return now >= start or now <= end


async def _format_for_notify(conn, message_id: str) -> tuple[str, str, str] | None:
    cur = await conn.execute(
        """SELECT m.id, m.chat_title, m.sender_name, m.text, p.score, p.urgency, p.importance
           FROM messages m LEFT JOIN priorities p ON p.message_id=m.id
           WHERE m.id=?""",
        (message_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    title = f"{row['chat_title'] or '—'} · {row['sender_name'] or '—'}"
    text = (row["text"] or "").replace("\n", " ").strip()
    if len(text) > 200:
        text = text[:200] + "…"
    score = row["score"] or 0
    body = f"[{score:.1f}] {text}"
    url = f"{WEB_BASE}/message/{row['id']}"
    return title, body, url


async def _fire_notifications(client, conn, cfg, message_id: str) -> None:
    payload = await _format_for_notify(conn, message_id)
    if not payload:
        return
    title, body, url = payload
    if cfg.notify_windows_toast:
        try:
            await notify_toast(title, body, url)
        except Exception as e:
            log.warning("toast упал: %s", e)
    if cfg.notify_tg_self and client is not None:
        try:
            await notify_tg(client, title, body, url)
        except Exception as e:
            log.warning("tg self упал: %s", e)
    # bot route — отдельный путь: бот сам поллит таблицы, здесь только лог.
    if getattr(cfg, "notify_tg_bot", False) and getattr(cfg, "tg_bot_token", ""):
        log.debug("notify_tg_bot включён — уведомление пойдёт через bot watcher")


async def _snooze_sweeper(client, conn, cfg) -> None:
    """Every 60s wake snoozed messages whose snooze_until has passed."""
    while True:
        try:
            # find newly-woken: latest action='snoozed' with snooze_until <= now
            cur = await conn.execute(
                """SELECT ua.id, ua.message_id FROM user_actions ua
                   WHERE ua.action='snoozed' AND ua.snooze_until <= datetime('now')
                   AND ua.id = (SELECT MAX(id) FROM user_actions WHERE message_id=ua.message_id)"""
            )
            woken = [(r["id"], r["message_id"]) for r in await cur.fetchall()]
            await cur.close()
            for _id, mid in woken:
                await conn.execute(
                    "UPDATE user_actions SET action='unread' WHERE id=?", (_id,)
                )
            if woken:
                await conn.commit()
                log.info("snooze sweeper: разбужено %d сообщений", len(woken))
                if _in_active_hours(cfg):
                    for _id, mid in woken:
                        await _fire_notifications(client, conn, cfg, mid)
                else:
                    for _id, mid in woken:
                        await conn.execute(
                            "INSERT OR IGNORE INTO pending_notifications(message_id) VALUES (?)",
                            (mid,),
                        )
                    await conn.commit()
        except Exception as e:
            log.warning("snooze sweeper упал: %s", e)
        await asyncio.sleep(60)


async def _new_task_watcher(client, conn, cfg) -> None:
    """Every 30s find newly classified high-score messages."""
    # track which message_ids we've already notified/queued in-process to dedupe
    seen: set[str] = set()
    while True:
        try:
            cur = await conn.execute(
                """SELECT p.message_id FROM priorities p
                   JOIN messages m ON m.id = p.message_id
                   LEFT JOIN chats c ON c.chat_id = m.chat_id
                   WHERE p.score >= 3.0
                     AND m.is_context_only = 0
                     AND m.deleted_at IS NULL
                     AND COALESCE(c.archived,0) = 0
                     AND COALESCE(c.processing,1) = 1
                     AND p.classified_at >= datetime('now', '-90 seconds')
                     AND NOT EXISTS (
                         SELECT 1 FROM user_actions ua WHERE ua.message_id=p.message_id
                          AND ua.action IN ('done','archived','snoozed')
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM pending_notifications pn WHERE pn.message_id=p.message_id
                     )"""
            )
            ids = [r["message_id"] for r in await cur.fetchall()]
            await cur.close()
            ids = [i for i in ids if i not in seen]
            if ids:
                if _in_active_hours(cfg):
                    for mid in ids:
                        await _fire_notifications(client, conn, cfg, mid)
                        seen.add(mid)
                else:
                    for mid in ids:
                        await conn.execute(
                            "INSERT OR IGNORE INTO pending_notifications(message_id) VALUES (?)",
                            (mid,),
                        )
                        seen.add(mid)
                    await conn.commit()
                    log.info("вне активных часов: в очередь добавлено %d", len(ids))
            # cap in-memory dedupe set
            if len(seen) > 5000:
                seen.clear()
        except Exception as e:
            log.warning("new task watcher упал: %s", e)
        await asyncio.sleep(30)


async def _active_hours_opener(client, conn, cfg) -> None:
    """At active_hours_start: drain pending and send one summary."""
    tz = ZoneInfo(cfg.timezone)
    last_fired_date: str | None = None
    while True:
        try:
            now_local = _now_local(tz)
            start = _parse_hm(cfg.active_hours_start)
            today_key = now_local.strftime("%Y-%m-%d")
            # within first 2 minutes of start, fire once per day
            start_dt = now_local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            if (start_dt <= now_local <= start_dt + timedelta(minutes=2)
                    and last_fired_date != today_key):
                cur = await conn.execute(
                    """SELECT pn.message_id, m.chat_title, m.sender_name, p.score
                       FROM pending_notifications pn
                       JOIN messages m ON m.id = pn.message_id
                       LEFT JOIN priorities p ON p.message_id = pn.message_id
                       ORDER BY COALESCE(p.score,0) DESC LIMIT 100"""
                )
                rows = [dict(r) for r in await cur.fetchall()]
                await cur.close()
                if rows:
                    n = len(rows)
                    top_titles = []
                    for r in rows[:5]:
                        t = r["chat_title"] or "—"
                        s = r["sender_name"] or "—"
                        top_titles.append(f"{t}/{s}")
                    body = "Топ: " + "; ".join(top_titles)
                    title = f"{n} новых задач за нерабочее время"
                    url = f"{WEB_BASE}/"
                    if cfg.notify_windows_toast:
                        await notify_toast(title, body, url)
                    if cfg.notify_tg_self and client is not None:
                        await notify_tg(client, title, body, url)
                    await conn.execute("DELETE FROM pending_notifications")
                    await conn.commit()
                last_fired_date = today_key
        except Exception as e:
            log.warning("active hours opener упал: %s", e)
        await asyncio.sleep(30)


async def run_notifications(client, conn, cfg) -> None:
    """Entry point: run all scheduler loops concurrently."""
    log.info(
        "Notifications scheduler: active_hours=%s-%s tz=%s toast=%s tg_self=%s",
        cfg.active_hours_start, cfg.active_hours_end, cfg.timezone,
        cfg.notify_windows_toast, cfg.notify_tg_self,
    )
    tasks = [
        asyncio.create_task(_snooze_sweeper(client, conn, cfg), name="snooze_sweeper"),
        asyncio.create_task(_new_task_watcher(client, conn, cfg), name="new_task_watcher"),
        asyncio.create_task(_active_hours_opener(client, conn, cfg), name="active_hours_opener"),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        raise
