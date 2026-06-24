# NexusTG — Напоминания о дедлайнах, редактирование задач, вкладка «Настройки»

**Дата:** 2026-06-24
**Статус:** одобрено пользователем, готово к реализации

## Контекст

Три связанные фичи задачника/конфигурации + одна операционная задача (массовое закрытие инбокса) + финальная проверка кнопок. Все правки локальные, в стеке Python 3.12 / FastAPI / Telethon / SQLite / HTMX / Pico.css. Тестов в проекте нет — проверка через `compileall` + ручной/субагентный smoke.

---

## Фича 1 — Напоминания о дедлайнах через Telegram-бот

**Цель:** задачи с `due_at` шлют напоминание в бота за N часов до дедлайна и при наступлении/просрочке.

**Канал:** только Telegram-бот (`@claudeCLOD_bot`). Toast/селф-ЛС не дублируем.

**Реализация:** новый асинхронный цикл `_deadline_loop(bot, conn, cfg)` в `bot/main.py`, по аналогии с `_watcher_loop`, регистрируется в `run_bot` рядом с watcher. Поллинг каждые `POLL_INTERVAL` (30с).

**Дедуп:** новая колонка `tasks.reminder_stage INTEGER NOT NULL DEFAULT 0`:
- `0` — ничего не отправляли
- `1` — отправлено предупреждение «за N часов»
- `2` — отправлено уведомление о наступлении/просрочке

**Логика тика** (только `status IN ('todo','doing','waiting')` и `due_at IS NOT NULL`):
- `stage=0` и `now >= due − N ч` и `now < due` → шлём предупреждение, `stage=1`
- `stage<2` и `now >= due` → шлём «дедлайн наступил/просрочен», `stage=2`

**Время:** `due_at` хранится как наивное локальное (`datetime-local` → `"2026-06-25T14:30"`). В цикле парсится как локальное в `ZoneInfo(cfg.timezone)`, переводится в UTC, сравнивается с `datetime.now(timezone.utc)`. Невалидный/непарсящийся `due_at` пропускаем (лог debug).

**N (часы):** читается **живьём** из `config.toml` на каждом тике через хелпер (не из замороженного `cfg`), чтобы смена в «Настройках» применялась без перезапуска. Дефолт `3`. Ключ — `task_reminder_hours_before`.

**Сообщение бота:**
```
⏰ Дедлайн через ~N ч
#<id> <title>
Срок: <due_at>
```
(для просрочки — «🔴 Дедлайн наступил/просрочен»). Кнопки: `[✓ Готово]` → существующий колбэк `tdone:{id}`; `[🌐 Открыть задачу]` → `{WEB_BASE}/tasks#task-{id}`.

**Сброс stage:** при изменении `due_at` через `POST /tasks/{id}/edit` ставим `reminder_stage=0` (перенос дедлайна должен заново триггерить напоминание).

---

## Фича 2 — UI-форма редактирования задачи

Backend `POST /tasks/{id}/edit` уже существует (принимает title/priority/due_at/notes). Нужен только фронт — inline-форма через HTMX-swap, без JS-модалок.

**Поток:**
1. Кнопка `✏ Изменить` на карточке (`partials/task_row.html`) → `hx-get /tasks/{id}/edit-form` → заменяет карточку формой.
2. `partials/task_edit.html`: поля title / priority (select) / due_at (`datetime-local`) / notes — предзаполнены текущими значениями.
3. `Сохранить` → `hx-post /tasks/{id}/edit` → возвращает обновлённый `partials/task_row.html`.
4. `Отмена` → `hx-get /tasks/{id}/row` → возвращает карточку без изменений.

**Новые эндпоинты (GET, рендерят partial):**
- `GET /tasks/{id}/edit-form` → `partials/task_edit.html`
- `GET /tasks/{id}/row` → `partials/task_row.html`

`due_at` для предзаполнения `datetime-local` приводится к формату `YYYY-MM-DDTHH:MM` (если в БД с пробелом/секундами — нормализуем).

---

## Фича 3 — Вкладка «Настройки»

**Цель:** редактировать безопасные поля конфига из веба.

**Роут** `web/routes/settings.py`: `GET /settings` (форма) + `POST /settings` (сохранение). Регистрация в `web/app.py:create_app()`. Вкладка в `base.html` (`active=='settings'`).

