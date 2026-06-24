"""Telegram-бот: inbox-уведомления и команды /start /inbox /digest /help."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession

from app.config import DATA_DIR
from app.links import telegram_deep_link
from app.settings_io import live_reminder_hours
from app.tasks import create_task_from_message

log = logging.getLogger(__name__)

WEB_BASE = "http://127.0.0.1:8000"
POLL_INTERVAL = 30
BOT_SESSION_PATH = DATA_DIR / "tg_bot.session"


def _make_bot_client() -> TelegramClient:
    # отдельная файловая сессия для бота — чтобы не конфликтовать с user-сессией
    return TelegramClient(str(BOT_SESSION_PATH.with_suffix("")), 0, "")


def _tg_deep_link(message_id: str, is_dm: bool = False) -> str:
    """Ссылка на сообщение в Telegram. Fallback — открыть в дашборде."""
    link = telegram_deep_link(message_id, is_dm=is_dm)
    return link or f"{WEB_BASE}/message/{message_id}"


async def _format_payload(conn, message_id: str) -> tuple[str, list] | None:
    cur = await conn.execute(
        """SELECT m.id, m.chat_title, m.sender_name, m.text, m.is_dm, m.is_mention, m.is_reply_to_me,
                  p.urgency, p.importance, p.score
           FROM messages m LEFT JOIN priorities p ON p.message_id=m.id
           WHERE m.id=?""",
        (message_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    src = "ЛС" if row["is_dm"] else ("@упоминание" if row["is_mention"] else ("↩ ответ" if row["is_reply_to_me"] else "?"))
    text = (row["text"] or "").strip().replace("\n", " ")
    if len(text) > 300:
        text = text[:300] + "…"
    # темы
    cur = await conn.execute(
        """SELECT t.label_ru FROM message_topics mt JOIN topics t ON t.id=mt.topic_id
           WHERE mt.message_id=?""",
        (message_id,),
    )
    topics = ", ".join([r[0] for r in await cur.fetchall()]) or "—"
    await cur.close()
    body = (
        f"🔔 [{src}] {row['chat_title'] or '—'}\n"
        f"{row['sender_name'] or '—'}: {text}\n"
        f"Темы: {topics}\n"
        f"❗ срочн={row['urgency']} важн={row['importance']} score={(row['score'] or 0):.1f}"
    )
    buttons = [
        [Button.inline("✅ Готово", f"done:{message_id}".encode()),
         Button.inline("💤 1 ч", f"snooze:{message_id}:1h".encode())],
        [Button.inline("🌅 Завтра 10:00", f"snooze:{message_id}:tomorrow10".encode()),
         Button.inline("📦 Архив", f"archive:{message_id}".encode())],
        [Button.inline("📋 В задачник", f"task:{message_id}".encode())],
        [Button.url("🌐 Открыть в дашборде", f"{WEB_BASE}/message/{message_id}"),
         Button.url("💬 К диалогу в Telegram", _tg_deep_link(message_id, is_dm=bool(row["is_dm"])))],
    ]
    return body, buttons


async def _record_action(conn, message_id: str, action: str, snooze_until: str | None = None):
    await conn.execute(
        "INSERT INTO user_actions(message_id, action, snooze_until) VALUES (?,?,?)",
        (message_id, action, snooze_until),
    )
    await conn.execute("DELETE FROM pending_notifications WHERE message_id=?", (message_id,))
    await conn.commit()


def _snooze_until(preset: str) -> str:
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo("Europe/Minsk")
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    if preset == "1h":
        tgt = now + timedelta(hours=1)
    elif preset == "tomorrow10":
        tgt = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    else:
        tgt = now + timedelta(hours=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_due_local(due: str, tz) -> datetime | None:
    """due_at — наивное локальное ('YYYY-MM-DDTHH:MM' или с пробелом/сек) → aware UTC."""
    s = (due or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(s, fmt)
            return naive.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


async def _send_deadline(bot: TelegramClient, cfg, tid: int, title: str, due_str: str, overdue: bool) -> bool:
    if not cfg.tg_my_id:
        return False
    head = "🔴 Дедлайн наступил/просрочен" if overdue else "⏰ Скоро дедлайн"
    body = f"{head}\n#{tid} {title}\nСрок: {due_str}"
    buttons = [
        [Button.inline("✅ Готово", f"tdone:{tid}".encode())],
        [Button.url("🌐 Открыть задачу", f"{WEB_BASE}/tasks#task-{tid}")],
    ]
    try:
        await bot.send_message(cfg.tg_my_id, body, buttons=buttons, link_preview=False)
        return True
    except Exception as e:
        log.warning("deadline send failed: %s", e)
        return False


async def send_inbox_notification(bot: TelegramClient, conn, cfg, message_id: str) -> bool:
    if not cfg.tg_my_id:
        return False
    payload = await _format_payload(conn, message_id)
    if not payload:
        return False
    body, buttons = payload
    try:
        await bot.send_message(cfg.tg_my_id, body, buttons=buttons, link_preview=False)
        return True
    except Exception as e:
        log.warning("bot send failed: %s", e)
        return False


async def _watcher_loop(bot: TelegramClient, conn, cfg) -> None:
    """Поллим новые классифицированные task'и с score>=3, шлём в бот."""
    seen: set[str] = set()
    while True:
        try:
            cur = await conn.execute(
                """SELECT p.message_id FROM priorities p
                   JOIN messages m ON m.id=p.message_id
                   LEFT JOIN chats c ON c.chat_id=m.chat_id
                   WHERE p.score>=3.0
                     AND m.is_context_only=0 AND m.deleted_at IS NULL
                     AND COALESCE(c.archived,0)=0 AND COALESCE(c.processing,1)=1
                     AND p.classified_at >= datetime('now','-3 minutes')
                     AND NOT EXISTS (SELECT 1 FROM user_actions ua
                          WHERE ua.message_id=p.message_id AND ua.action IN ('done','archived','snoozed'))"""
            )
            ids = [r[0] for r in await cur.fetchall()]
            await cur.close()
            for mid in ids:
                if mid in seen:
                    continue
                ok = await send_inbox_notification(bot, conn, cfg, mid)
                if ok:
                    seen.add(mid)
            if len(seen) > 5000:
                seen.clear()
        except Exception as e:
            log.warning("bot watcher: %s", e)
        await asyncio.sleep(POLL_INTERVAL)


