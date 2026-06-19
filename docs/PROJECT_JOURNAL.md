# Журнал проекта NexusTG

Контекст и история итераций для возврата к проекту. Дата создания: **2026-06-11**.

---

## 1. Что такое NexusTG (одной фразой)

Локальный AI-агрегатор Telegram-сообщений (ЛС + @упоминания + ответы) с LLM-классификацией, веб-инбоксом, задачником и Telegram-ботом для уведомлений. Всё под `./data/`, без облаков.

GitHub: https://github.com/sigurt33/NexusTG

---

## 2. Архитектура модулей

| Модуль | Назначение | Ключевые файлы |
|---|---|---|
| `app/` | конфиг, БД, CLI, общие хелперы | `cli.py`, `config.py`, `db.py`, `schema.sql`, `links.py`, `tasks.py` |
| `ingestion/` | Telethon-листенер + backfill + context | `telegram_listener.py`, `backfill.py`, `context.py`, `chats_sync.py` |
| `classifier/` | LLM-классификация (Gemini/Grok через OpenAI-совместимый API) | `grok_worker.py`, `prompts.py`, `topic_normalizer.py`, `consolidator.py` |
| `web/` | FastAPI + Jinja + HTMX + Pico.css | `app.py`, `routes/*.py`, `templates/*.html` |
| `bot/` | Telegram-бот для inbox-уведомлений | `main.py` |
| `notifications/` | Windows toast, self-ЛС, scheduler, outbox | `toast.py`, `tg_self.py`, `scheduler.py`, `outbox_sender.py` |

---

## 3. Структура БД (актуальное состояние)

Основные таблицы (см. `app/schema.sql` + аддитивные миграции в `app/db.py:ensure_columns`):

- **`messages`** — pk `id` = `"{chat_id}:{msg_id}"`, поля `chat_id`, `chat_title`, `sender_*`, `text`, `date_utc`, `is_dm`/`is_mention`/`is_reply_to_me`/`is_context_only`, `raw_json`, `edited_at`, `deleted_at`
- **`context_links`** — ±5 соседних сообщений вокруг target'а
- **`topics`** — иерархия тем (с `parent_id`), `hidden`, `message_count`
- **`message_topics`** — many-to-many
- **`priorities`** — `urgency` (1-5), `importance` (1-5), `score`, `rationale`, `model_version`
- **`user_actions`** — done/snoozed/archived/unread (история действий)
- **`chats`** — список чатов с `processing` (allow/block) и `archived`
- **`grok_usage`** — расход токенов по дням
- **`pending_notifications`** — очередь для бота/toast
- **`user_rules`** — «Алина-режим»: правила свободным текстом в промт
- **`reply_templates`** — заготовки ответов
- **`outbox`** — очередь исходящих ответов
- **`classification_examples`** — обучающие примеры (top-5 в промте)
- **`tasks`** — задачник (новая фича от 2026-06-11):
  - `id` (auto), `title`, `status` (`todo/doing/waiting/done/cancelled`), `priority` (`low/normal/high`)
  - `due_at`, `notes`, `source_message_id` (FK → messages, nullable), `created_at`, `updated_at`, `completed_at`
- **`messages_fts`** — FTS5 индекс по тексту/чату/отправителю

---

## 4. Запуск

```powershell
.\setup.ps1   # первичный логин (один раз)
.\run.ps1     # ingestion + classifier + bot
.\web.ps1     # http://127.0.0.1:8000
```

CLI: `uv run python -m app.cli {login|run|web|bot|backup}`

⚠️ Не запускать `run` в двух местах одновременно — Telegram отзовёт сессию.

---

## 5. Логи и диагностика

- Свежие логи: `data/run.log`, `data/run.err.log` (если есть)
- Архив старых: `data/logs/run_old_*.log`
- Сессия активна? — смотреть `LastWriteTime` у `data/tg.session` и `data/tg_bot.session`
- Сколько сообщений: `select count(*), max(date_utc) from messages`
- Compileall перед коммитом: `uv run python -m compileall -q app ingestion classifier web bot notifications`

