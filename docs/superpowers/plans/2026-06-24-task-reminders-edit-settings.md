# Task Reminders, Edit UI, Settings Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deadline reminders via the Telegram bot, an inline task-edit form on `/tasks`, and a `/settings` web page that edits `config.toml`; plus bulk-close the inbox backlog and verify all buttons.

**Architecture:** New `_deadline_loop` in `bot/main.py` polls `tasks` and notifies through the existing bot client, deduped by a new `tasks.reminder_stage` column; reminder-hours read live from `config.toml`. Task edit reuses the existing `POST /tasks/{id}/edit` with two new GET partials for inline HTMX swap. Settings page reads/writes flat `config.toml` via a dependency-free serializer in `app/settings_io.py`.

**Tech Stack:** Python 3.12, FastAPI, Telethon, aiosqlite, Jinja2 + HTMX + Pico.css.

**Verification convention:** Project has no pytest infra. Each task verifies via `compileall`, small assert-based check scripts run with `uv run python`, and live web smoke. Temp scripts live under `data/` (gitignored) and are deleted after use.

---

### Task 1: DB migration — `tasks.reminder_stage`

**Files:**
- Modify: `app/schema.sql`
- Modify: `app/db.py` (`ensure_columns`)

- [ ] **Step 1: Add column to schema.sql**

Find the `CREATE TABLE ... tasks` block in `app/schema.sql` and add the column (after `completed_at`):

```sql
    reminder_stage INTEGER NOT NULL DEFAULT 0,
```

- [ ] **Step 2: Add additive migration in db.py**

In `app/db.py:ensure_columns`, find the block that ensures `tasks` columns (search for `"tasks"`). Add alongside existing `ADD COLUMN` guards:

```python
    if "reminder_stage" not in _table_columns(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN reminder_stage INTEGER NOT NULL DEFAULT 0")
```

(Match the exact style of the surrounding `ensure_columns` code — if it uses `await` / aiosqlite, mirror that; the existing tasks-table guards in the same function are the template.)

- [ ] **Step 3: Apply + verify**

Run:
```
uv run python -m app.cli backup
uv run python -c "import asyncio; from app.db import init_db, connect; asyncio.run(init_db())"
```
Then verify column exists — create `data/_chk.py`:
```python
import sqlite3
cols = [r[1] for r in sqlite3.connect('data/app.db').execute("PRAGMA table_info(tasks)")]
assert "reminder_stage" in cols, cols
print("OK reminder_stage present")
```
Run: `uv run python data\_chk.py` → Expected: `OK reminder_stage present`. Then delete `data/_chk.py`.

- [ ] **Step 4: Commit**

```
git add app/schema.sql app/db.py
git commit -m "feat(db): add tasks.reminder_stage for deadline dedup"
```

---

### Task 2: Config — `task_reminder_hours_before` + flat-toml writer

**Files:**
- Create: `app/settings_io.py`
- Modify: `app/config.py` (dataclass + `load_config`)
- Modify: `config.toml`

- [ ] **Step 1: Add config value to config.toml**

Append to `config.toml`:
```toml
task_reminder_hours_before = 3
```

- [ ] **Step 2: Add field to Config dataclass + loader**

In `app/config.py`, add to `@dataclass Config` (after `notify_tg_bot`):
```python
    task_reminder_hours_before: int
```
In `load_config()` return, add:
```python
        task_reminder_hours_before=int(toml_cfg.get("task_reminder_hours_before", 3)),
```

- [ ] **Step 3: Create the flat-toml read/write helper**

Create `app/settings_io.py`:
```python
"""Чтение/запись плоского config.toml без сторонних зависимостей."""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"


def read_raw() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def write_config_values(updates: dict) -> None:
    """Слить updates в текущий config.toml и переписать (плоский, без таблиц)."""
    data = read_raw()
    data.update(updates)
    lines = [f"{k} = {_fmt(v)}" for k, v in data.items()]
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def live_reminder_hours(default: int = 3) -> int:
    """Прочитать часы до дедлайна заново из файла (для живого применения)."""
    try:
        return int(read_raw().get("task_reminder_hours_before", default))
    except Exception:
        return default
```

- [ ] **Step 4: Verify round-trip preserves other keys**