**Редактируемые поля (из `config.toml`):**
- `task_reminder_hours_before` (новое, int, дефолт 3) — **живое**
- `active_hours_start`, `active_hours_end` (HH:MM) — нужен перезапуск
- `notify_windows_toast`, `notify_tg_self`, `notify_tg_bot` (bool) — нужен перезапуск
- `grok_daily_token_budget` (int) — нужен перезапуск
- `grok_model` (str) — нужен перезапуск

**Только для чтения (показать, не редактировать):** секреты из `.env` (токены, ключи) — выводим «задано в .env / скрыто», без значений.

**Запись config.toml:** свой минимальный сериализатор плоского toml (`app/config.py:write_config(updates: dict)` или отдельный `app/settings_io.py`) — читает текущий toml, обновляет ключи, переписывает файл с сохранением типов (str в кавычках, bool как `true/false`, числа без кавычек). Без новой зависимости `tomli-w`. Валидация значений на входе (часы 0–168, HH:MM regex, budget ≥ 0).

**UI-маркировка:** поля «нужен перезапуск» помечены припиской; `task_reminder_hours_before` — «применяется сразу».

**Добавить в dataclass `Config`:** поле `task_reminder_hours_before: int` + чтение в `load_config()`.

---

## Операционная задача A — Массовое закрытие инбокса

Пометить `done` все открытые сообщения **старше сегодня** (локально Europe/Minsk), сегодняшние (109) не трогать. Ожидаемо ~4412 сообщений.

**Перед выполнением:** бэкап БД (`uv run python -m app.cli backup` или копия `data/app.db`).

**Критерий «открытое»** (как в боте `/inbox`): `is_context_only=0`, `deleted_at IS NULL`, чат не archived и `processing=1`, нет действия `done/archived` и нет активного `snoozed`.

**Операция:** для каждого подходящего `message_id` (с `date(date_utc,'+3h') < date('now','+3h')`) вставить `INSERT INTO user_actions(message_id, action) VALUES (?, 'done')`. Одной транзакцией. Затем отчёт: сколько закрыто, сколько осталось открытых сегодня.

---

## Операционная задача B — Проверка всех кнопок

Финальная фаза. Аудит + smoke:
1. **Субагент-аудит:** сверить все HTMX-кнопки в шаблонах (`hx-get/hx-post` пути) с зарегистрированными роутами в `web/routes/*`, и все inline-кнопки бота (`Button.inline` префиксы) с ветками колбэка `_cb`. Найти битые/осиротевшие.
2. **Живой smoke:** поднять `.\web.ps1` в фоне, пройти ключевые GET-роуты (200), проверить новые эндпоинты (`/settings`, `/tasks/{id}/edit-form`, `/tasks/{id}/row`). Остановить процесс после.
3. Бот-кнопки проверяются логикой (аудит) + ручным тестом пользователя на финале (создать задачу с дедлайном через пару минут).

---

## Затрагиваемые файлы

| Файл | Изменение |
|---|---|
| `app/schema.sql` | колонка `tasks.reminder_stage` |
| `app/db.py` (`ensure_columns`) | миграция `reminder_stage` |
| `app/config.py` | поле `task_reminder_hours_before` + запись toml |
| `config.toml` | `task_reminder_hours_before = 3` |
| `bot/main.py` | `_deadline_loop` + регистрация в `run_bot` |
| `web/routes/tasks.py` | GET `/edit-form`, GET `/row`; сброс `reminder_stage` в `/edit` |
| `web/routes/settings.py` | новый роут (GET/POST) |
| `web/app.py` | регистрация settings-роутера |
| `web/templates/base.html` | вкладка «Настройки» |
| `web/templates/settings.html` | новая страница |
| `web/templates/partials/task_row.html` | кнопка ✏ |
| `web/templates/partials/task_edit.html` | новый partial |
| `docs/PROJECT_JOURNAL.md` | запись в §6 хронологии |

## Проверка (нет юнит-тестов)

- `uv run python -m compileall -q app ingestion classifier web bot notifications`
- `uv run python -c "from app import cli"`
- Живой web smoke (см. задачу B)
- `data/run.err.log` чист после рестарта бота

## Вне скоупа (отложено)

- Lite-режим (`--lite`)
- Drag-and-drop на kanban, подзадачи, теги, bulk-операции в задачнике, CSV-экспорт задач
- Редактирование секретов (.env) через веб