---

## 6. Хронология фич

| Дата | Изменение | Коммит/файлы |
|---|---|---|
| 2026-05-27 | Phase 1: ingestion + storage | начальная версия |
| 2026-05-29..30 | Phase 2-4: classifier (Gemini), web inbox, темы, реклассификация, примеры | classifier/, web/ |
| 2026-05-30 | Phase 5: /chats allow-block, отчёты, CSV-экспорт, дерево тем, шаблоны, правила, TG-бот | bot/, web/routes/* |
| 2026-06-11 | GitHub-публикация: `sigurt33/NexusTG`, README с описанием, скриншоты (заблюренные), скиллы `nexustg-dev` и `nexustg-lite` в `skills/`, junction в `.claude/skills/` | первые коммиты + skills/ |
| 2026-06-11 | **Задачник (Tasks)**: новая таблица `tasks`, веб-страница `/tasks` (kanban-style по статусам), кнопка «📋 → В задачник» на карточке сообщения, общий helper `app/links.py`, бот: кнопка `📋 В задачник` под инбокс-сообщением, команда `/tasks`, callback `task_done` | см. §7 |
| 2026-06-11 | Журнал проекта создан (`docs/PROJECT_JOURNAL.md`) | этот файл |
| 2026-06-12 | **Русификация задачника**: добавлены `STATUS_BTN_LABELS` и `PRIORITY_LABELS` в `web/routes/tasks.py`; все статус-кнопки в `partials/task_row.html` теперь подписаны по-русски («→ В процессе», «→ Готово», «→ Жду», «→ Отменено», «→ К работе»); кнопки «💬 К сообщению в Telegram», «🌐 В дашборде», «🗑 Удалить»; форма создания: ⬇ низкий / · обычный / ⚡ высокий | `web/routes/tasks.py`, `web/templates/tasks.html`, `web/templates/partials/task_row.html` |
| 2026-06-12 | **Закреп инструкции в Telegram-боте**: одноразовый скрипт `bot/pin_help.py` снимает старые закрепы (`UnpinAllMessagesRequest`), шлёт краткую инструкцию (команды + кнопки), закрепляет её (`pin_message`) и обновляет список команд бота через `SetBotCommandsRequest` (видим в меню `/`) | `bot/pin_help.py` |
| 2026-06-19 | **Пуш изменений 11–12 июня в `sigurt33/NexusTG`**: 3 коммита — `feat: add task manager`, `feat(bot): add pin_help script`, `docs: add project journal and update nexustg-dev skill`. `REVIEW.md` оставлен локально (личный отзыв, не для публичного репо) | `git push` (`6235242..dead2da`) |

---

## 7. Фича «Задачник» (2026-06-11) — что и где

**Модель:** `tasks(id, title, status, priority, due_at, notes, source_message_id, created_at, updated_at, completed_at)`. Статусы: todo / doing / waiting / done / cancelled. Приоритеты: low / normal / high.

**Backend:**
- `app/tasks.py` — pure-async `create_task_from_message(conn, message_id)` + `build_task_from_message` (генерит title из текста + prio из urgency/importance)
- `app/links.py` — общий `telegram_deep_link(message_id, is_dm)`; раньше код дублировался в `bot/main.py` и `web/routes/message.py`, теперь единый
- `app/schema.sql` + `app/db.py:ensure_columns` — миграция таблицы `tasks`

**Web (`web/routes/tasks.py`):**

| Метод | Путь | |
|---|---|---|
| GET | `/tasks` | страница, kanban по 5 статусам |
| POST | `/tasks/create` | ручная задача (HX-Redirect → /tasks) |
| POST | `/tasks/from-message/{message_id:path}` | конвертация из сообщения, возвращает inline-link «Задача #N создана» |
| POST | `/tasks/{id}/status` | смена статуса, возвращает обновлённый `partials/task_row.html` |
| POST | `/tasks/{id}/edit` | редактирование полей |
| POST | `/tasks/{id}/delete` | удаление |

Шаблоны: `tasks.html`, `partials/task_row.html`. Inline-стили — Pico-совместимые. Вкладка в `base.html` между «Темы» и «Выполненные». Кнопка «📋 → В задачник» добавлена в `web/templates/message.html` рядом с `📦 Архив`.

**Bot (`bot/main.py`):**
- В `_format_payload` добавлен ряд `[Button.inline("📋 В задачник", f"task:{message_id}")]`
- Callback handler `task:{mid}` — создаёт задачу, редактирует сообщение бота с указанием `Задача #N создана`, заменяет кнопки на `[Button.url("📋 Открыть задачу", WEB_BASE + "/tasks#task-N")]`
- Callback `tdone:{task_id}` — закрывает задачу из бота
- Команда `/tasks` — список 10 открытых задач с приоритетами, дедлайнами, и кнопками `✓ #N` для закрытия

---

## 7a. Bot — обновление справки (2026-06-12)

Скрипт `bot/pin_help.py` — идемпотентный, можно перезапускать после изменения текста или команд:

```powershell
uv run python -m bot.pin_help
```

Что делает:
1. `UnpinAllMessagesRequest(peer=TG_MY_ID)` — снимает все закрепы в чате с ботом
2. Шлёт `HELP_TEXT` (markdown) на `TG_MY_ID` и закрепляет без уведомления
3. `SetBotCommandsRequest` с `BotCommandScopeDefault` — обновляет 5 команд (`start`, `inbox`, `tasks`, `digest`, `help`), которые появляются в меню `/` в Telegram

⚠️ Должен быть `PYTHONIOENCODING=utf-8` при запуске на Windows (cp1251-stdout роняет `print` с эмодзи). Скрипт сам пытается `sys.stdout.reconfigure(encoding="utf-8")`.

Тексты — внутри файла, константы `HELP_TEXT` и `COMMANDS`. Менять там.

---

## 8. Что НЕ реализовано (TODO)

- [ ] Drag-and-drop на kanban-доске
- [ ] Напоминания о дедлайнах (cron + push в бот за N часов до `due_at`)
- [ ] Подзадачи / теги
- [ ] Bulk-операции в задачнике («закрыть все done старше недели»)
- [ ] CSV-экспорт задач (паттерн копировать из `/export/messages.csv`)
- [ ] UI-форма редактирования задачи (сейчас только статус и удаление через кнопки; для edit полей нужен модал)
- [ ] Lite-режим (`--lite` флаг) — см. `skills/nexustg-lite/SKILL.md`, концепт описан, реализации нет
- [x] ~~Коммит и пуш изменений 11–12 июня в `sigurt33/NexusTG`~~ — сделано 2026-06-19 (`6235242..dead2da`)

---

## 9. Известные особенности и грабли

- **Composite message_id с `:`** — в callback'ах бота нельзя просто `data.split(":")`. Используем `data[len("prefix:"):]` для извлечения хвоста.
- **PowerShell + кириллица в curl** — теряется кодировка через msys-bash. Тестировать формы только через браузер.
- **Junction-линки на Windows** — для shared скиллов между `skills/` (в репо) и `.claude/skills/` (gitignored). Создаются через `New-Item -ItemType Junction`.
- **`run.log` от прошлых сессий** — не путать с текущими. При перезапуске архивирую в `data/logs/run_old_<ts>.log`.
- **Параллельные `app.cli run`** — критично! Не запускать 2+ инстанса с одной сессией → Telegram отзовёт.
- **`uv run`** порождает 2 процесса python.exe (wrapper + сам процесс) — это норма, не дубликат.
- **CP1251 stdout на Windows** — `print()` с кириллицей/эмодзи падает с `UnicodeEncodeError`. Решения: `PYTHONIOENCODING=utf-8` перед запуском или `sys.stdout.reconfigure(encoding="utf-8")` в начале скрипта.
- **Параллельная Telethon-сессия бота** — `bot.start(bot_token=...)` поверх уже работающего бота не ломается (bot API толерантен), но писать в один и тот же session-файл одновременно из двух процессов не стоит. `bot/pin_help.py` использует тот же `tg_bot.session` — запускать только когда основной run остановлен, либо после, либо вместе (telegram bot API толерантен, но во избежание гонок — последовательно).

---

## 10. Полезные команды диагностики

```powershell
# Что запущено
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? { $_.CommandLine -like "*app.cli*" } | Select Id, CommandLine

# Остановить всё
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? { $_.CommandLine -like "*app.cli*" } | % { Stop-Process -Id $_.ProcessId -Force }

# Сколько новых сообщений за 10 минут
uv run python -c "import sqlite3; print(sqlite3.connect('data/app.db').execute(\"select count(*) from messages where date_utc>datetime('now','-10 minutes')\").fetchone())"

# Открытые задачи
uv run python -c "import sqlite3; [print(r) for r in sqlite3.connect('data/app.db').execute(\"select id, status, priority, title from tasks where status in ('todo','doing') order by id\").fetchall()]"

# Обновить закреп и команды в боте (после правки HELP_TEXT/COMMANDS)
$env:PYTHONIOENCODING="utf-8"; uv run python -m bot.pin_help
```

---

## 11. Reference map (полный поверхностный inventory)

> Снимок от 2026-06-12. Используй как стартовую точку при правках — `Grep`/`Read` подтвердят актуальность.

### 11.1 CLI (`app/cli.py`)

| Команда | Функция | Назначение |
|---|---|---|
| `login` | `cmd_login` | Интерактивный логин (телефон + код + 2FA) |
| `login-sms` | `cmd_login_sms` | Принудительно SMS вместо app-кода |
| `run` | `cmd_run` | Основной демон: ingestion + classifier + scheduler + bot |
| `web` | `cmd_web` | uvicorn FastAPI на 127.0.0.1:8000 |
| `bot` | `cmd_bot` | Только бот (debug) |
| `classify` | `cmd_classify` | Только классификатор |
| `classify-batch` | `cmd_classify_batch` | One-shot пачка с отчётом |
| `sync-chats` | `cmd_sync_chats` | Обновить таблицу `chats` |
| `notify` | `cmd_notify` | Только scheduler (debug) |
| `backup` | `cmd_backup` | zip `data/` → `backups/data_YYYYMMDD_HHMM.zip` |

### 11.2 Конфиг (`app/config.py` + `config.toml` + `.env`)

**Из .env:** `TG_API_ID`, `TG_API_HASH`, `XAI_API_KEY`, `TG_BOT_TOKEN`, `TG_MY_ID`

**Из config.toml:** `timezone`, `active_hours_start`, `active_hours_end`, `backfill_days`, `context_window`, `notify_windows_toast`, `notify_tg_self`, `notify_tg_bot`, `ui_lang`, `grok_model`, `grok_daily_token_budget`, `llm_base_url`, `llm_input_usd_per_m`, `llm_output_usd_per_m`

### 11.3 Web-маршруты (полный список)

| Файл | Маршрут | Назначение |
|---|---|---|
| `inbox.py` | GET `/` | Инбокс с фильтрами по источнику/теме/чату/тексту |
| `search.py` | GET `/search` | FTS5 поиск |
| `topics.py` | GET `/topics`, POST `/topics/{id}/rename`, `/hide`, `/unhide`, `/merge`, `/merge-bulk`, `/set-parent`, `/unassign/{mid}`, `/assign-message`, GET `/topics/{id}/messages` | Управление таксономией |
| `message.py` | GET `/message/{mid}`, POST `/reclassify`, `/priority`, `/save-example`, GET `/examples`, POST `/examples/{mid}/delete`, `/note` | Карточка сообщения + примеры |
| `actions.py` | POST `/actions/{mid}/done`, `/snooze`, `/archive`, `/unread` | Действия над сообщением |
| `tasks.py` | GET `/tasks`, POST `/tasks/create`, `/tasks/from-message/{mid}`, `/tasks/{tid}/status`, `/edit`, `/delete` | Задачник |
| `chats.py` | GET `/chats`, POST `/chats/{cid}/toggle-processing` | Allow/block чатов |
| `digest.py` | GET `/digest` | Сводка за вчера |
| `done.py` | GET `/done` | Выполненные сообщения |
| `reports.py` | GET `/reports`, `/reports/weekly`, `/reports/topics`, `/reports/chats`, `/reports/senders`, `/export/messages.csv`, `/export/topics.csv` | Отчёты и CSV-экспорт |
| `rules.py` | GET `/rules`, POST `/rules/add`, `/rules/{id}/toggle`, `/delete` | Правила «Алина-режим» |
| `templates_route.py` | GET `/templates`, GET `/api/templates`, POST `/templates/add`, `/templates/{id}/edit`, `/delete` | Шаблоны ответов |
| `health.py` | GET `/health` | Статус сервиса |

Регистрация всех роутеров — `web/app.py:create_app()` (около строки 112).

### 11.4 Шаблоны (`web/templates/`)

**Страницы:** `base.html`, `inbox.html`, `search.html`, `topics.html`, `topic_messages.html`, `message.html`, `chats.html`, `digest.html`, `done.html`, `reports.html`, `reports_weekly.html`, `reports_topics.html`, `reports_chats.html`, `reports_senders.html`, `rules.html`, `templates.html`, `health.html`, `examples.html`, `tasks.html`.

**Partials (`partials/`):** `message_list.html`, `message_row.html`, `message_priority.html`, `chat_row.html`, `chats_table.html`, `topic_row.html`, `topics_table.html`, `task_row.html`, `rules_list.html`, `templates_list.html`, `search_results.html`.

### 11.5 Bot (`bot/main.py`)

**Команды:** `/start`, `/help`, `/inbox`, `/tasks`, `/digest`.

**Callback-префиксы:** `done:`, `snooze:`, `archive:`, `task:`, `tdone:`.

**Ключевые helpers:**
- `_format_payload(conn, message_id)` — собирает text + inline-кнопки для одного сообщения
- `send_inbox_notification(bot, conn, cfg, message_id)` — публичный API: отправить в бот
- `_watcher_loop(bot, conn, cfg)` — фоновый цикл поллинга новых score≥3
- `_tg_deep_link(message_id, is_dm)` — обёртка над `app.links.telegram_deep_link` с fallback на WEB_BASE
- `register_handlers(bot, conn, cfg)` — все хендлеры
- `run_bot(conn, cfg)` — entrypoint

Отдельно: `bot/pin_help.py` — закреп инструкции + `SetBotCommandsRequest`.

### 11.6 БД — таблицы (актуальные)

`messages`, `context_links`, `topics`, `message_topics`, `priorities`, `user_actions`, `ingest_state`, `chats`, `grok_usage`, `pending_notifications`, `tasks`, `user_rules`, `reply_templates`, `outbox`, `classification_examples`, `messages_fts` (виртуальная FTS5).

**Триггеры:** `messages_ai/ad/au` — синхронизация FTS5 при insert/delete/update.

**Аддитивные миграции:** `app/db.py:ensure_columns()` — `chats.processing`, `topics.parent_id`, `user_rules`, `reply_templates`, `outbox`, `tasks`, `classification_examples`.

### 11.7 Classifier (`classifier/`)

| Файл | Что делает | Ключевые функции |
|---|---|---|
| `grok_worker.py` | Polling-воркер: берёт unscored messages → LLM → priorities + topics | `run_worker`, `classify_one`, `reclassify_one`, `_make_client` |
| `prompts.py` | Сборка системного промта (примеры + правила + top-темы + JSON-schema) | `build_system_prompt`, `fetch_top_topics`, `fetch_active_rules` |
| `topic_normalizer.py` | Fuzzy-маппинг slug/label → существующая тема (rapidfuzz, threshold ~85%) | `resolve_or_create` |
| `consolidator.py` | Ночной cleanup: hide dead topics (<3 msgs, >14d), suggest merges | `nightly_consolidate` |

### 11.8 Ingestion (`ingestion/`)

| Файл | Что делает | Ключевые функции |
|---|---|---|
| `telegram_listener.py` | Realtime: NewMessage / MessageEdited / MessageDeleted; захватывает ЛС, упоминания, ответы; вокруг target'а — ±N контекста | `run_listener`, `evaluate_and_capture`, класс `State` |
| `backfill.py` | Бэкфилл `backfill_days` назад per chat, устойчив к перезапуску через `ingest_state` | `run_backfill`, `backfill_chat` |
| `chats_sync.py` | Синхронизация диалогов в таблицу `chats`, помечает `archived` | `sync_chats`, `archived_chat_ids` |
| `context.py` | Подгрузка ±N сообщений вокруг target'а в `context_links` | `fetch_context`, `_insert_context_message` |

### 11.9 Notifications (`notifications/`)

| Файл | Что делает |
|---|---|
| `scheduler.py` | Snooze-sweeper (возврат отложенных), watcher новых высокоприоритетных, окно активных часов |
| `toast.py` | Windows-toast (graceful fallback если `windows-toasts` не установлен) |
| `tg_self.py` | Self-ЛС через user-сессию (Saved Messages) |
| `outbox_sender.py` | Поллинг `outbox`, отправка reply через Telethon user-сессию |

### 11.10 Shared utilities (`app/`)

| Файл | Публичный API |
|---|---|
| `db.py` | `connect()`, `init_db()`, `ensure_columns(conn)` |
| `config.py` | `load_config() -> Config`, dataclass `Config`, константы `DB_PATH`, `DATA_DIR`, `SCHEMA_PATH` |
| `links.py` | `telegram_deep_link(message_id, is_dm) -> str \| None` |
| `tasks.py` | `build_task_from_message(conn, mid)`, `create_task_from_message(conn, mid) -> int \| None` |
| `me.py` | `load_cached_me()`, `fetch_and_cache_me()` |
| `cli.py` | `main()` entrypoint + `cmd_*` функции |

### 11.11 PowerShell-launchers (корень)

- `setup.ps1` — `uv sync` + первичный логин
- `run.ps1` — `uv run python -m app.cli run`
- `web.ps1` — `uv run python -m app.cli web`
- `migrate.ps1` — миграции схемы (см. `MIGRATE.md`)

### 11.12 Тесты

**Нет.** Если буду добавлять — паттерн: `pytest` + `pytest-asyncio` + временная SQLite через `tmp_path`. Положить в `tests/`.

### 11.13 Skills

- `skills/nexustg-dev/SKILL.md` — рабочий процесс (карта + запуск + чек-листы)
- `skills/nexustg-lite/SKILL.md` — концепт лёгкого режима
- Junction `.claude/skills/nexustg-{dev,lite}` → `skills/nexustg-{dev,lite}` (gitignored, для активации Claude Code в этом проекте)

---

## 12. Cookbook — типовые правки

### Добавить новую веб-страницу

1. Создать `web/routes/<feature>.py` с `router = APIRouter()` и эндпоинтами (паттерн: `tasks.py`, `topics.py`)
2. Создать `web/templates/<feature>.html` (extends `base.html`) и partials в `partials/`
3. Зарегистрировать в `web/app.py:create_app()` — добавить в импорт и в список `include_router`
4. Добавить вкладку в `web/templates/base.html` (с `active=='<feature>'` маркером)
5. Если нужен `active` в шаблоне — передать в `TemplateResponse(... {"active": "<feature>"})`

### Добавить новую колонку в существующую таблицу

1. Дописать ALTER в `app/db.py:ensure_columns()` через `_table_columns` + `ADD COLUMN`
2. Обновить DDL в `app/schema.sql` (для свежих установок)
3. Перезапустить `app.cli run` или `web` — `init_db()` → `ensure_columns` сработает

### Добавить новую таблицу

1. `CREATE TABLE IF NOT EXISTS ...` в `app/db.py:ensure_columns()` + индексы
2. То же в `app/schema.sql`
3. Перезапуск

### Добавить команду в бота

1. В `bot/main.py:register_handlers` декоратор `@bot.on(events.NewMessage(pattern=r"^/yourcmd"))` + защитник `if not _allowed(event): return`
2. Если нужна inline-клавиатура — `Button.inline("...", b"prefix:payload")`, добавить ветку в `_cb` под `action == "prefix"`
3. Обновить `HELP_TEXT` и `COMMANDS` в `bot/pin_help.py`, запустить `uv run python -m bot.pin_help` для пере-закрепа

### Сменить LLM-провайдера

1. `config.toml`: `llm_base_url`, `grok_model`, `llm_input_usd_per_m`, `llm_output_usd_per_m`
2. `.env`: `XAI_API_KEY` (имя ключа исторически такое, переименовывать не надо — `app/config.py` его читает)
3. Перезапустить `app.cli run`. Формат — OpenAI-совместимый (Gemini / Grok / OpenAI / DeepSeek / любой compatible)

### Добавить HTMX-кнопку на карточку сообщения

1. В `web/templates/message.html` блок `<div id="msg-actions">` или `partials/message_row.html`
2. Паттерн:
```html
<button type="button" title="..."
        hx-post="/your-route/{{ target.id }}"
        hx-target="#msg-actions" hx-swap="outerHTML">🎯 Action</button>
```
3. Эндпоинт возвращает либо HTML-фрагмент, либо `Response(status_code=204)` с `HX-Redirect` для редиректа

### Изменить промт классификатора

1. `classifier/prompts.py:build_system_prompt()` — там собирается финальный промт
2. Динамические части: `fetch_top_topics()` (топ-10), `fetch_active_rules()` (правила «Алина»), последние 5 примеров из `classification_examples`
3. Реклассифицировать одно сообщение для проверки: кнопка «🔁 Переклассифицировать» в карточке `/message/{id}` или endpoint POST `/message/{id}/reclassify`

### Добавить CSV-экспорт

Паттерн в `web/routes/reports.py` (для `/export/messages.csv`). UTF-8 BOM + разделитель `;` для совместимости с Excel.

---

## 13. Производительность / лимиты

- **БД WAL-режим**: `app/db.py:connect()` → `PRAGMA journal_mode=WAL`, `busy_timeout=30000`. Можно параллельно читать пока пишет ingestion
- **FTS5 индекс**: триггеры `messages_ai/ad/au` поддерживают синхронизацию. Поиск по любым словам с unicode61 + `remove_diacritics 2`
- **Бюджет токенов**: `grok_daily_token_budget` (по умолчанию 20M). Учёт в `grok_usage(date, prompt_tokens, completion_tokens, cost_usd)`
- **Backfill** 30 дней при первом старте — `ingest_state.backfill_done` отмечает завершение per chat
- **Размер БД сейчас** (12.06): ~47 МБ, ~14k сообщений (см. `app.db`)

---

## 14. Связанные документы

- [`README.md`](../README.md) — публичное описание для GitHub
- [`GUIDE.md`](../GUIDE.md) — внутренний гайд по архитектуре
- [`REPORT.md`](../REPORT.md) — технический отчёт по фазам
- [`MIGRATE.md`](../MIGRATE.md) — миграции схемы
- [`REVIEW.md`](../REVIEW.md) — отзыв о курсе Zerocoder
- [`skills/nexustg-dev/SKILL.md`](../skills/nexustg-dev/SKILL.md) — скилл для Claude Code по работе с репо
- [`skills/nexustg-lite/SKILL.md`](../skills/nexustg-lite/SKILL.md) — концепт лёгкого режима
- Планы итераций: `C:\Users\User\.claude\plans\*.md`
- Этот журнал: `docs/PROJECT_JOURNAL.md` — обновлять при каждой значимой фиче (минимум: запись в §6 хронологии)