Create `data/_chk.py`:
```python
from app.settings_io import read_raw, write_config_values, live_reminder_hours
before = read_raw()
write_config_values({"task_reminder_hours_before": 5})
after = read_raw()
assert after["task_reminder_hours_before"] == 5
assert after["timezone"] == before["timezone"]
assert after["notify_tg_bot"] == before["notify_tg_bot"]
assert isinstance(after["notify_tg_bot"], bool)
assert after["llm_input_usd_per_m"] == before["llm_input_usd_per_m"]
write_config_values({"task_reminder_hours_before": 3})  # restore
assert live_reminder_hours() == 3
print("OK toml round-trip")
```
Run: `uv run python data\_chk.py` → Expected: `OK toml round-trip`. Delete `data/_chk.py`.

Also confirm config still loads: `uv run python -c "from app.config import load_config; print(load_config().task_reminder_hours_before)"` → Expected: `3`.

- [ ] **Step 5: Commit**

```
git add app/config.py app/settings_io.py config.toml
git commit -m "feat(config): task_reminder_hours_before + flat-toml writer"
```

---

### Task 3: Bot deadline-reminder loop

**Files:**
- Modify: `bot/main.py`

- [ ] **Step 1: Add imports + helpers**

In `bot/main.py`, extend the datetime import at top and add settings import:
```python
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from app.settings_io import live_reminder_hours
```
(`datetime, timezone` already imported — adjust the existing line to include `timedelta`; add `ZoneInfo` and the `live_reminder_hours` import.)

- [ ] **Step 2: Add due-parse + send helpers**

Add near the other module-level helpers in `bot/main.py`:
```python
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
```

- [ ] **Step 3: Add the loop**

Add after `_watcher_loop` in `bot/main.py`:
```python
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
```

- [ ] **Step 4: Register loop in run_bot**

In `bot/main.py:run_bot`, after the existing `watcher = asyncio.create_task(...)` line add:
```python
    deadline = asyncio.create_task(_deadline_loop(bot, conn, cfg), name="bot_deadline")
```
And in the `finally:` block, after the watcher cancellation, mirror it for `deadline`:
```python
        deadline.cancel()
        try:
            await deadline
        except (asyncio.CancelledError, Exception):
            pass
```

- [ ] **Step 5: Verify time logic in isolation**

Create `data/_chk.py`:
```python
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from bot.main import _parse_due_local

tz = ZoneInfo("Europe/Minsk")
# 14:30 local Minsk (UTC+3) == 11:30 UTC
due = _parse_due_local("2026-06-25T14:30", tz)
assert due == datetime(2026, 6, 25, 11, 30, tzinfo=timezone.utc), due
assert _parse_due_local("2026-06-25 14:30:00", tz) == due
assert _parse_due_local("garbage", tz) is None
# window math: with hours=3, "now" 2h before due is inside pre-window
now = due - timedelta(hours=2)
assert (due - timedelta(hours=3)) <= now < due
print("OK due parse + window")
```
Run: `uv run python data\_chk.py` → Expected: `OK due parse + window`. Delete `data/_chk.py`.

- [ ] **Step 6: compileall + commit**

```
uv run python -m compileall -q bot
git add bot/main.py
git commit -m "feat(bot): deadline reminder loop with stage dedup"
```

---

### Task 4: Inline task-edit UI

**Files:**
- Create: `web/templates/partials/task_edit.html`
- Modify: `web/templates/partials/task_row.html`
- Modify: `web/routes/tasks.py`

- [ ] **Step 1: Add `_due_local` helper + edit-form/row routes in tasks.py**

In `web/routes/tasks.py`, add a helper near the top (after the label dicts):
```python
def _due_local(due: str | None) -> str:
    """due_at из БД → значение для <input type=datetime-local> (YYYY-MM-DDTHH:MM)."""
    if not due:
        return ""
    return due.strip().replace(" ", "T")[:16]
```

Add two GET endpoints (after `tasks_set_status`):
```python
@router.get("/tasks/{task_id}/row")
async def tasks_row(request: Request, task_id: int):
    conn = request.app.state.db
    return await _render_row(request, conn, task_id)


@router.get("/tasks/{task_id}/edit-form")
async def tasks_edit_form(request: Request, task_id: int):
    conn = request.app.state.db
    cur = await conn.execute(
        """SELECT t.*, m.chat_title, m.sender_name, m.is_dm
           FROM tasks t LEFT JOIN messages m ON m.id=t.source_message_id
           WHERE t.id=?""",
        (task_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        raise HTTPException(404, "Задача не найдена")
    r = dict(row)
    r["due_local"] = _due_local(r.get("due_at"))
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/task_edit.html",
        {"t": r, "priorities": PRIORITIES, "priority_labels": PRIORITY_LABELS},
    )
```