async def _deadline_loop(bot: TelegramClient, conn, cfg) -> None:
    """Напоминания о дедлайнах задач: за N ч (stage 1) и при наступлении/просрочке (stage 2)."""
    tz = ZoneInfo(cfg.timezone)
    while True:
        try:
            hours = live_reminder_hours(cfg.task_reminder_hours_before)
            now = datetime.now(timezone.utc)
            cur = await conn.execute(
                """SELECT id, title, due_at, reminder_stage FROM tasks
                   WHERE status IN ('todo','doing','waiting')
                     AND due_at IS NOT NULL AND reminder_stage < 2"""
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                due = _parse_due_local(r["due_at"], tz)
                if due is None:
                    continue
                tid, title, stage = r["id"], r["title"], r["reminder_stage"]
                if now >= due and stage < 2:
                    if await _send_deadline(bot, cfg, tid, title, r["due_at"], overdue=True):
                        await conn.execute("UPDATE tasks SET reminder_stage=2 WHERE id=?", (tid,))
                        await conn.commit()
                elif stage == 0 and (due - timedelta(hours=hours)) <= now < due:
                    if await _send_deadline(bot, cfg, tid, title, r["due_at"], overdue=False):
                        await conn.execute("UPDATE tasks SET reminder_stage=1 WHERE id=?", (tid,))
                        await conn.commit()
        except Exception as e:
            log.warning("deadline loop: %s", e)
        await asyncio.sleep(POLL_INTERVAL)


def register_handlers(bot: TelegramClient, conn, cfg) -> None:
    my_id = cfg.tg_my_id

    def _allowed(event) -> bool:
        return my_id and event.sender_id == my_id

    @bot.on(events.NewMessage(pattern=r"^/start"))
    async def _start(event):
        if not _allowed(event):
            return
        await event.reply(
            "Привет! Я буду присылать сюда новые задачи из Telegram.\n"
            "Команды:\n"
            "/inbox — топ-10 активных задач\n"
            "/tasks — открытые задачи из задачника\n"
            "/digest — сводка за вчера\n"
            "/help — помощь",
        )

    @bot.on(events.NewMessage(pattern=r"^/help"))
    async def _help(event):
        if not _allowed(event):
            return
        await event.reply(
            "/start — приветствие\n"
            "/inbox — последние 10 задач\n"
            "/tasks — открытые задачи из задачника (todo+doing)\n"
            "/digest — вчерашняя сводка\n"
            "Кнопки на сообщении: ✅ Готово, 💤 1 ч, 🌅 Завтра 10:00, 📦 Архив, 📋 В задачник, 🌐 Открыть.",
        )

    @bot.on(events.NewMessage(pattern=r"^/inbox"))
    async def _inbox(event):
        if not _allowed(event):
            return
        cur = await conn.execute(
            """SELECT m.id FROM messages m
               JOIN priorities p ON p.message_id=m.id
               LEFT JOIN chats c ON c.chat_id=m.chat_id
               WHERE m.is_context_only=0 AND m.deleted_at IS NULL
                 AND COALESCE(c.archived,0)=0 AND COALESCE(c.processing,1)=1
                 AND NOT EXISTS (SELECT 1 FROM user_actions ua
                   WHERE ua.message_id=m.id AND (ua.action IN ('done','archived')
                        OR (ua.action='snoozed' AND ua.snooze_until > datetime('now'))))
               ORDER BY p.score DESC, m.date_utc DESC LIMIT 10"""
        )
        ids = [r[0] for r in await cur.fetchall()]
        await cur.close()
        if not ids:
            await event.reply("Инбокс пуст 🎉")
            return
        for mid in ids:
            await send_inbox_notification(bot, conn, cfg, mid)

    @bot.on(events.NewMessage(pattern=r"^/tasks"))
    async def _tasks(event):
        if not _allowed(event):
            return
        cur = await conn.execute(
            """SELECT id, title, priority, due_at, status
               FROM tasks WHERE status IN ('todo','doing')
               ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                        due_at IS NULL, due_at, created_at
               LIMIT 10"""
        )
        rows = await cur.fetchall()
        await cur.close()
        if not rows:
            await event.reply("📋 Задачник пуст 🎉",
                              buttons=[[Button.url("🌐 Открыть задачник", f"{WEB_BASE}/tasks")]])
            return
        prio_emoji = {"high": "⚡", "normal": "·", "low": "▫"}
        lines = [f"📋 Задачник · открытых: {len(rows)}"]
        kb_rows = []
        for r in rows:
            tid, title, prio, due, status = r[0], r[1], r[2], r[3], r[4]
            tag = "🛠" if status == "doing" else "📥"
            line = f"{tag} #{tid} {prio_emoji.get(prio,'·')} {title}"
            if due:
                line += f" · ⏰ {due}"
            lines.append(line)
            kb_rows.append([Button.inline(f"✓ #{tid}", f"tdone:{tid}".encode())])
        kb_rows.append([Button.url("🌐 Открыть задачник", f"{WEB_BASE}/tasks")])
        await event.reply("\n".join(lines), buttons=kb_rows)

    @bot.on(events.NewMessage(pattern=r"^/digest"))
    async def _digest(event):
        if not _allowed(event):
            return
        cur = await conn.execute(
            """SELECT m.chat_title, m.sender_name, p.score, m.text
               FROM priorities p JOIN messages m ON m.id=p.message_id
               WHERE date(p.classified_at, '+3 hours') = date('now','-1 day','+3 hours')
                 AND m.is_context_only=0 AND m.deleted_at IS NULL
               ORDER BY p.score DESC LIMIT 20"""
        )
        rows = await cur.fetchall()
        await cur.close()
        if not rows:
            await event.reply("За вчера задач не было.")
            return
        lines = [f"Дайджест за вчера ({len(rows)}):"]
        for r in rows[:20]:
            t = (r["text"] or "").replace("\n", " ")[:120]
            lines.append(f"• [{(r['score'] or 0):.1f}] {r['chat_title']}/{r['sender_name']}: {t}")
        await event.reply("\n".join(lines))

    @bot.on(events.CallbackQuery())
    async def _cb(event):
        if my_id and event.sender_id != my_id:
            await event.answer("Нет доступа", alert=True)
            return
        data = event.data.decode("utf-8", errors="ignore")
        parts = data.split(":", 2)
        action = parts[0]
        # message_id может содержать ":" (chat_id:msg_id), берём оставшийся хвост
        if action == "snooze" and len(parts) >= 3:
            # snooze:<chat:msg>:preset — но preset последнее, message_id — между
            # data вид: snooze:{message_id}:{preset}; message_id = chat:msg → 4 части!
            # Парсим заново:
            raw = data[len("snooze:"):]
            # preset — после последнего :
            mid, _, preset = raw.rpartition(":")
            until = _snooze_until(preset)
            await _record_action(conn, mid, "snoozed", until)
            await event.edit(event.message.message + f"\n✓ Отложено · {datetime.now().strftime('%H:%M')}",
                             buttons=None)
            await event.answer("Отложено")
            return
        if action == "done":
            mid = data[len("done:"):]
            await _record_action(conn, mid, "done")
            await event.edit(event.message.message + f"\n✓ Готово · {datetime.now().strftime('%H:%M')}",
                             buttons=None)
            await event.answer("Готово")
            return
        if action == "archive":
            mid = data[len("archive:"):]
            await _record_action(conn, mid, "archived")
            await event.edit(event.message.message + f"\n✓ Архив · {datetime.now().strftime('%H:%M')}",
                             buttons=None)
            await event.answer("В архиве")
            return
        if action == "task":
            mid = data[len("task:"):]
            task_id = await create_task_from_message(conn, mid)
            if task_id is None:
                await event.answer("Сообщение не найдено", alert=True)
                return
            # заодно помечаем сообщение «Готово» — оно уходит из инбокса
            await _record_action(conn, mid, "done")
            await event.edit(
                event.message.message + f"\n📋 → Задача #{task_id} создана · убрано из инбокса · {datetime.now().strftime('%H:%M')}",
                buttons=[[Button.url("📋 Открыть задачу", f"{WEB_BASE}/tasks#task-{task_id}")]],
            )
            await event.answer(f"Задача #{task_id} создана")
            return
        if action == "tdone":
            try:
                tid = int(data[len("tdone:"):])
            except ValueError:
                await event.answer("bad task id"); return
            await conn.execute(
                "UPDATE tasks SET status='done', completed_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
                (tid,),
            )
            await conn.commit()
            await event.answer(f"Задача #{tid} закрыта")
            try:
                await event.edit(event.message.message + f"\n✓ #{tid} → done · {datetime.now().strftime('%H:%M')}")
            except Exception:
                pass
            return
        await event.answer("Неизвестное действие")


async def run_bot(conn, cfg) -> None:
    if not cfg.tg_bot_token:
        log.info("TG_BOT_TOKEN не задан — бот пропущен.")
        return
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        log.warning("TG_API_ID/HASH нужны и для бота — бот пропущен.")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bot = TelegramClient(str(BOT_SESSION_PATH.with_suffix("")), cfg.tg_api_id, cfg.tg_api_hash)
    await bot.start(bot_token=cfg.tg_bot_token)
    log.info("TG-бот запущен.")
    register_handlers(bot, conn, cfg)
    watcher = asyncio.create_task(_watcher_loop(bot, conn, cfg), name="bot_watcher")
    deadline = asyncio.create_task(_deadline_loop(bot, conn, cfg), name="bot_deadline")
    try:
        await bot.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        deadline.cancel()
        try:
            await deadline
        except (asyncio.CancelledError, Exception):
            pass
        await bot.disconnect()