- [ ] **Step 2: Reset reminder_stage when due_at changes in /edit**

Replace the body of `tasks_edit` so it resets the dedup stage only when `due_at` actually changes:
```python
@router.post("/tasks/{task_id}/edit")
async def tasks_edit(
    request: Request, task_id: int,
    title: str = Form(...),
    priority: str = Form("normal"),
    due_at: str = Form(""),
    notes: str = Form(""),
):
    if priority not in PRIORITIES:
        priority = "normal"
    conn = request.app.state.db
    new_due = due_at.strip() or None
    cur = await conn.execute("SELECT due_at FROM tasks WHERE id=?", (task_id,))
    old = await cur.fetchone()
    await cur.close()
    old_due = old["due_at"] if old else None
    if new_due != old_due:
        await conn.execute(
            "UPDATE tasks SET title=?, priority=?, due_at=?, notes=?, reminder_stage=0, updated_at=datetime('now') WHERE id=?",
            (title.strip(), priority, new_due, (notes.strip() or None), task_id),
        )
    else:
        await conn.execute(
            "UPDATE tasks SET title=?, priority=?, due_at=?, notes=?, updated_at=datetime('now') WHERE id=?",
            (title.strip(), priority, new_due, (notes.strip() or None), task_id),
        )
    await conn.commit()
    return await _render_row(request, conn, task_id)
```

- [ ] **Step 3: Create task_edit.html partial**

Create `web/templates/partials/task_edit.html`:
```html
<article id="task-{{ t.id }}" class="task-card prio-{{ t.priority }}"
         style="border-left:4px solid #b59f3b;background:var(--background-color,#fff);border-radius:6px;padding:.5rem .6rem;box-shadow:0 1px 2px rgba(0,0,0,.06);">
  <form hx-post="/tasks/{{ t.id }}/edit" hx-target="#task-{{ t.id }}" hx-swap="outerHTML"
        style="display:flex;flex-direction:column;gap:.4rem;">
    <input name="title" value="{{ t.title }}" required title="Заголовок задачи" style="font-size:16px;">
    <div style="display:flex;gap:.4rem;flex-wrap:wrap;">
      <select name="priority" title="Приоритет">
        <option value="low"    {% if t.priority=='low' %}selected{% endif %}>⬇ низкий</option>
        <option value="normal" {% if t.priority=='normal' %}selected{% endif %}>· обычный</option>
        <option value="high"   {% if t.priority=='high' %}selected{% endif %}>⚡ высокий</option>
      </select>
      <input type="datetime-local" name="due_at" value="{{ t.due_local }}" title="Дедлайн" style="font-size:16px;">
    </div>
    <textarea name="notes" rows="2" placeholder="Заметки..." title="Заметки" style="font-size:16px;">{{ t.notes or '' }}</textarea>
    <div style="display:flex;gap:.25rem;">
      <button type="submit" title="Сохранить изменения" style="font-size:.8rem;padding:.2rem .5rem;">💾 Сохранить</button>
      <button type="button" class="secondary" title="Отменить"
              hx-get="/tasks/{{ t.id }}/row" hx-target="#task-{{ t.id }}" hx-swap="outerHTML"
              style="font-size:.8rem;padding:.2rem .5rem;">Отмена</button>
    </div>
  </form>
</article>
```

- [ ] **Step 4: Add ✏ button to task_row.html**

In `web/templates/partials/task_row.html`, inside the `.task-actions` div, add before the delete button:
```html
    <button type="button" title="Редактировать задачу"
            hx-get="/tasks/{{ t.id }}/edit-form"
            hx-target="#task-{{ t.id }}" hx-swap="outerHTML"
            style="font-size:.78rem;padding:.15rem .4rem;">✏ Изменить</button>
```

- [ ] **Step 5: compileall + live smoke**

```
uv run python -m compileall -q web
```
Start web in background, then probe the new routes (use existing task #1):
```
Start-Process -FilePath "powershell" -ArgumentList "-NoProfile","-Command","uv run python -m app.cli web" -WindowStyle Hidden
```
Wait ~3s, then:
```
(Invoke-WebRequest http://127.0.0.1:8000/tasks/1/edit-form -UseBasicParsing).StatusCode
(Invoke-WebRequest http://127.0.0.1:8000/tasks/1/row -UseBasicParsing).StatusCode
```
Expected: `200` and `200`. Stop the web process afterward:
```
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*app.cli web*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```
(Only stop the *test* web process — note: a production web may also be running; identify by ProcessId started here. If unsure, skip stop and report.)

- [ ] **Step 6: Commit**

```
git add web/routes/tasks.py web/templates/partials/task_edit.html web/templates/partials/task_row.html
git commit -m "feat(web): inline task edit form on /tasks"
```

---

### Task 5: Settings page

**Files:**
- Create: `web/routes/settings.py`
- Create: `web/templates/settings.html`
- Modify: `web/app.py`
- Modify: `web/templates/base.html`

- [ ] **Step 1: Create settings route**

Create `web/routes/settings.py`:
```python
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
```

- [ ] **Step 2: Create settings.html**

Create `web/templates/settings.html`:
```html
{% extends "base.html" %}
{% block title %}Настройки — NexusTG{% endblock %}
{% block body %}
<h3>⚙ Настройки</h3>
<p style="opacity:.75;font-size:.9rem;">Настройки сохраняются в <code>config.toml</code>. Часы до дедлайна применяются сразу; остальное — после перезапуска сервиса.</p>

<form hx-post="/settings" style="display:flex;flex-direction:column;gap:.7rem;max-width:560px;">
  <label>Часы до дедлайна для напоминания <small>(применяется сразу)</small>
    <input type="number" name="task_reminder_hours_before" min="0" max="168"
           value="{{ cfg.task_reminder_hours_before | default(3) }}">
  </label>

  <div style="display:flex;gap:.6rem;flex-wrap:wrap;">
    <label style="flex:1;">Активные часы — старт <small>(перезапуск)</small>
      <input name="active_hours_start" value="{{ cfg.active_hours_start | default('10:00') }}" placeholder="10:00">
    </label>
    <label style="flex:1;">Активные часы — конец <small>(перезапуск)</small>
      <input name="active_hours_end" value="{{ cfg.active_hours_end | default('18:30') }}" placeholder="18:30">
    </label>
  </div>

  <fieldset>
    <legend>Уведомления <small>(перезапуск)</small></legend>
    <label><input type="checkbox" name="notify_tg_bot" {% if cfg.notify_tg_bot %}checked{% endif %}> Telegram-бот</label>
    <label><input type="checkbox" name="notify_windows_toast" {% if cfg.notify_windows_toast %}checked{% endif %}> Windows-toast</label>
    <label><input type="checkbox" name="notify_tg_self" {% if cfg.notify_tg_self %}checked{% endif %}> Селф-ЛС (Saved Messages)</label>
  </fieldset>

  <label>Дневной бюджет токенов LLM <small>(перезапуск)</small>
    <input type="number" name="grok_daily_token_budget" min="0" value="{{ cfg.grok_daily_token_budget | default(0) }}">
  </label>
  <label>Модель LLM <small>(перезапуск)</small>
    <input name="grok_model" value="{{ cfg.grok_model | default('') }}">
  </label>

  <button type="submit">💾 Сохранить</button>
</form>

<h4 style="margin-top:1.2rem;">Секреты (только из <code>.env</code>)</h4>
<table>
  <tbody>
    {% for k, present in secrets.items() %}
    <tr><td><code>{{ k }}</code></td><td>{% if present %}✅ задано{% else %}— не задано{% endif %}</td></tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 3: Register router in web/app.py**

In `web/app.py:create_app`, add `settings` to the import line:
```python
    from web.routes import inbox, message, search, topics, digest, health, actions, chats, reports, rules, done, templates_route, tasks, pwa, settings
```
And add the include after `tasks.router`:
```python
    app.include_router(settings.router)
```

- [ ] **Step 4: Add nav tab in base.html**

In `web/templates/base.html`, inside `.nav-collapse`, add after the `/health` link:
```html
    <a href="/settings" class="{% if active=='settings' %}active{% endif %}" title="Настройки: часы до дедлайна, активные часы, уведомления, LLM">⚙ Настройки</a>
```

- [ ] **Step 5: compileall + live smoke**

```
uv run python -m compileall -q web
```
Start web in background (as in Task 4 Step 5), then:
```
(Invoke-WebRequest http://127.0.0.1:8000/settings -UseBasicParsing).StatusCode
```
Expected: `200`. Then verify a POST round-trip leaves config valid — create `data/_chk.py`:
```python
from app.settings_io import read_raw
c = read_raw()
assert "task_reminder_hours_before" in c
assert isinstance(c["notify_tg_bot"], bool)
print("OK settings config intact")
```
Run `uv run python data\_chk.py` → Expected: `OK settings config intact`. Delete it. Stop test web process.

- [ ] **Step 6: Commit**

```
git add web/routes/settings.py web/templates/settings.html web/app.py web/templates/base.html
git commit -m "feat(web): settings page editing config.toml"
```

---

### Task 6: Bulk-close inbox backlog (older than today)

**Files:**
- Temp: `data/_bulk_close.py` (deleted after run)

- [ ] **Step 1: Backup DB first**

```
uv run python -m app.cli backup
```
Expected: a `backups/data_*.zip` is created. Confirm with `Get-ChildItem backups | Sort LastWriteTime -Desc | Select -First 1`.

- [ ] **Step 2: Write the bulk-close script**

Create `data/_bulk_close.py`:
```python
import sqlite3
c = sqlite3.connect('data/app.db')
c.row_factory = sqlite3.Row

open_filter = """
FROM messages m
LEFT JOIN chats c ON c.chat_id=m.chat_id
WHERE m.is_context_only=0 AND m.deleted_at IS NULL
  AND COALESCE(c.archived,0)=0 AND COALESCE(c.processing,1)=1
  AND NOT EXISTS (SELECT 1 FROM user_actions ua
       WHERE ua.message_id=m.id AND (ua.action IN ('done','archived')
            OR (ua.action='snoozed' AND ua.snooze_until > datetime('now'))))
  AND date(m.date_utc,'+3 hours') < date('now','+3 hours')
"""
ids = [r["id"] for r in c.execute("SELECT m.id " + open_filter)]
print("to close:", len(ids))
c.executemany("INSERT INTO user_actions(message_id, action) VALUES (?, 'done')",
              [(i,) for i in ids])
c.commit()
# report remaining open today
rem = c.execute("""
SELECT count(*) FROM messages m LEFT JOIN chats c ON c.chat_id=m.chat_id
WHERE m.is_context_only=0 AND m.deleted_at IS NULL
  AND COALESCE(c.archived,0)=0 AND COALESCE(c.processing,1)=1
  AND NOT EXISTS (SELECT 1 FROM user_actions ua WHERE ua.message_id=m.id
       AND (ua.action IN ('done','archived') OR (ua.action='snoozed' AND ua.snooze_until > datetime('now'))))
""").fetchone()[0]
print("remaining open (today):", rem)
```

- [ ] **Step 3: Run + verify**

Run: `uv run python data\_bulk_close.py`
Expected: `to close: ~4412` then `remaining open (today): ~109`. If `to close` is wildly off (e.g. 0 or >5000), STOP and report — do not proceed.

- [ ] **Step 4: Clean up temp script**

```
Remove-Item data\_bulk_close.py
```
(No git commit — operates on gitignored `data/`.)

---

### Task 7: Verify all buttons (audit + smoke)

**Files:** none (verification only)

- [ ] **Step 1: Dispatch audit subagent**

Use the Explore agent with this prompt:
> Audit button/endpoint wiring in this repo. (1) In `web/templates/**/*.html`, list every `hx-get`/`hx-post`/`hx-delete` URL and every `<form action=>`. For each, confirm a matching route exists in `web/routes/*.py` (account for `{id}` path params). (2) In `bot/main.py`, list every `Button.inline` callback prefix (text before `:`) and confirm each has a handling branch in the `_cb` callback. Report any template button pointing at a missing route, or any bot inline button whose prefix has no `_cb` branch. Output a table: location → target → OK/BROKEN.

Review the report. Fix any BROKEN wiring found (likely none for pre-existing buttons; focus on the newly added `✏ Изменить`, `/settings`, deadline buttons).

- [ ] **Step 2: Live web smoke of key GET routes**

Start web in background, then probe each (all should be `200`):
```
"/","/search","/topics","/tasks","/done","/digest","/reports","/chats","/rules","/templates","/health","/settings","/tasks/1/edit-form","/tasks/1/row" | ForEach-Object { "$_ -> " + (Invoke-WebRequest ("http://127.0.0.1:8000"+$_) -UseBasicParsing).StatusCode }
```
Expected: every line ends in `200`. Stop the test web process afterward.

- [ ] **Step 3: Report findings**

Summarize: which buttons/routes verified OK, anything fixed. No commit unless a fix was made (commit fixes with `fix(web): ...`).

---

### Task 8: Restart bot, journal, finalize

**Files:**
- Modify: `docs/PROJECT_JOURNAL.md`

- [ ] **Step 1: Restart the running service so new bot loop + config field load**

The `app.cli run` process loaded old config/code. Restart it:
```
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*app.cli run*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process -FilePath "powershell" -ArgumentList "-NoProfile","-Command","uv run python -m app.cli run" -WindowStyle Hidden
```
Wait ~8s, then check `data/run.err.log` tail is clean (no traceback) and the deadline loop logged start if it logs. Restart web likewise if it was running prod.

- [ ] **Step 2: Update journal §6 chronology**

In `docs/PROJECT_JOURNAL.md`, add rows to the §6 table:
```
| 2026-06-24 | **Напоминания о дедлайнах**: цикл `_deadline_loop` в `bot/main.py` шлёт в бота за N ч до `due_at` (stage 1) и при наступлении/просрочке (stage 2); дедуп через новую колонку `tasks.reminder_stage`; N (`task_reminder_hours_before`) читается живьём из config.toml | `bot/main.py`, `app/db.py`, `app/schema.sql` |
| 2026-06-24 | **Inline-редактирование задачи**: кнопка ✏ → форма (`partials/task_edit.html`) через HTMX-swap; GET `/tasks/{id}/edit-form` и `/tasks/{id}/row`; правка due_at сбрасывает `reminder_stage` | `web/routes/tasks.py`, `web/templates/partials/*` |
| 2026-06-24 | **Вкладка «Настройки»**: `/settings` редактирует безопасные поля config.toml (часы до дедлайна — живьём, остальное — после перезапуска); flat-toml writer `app/settings_io.py`; секреты .env только для чтения | `web/routes/settings.py`, `web/templates/settings.html`, `app/settings_io.py` |
| 2026-06-24 | **Массовое закрытие инбокса**: ~4412 сообщений старше сегодня помечены done, 109 сегодняшних оставлены | разовый скрипт |
```
Also update §3 (tasks table fields — add `reminder_stage`) and §8 TODO (mark deadline reminders + task edit UI done).

- [ ] **Step 3: Commit journal**

```
git add docs/PROJECT_JOURNAL.md
git commit -m "docs: journal — deadline reminders, task edit, settings, bulk-close"
```

- [ ] **Step 4: Hand off for manual phone test**

Tell the user: create a task on `/tasks` with a due_at ~2 minutes ahead, set `task_reminder_hours_before` small if needed, and confirm the bot sends the reminder + the `✅ Готово` button closes it. This validates the end-to-end deadline flow that can't be auto-tested.

---

## Self-Review

**Spec coverage:**
- Фича 1 (reminders) → Tasks 1, 2, 3 ✓
- Фича 2 (edit UI) → Task 4 ✓
- Фича 3 (settings) → Tasks 2 (writer/config), 5 ✓
- Op A (bulk close) → Task 6 ✓
- Op B (verify buttons) → Task 7 ✓
- Journal + finalize → Task 8 ✓
- Live reminder-hours reload → Task 2 (`live_reminder_hours`) + Task 3 (loop uses it) ✓
- reminder_stage reset on due change → Task 4 Step 2 ✓

**Placeholder scan:** No TBD/TODO; all code blocks concrete. "~4412/~109" are expected-value ranges with explicit STOP guard, not placeholders.

**Type consistency:** `reminder_stage` INTEGER consistent across schema/migration/loop. `task_reminder_hours_before` int across config.toml/dataclass/loader/settings form/live reader. `live_reminder_hours()` signature matches call in `_deadline_loop`. `_parse_due_local`/`_send_deadline`/`_due_local` names consistent between definition and use.
