# Build Prompt — Локальный агрегатор Telegram-задач

## Цель
Построить локальное Windows-приложение, которое собирает из ~40 Telegram-групп и ЛС все сообщения, требующие внимания пользователя (`@упоминания`, ответы на сообщения пользователя, любые ЛС), классифицирует их через xAI Grok по автонарастающим темам и приоритету, и показывает входящие задачи в веб-дашборде на русском языке с полнотекстовым поиском, snooze/done-действиями и десктоп+TG-уведомлениями в рабочие часы 10:00–18:30.

## Жёсткий стек (не менять без согласования)
- **Язык/рантайм:** Python 3.12, менеджер окружения — `uv`
- **MTProto-клиент:** Telethon (последняя стабильная)
- **БД:** SQLite в WAL-режиме + FTS5 (полнотекст), миграции через простой `schema.sql` (без Alembic на MVP)
- **LLM:** xAI Grok через `openai` Python SDK с `base_url="https://api.x.ai/v1"`, модель **`grok-4.1-fast`**, JSON Schema (`response_format`)
- **Веб:** FastAPI + Uvicorn + Jinja2 + HTMX + Pico.css. БЕЗ Node/npm
- **Все данные:** под `./data/` (`app.db`, `tg.session`, `logs/`) — ничего в `%APPDATA%`
- **Запуск:** `setup.ps1` (uv sync + первичный логин TG) и `run.ps1` (поднимает ingest+classifier+web)

## Конфиг (`.env.example` + `config.toml`)
`.env`: `TG_API_ID`, `TG_API_HASH`, `XAI_API_KEY`
`config.toml`: `timezone="Europe/Warsaw"` (уточнить у пользователя), `active_hours_start="10:00"`, `active_hours_end="18:30"`, `backfill_days=30`, `context_window=5`, `notify_windows_toast=true`, `notify_tg_self=true`, `ui_lang="ru"`, `grok_model="grok-4.1-fast"`, `grok_daily_token_budget=200000`

## Источники захвата (правила)
Сообщение попадает в инбокс если:
1. **DM** — любое входящее личное сообщение (включая контакты и не-контакты), флаг `is_dm=true`. Приоритет базы НЕ повышается за факт DM (равно с группами).
2. **Mention в группе** — `event.message.mentioned == True` ИЛИ entity типа `MessageEntityMention`/`MessageEntityMentionName` указывает на `me.id` ИЛИ regex `@<my_username>` найден в тексте (учитывать смену username). Флаг `is_mention=true`.
3. **Reply на моё сообщение** — `event.is_reply` и `(await event.get_reply_message()).sender_id == me.id`. Флаг `is_reply_to_me=true`.

Захватываем все группы, в которых состоит пользователь (без allow-list); фильтрация — только по трём правилам выше. Channels (broadcast) — пропускать, только мегагруппы/группы/ЛС.

## Контекст вокруг сообщения
Для каждого захваченного сообщения тянуть ±5 соседних сообщений из того же чата (через `client.iter_messages(chat, offset_id=msg.id, limit=5)` в обе стороны), сохранять в `context_links(message_id, context_msg_id, position)`. Сами контекстные сообщения сохранять в `messages` отдельной записью с пометкой `is_context_only=true` если они не попадают под правила 1–3.

## Данные (schema.sql)
- `messages(id, chat_id, chat_title, sender_id, sender_name, text, date_utc, reply_to_id, is_dm, is_mention, is_reply_to_me, is_context_only, edited_at, deleted_at, raw_json, created_at)` — `id` = `"{chat_id}:{msg_id}"`
- `context_links(message_id, context_msg_id, position)` — position от -5 до +5, 0 = сам инбокс
- `topics(id, slug, label_ru, description, embedding BLOB NULL, created_at, message_count, hidden)` — авто-нарастают
- `message_topics(message_id, topic_id, confidence)` — N тем на сообщение
- `priorities(message_id PK, urgency 1-5, importance 1-5, score, rationale, classified_at, model_version)` — `score = urgency*0.6 + importance*0.4` (нормализованный)
- `user_actions(id, message_id, action ENUM(done|snoozed|archived|unread), snooze_until, created_at)`
- `messages_fts` — FTS5 virtual table над `text + chat_title + sender_name`, tokenize `unicode61 remove_diacritics 2`, синхронизация триггерами INSERT/UPDATE/DELETE
- `ingest_state(chat_id PK, last_backfilled_id, last_seen_id, updated_at)`
- `grok_usage(date PK, prompt_tokens, completion_tokens, cost_usd)` — для дневного бюджета

Индексы: `(date_utc DESC)`, `(chat_id, date_utc)`, `(is_dm)`, `(is_mention)`.

## Ingestion-сервис (`ingestion/telegram_listener.py`)
- Один Telethon-клиент, asyncio-loop, сессия в `data/tg.session`
- При старте: `iter_dialogs()` → определить все диалоги (DM + группы), пропустить broadcast-каналы
- **Backfill (первый запуск):** для каждого чата идти назад `iter_messages(chat, offset_date=now-30d)`, фильтровать по правилам 1–3, сохранять. Между чатами `asyncio.sleep(1)`, ловить `FloodWaitError` — `await asyncio.sleep(e.seconds + 1)`. Прогресс пишем в `ingest_state.last_backfilled_id`. Backfill устойчив к перезапуску.
- **Realtime:** хендлеры на `events.NewMessage`, `events.MessageEdited`, `events.MessageDeleted`. Edit → дописать в `raw_json.edits[]`, обновить `text` и `edited_at`. Delete → soft-delete (`deleted_at=now`), запись не удаляем.
- На захвате — синхронно вытянуть ±5 контекста и положить в `context_links`
- Кешировать `get_entity` в локальном dict, чтобы не дёргать сервер

## Classifier worker (`classifier/grok_worker.py`)
- Polling каждые 10 сек: выбрать до 10 строк где нет записи в `priorities` И `is_context_only=false`
- Для каждой группы из 1–5 сообщений (батч по одному чату/треду) — один вызов Grok
- **Prompt** (system, кешируется):
  - Роль: «Классификатор задач из Telegram. Возвращаешь строгий JSON по схеме.»
  - Список существующих тем (top-50 по `message_count` за 30 дней): `slug | label_ru | description`
  - Инструкция: «Выбери 1–3 темы из списка ИЛИ предложи новую (укажи `is_new=true`, дай `slug` snake_case, `label_ru`, `description`). Оцени urgency (1-5) и importance (1-5). Дай rationale на русском, 1 строка.»
- **JSON Schema** через `response_format={"type":"json_schema","json_schema":{...}}`:
  ```json
  {
    "topics": [{"slug":"...", "label_ru":"...", "is_new": false, "description":"..."}],
    "urgency": 3, "importance": 4, "rationale": "..."
  }
  ```
- Подавать message.text + контекст (±2 из ±5, чтобы не раздувать) + chat_title + sender_name
- **Нормализация тем:** перед вставкой нового топика — `rapidfuzz.fuzz.token_set_ratio` на label_ru против существующих, threshold 85 → reuse существующий; иначе INSERT новый
- Лимиты: на старте дня сбрасывать счётчик в `grok_usage`; если >80% бюджета — флаг `degraded_mode` (только классификация DM/mention, replies в очереди)
- Backoff: 429 → exp backoff 2,4,8,16,60 сек, max 5 ретраев, потом отложить на 5 мин

**Ночной consolidator (03:00 локально):**
- Найти топики с `message_count < 3` за 30 дней и `created_at < 14d` назад → пометить `hidden=true`
- Найти пары топиков с похожими label_ru (fuzz > 90) → залогировать в `data/logs/merge_suggestions.log` (без авто-merge на MVP, выбор за пользователем через topic admin UI)

## Web (`web/app.py`)
- FastAPI на `127.0.0.1:8000`, без авторизации (локально)
- Шаблоны Jinja2 в `web/templates/`, статика — Pico.css + htmx.min.js (положить локально, не CDN)
- **Маршруты:**
  - `GET /` — инбокс: сортировка по `priorities.score DESC, date_utc DESC`, по умолчанию скрыты `done`/`archived`/активный `snoozed`. Фильтры (через HTMX): topic chips, источник (DM/mention/reply), чат, диапазон дат
  - `GET /message/{id}` — деталь сообщения с раскрытым контекстом ±5, кнопками действий, deep-link `tg://openmessage?chat_id=...&message_id=...`
  - `GET /search?q=...` — FTS5 `MATCH`, `bm25()` ranking, `snippet()` для подсветки
  - `POST /actions/{message_id}` — body: `action`, `snooze_until?`. Пресеты snooze: **1ч / до завтра 10:00 / до понедельника 10:00 / своё (datetime-local)**
  - `GET /topics` — admin: переименовать (label_ru), скрыть, merge (выбрать src→dst, обновить `message_topics.topic_id`)
  - `GET /digest` — сводка за вчера: топ-20 по score, группировка по темам
  - `GET /health` — лаг ingest, последний classified_at, сегодняшний расход Grok
- HTMX везде — без full page reload. Язык интерфейса — русский.

## Snooze sweeper
В web-процессе фоновая `asyncio.create_task` петля каждые 60 сек: `UPDATE user_actions SET action='unread' WHERE action='snoozed' AND snooze_until <= now()`. После апа — пушнуть toast/TG-уведомление.

## Уведомления (10:00–18:30 local)
- При появлении нового inbox-сообщения с `priority.score >= 3.0`:
  - **Windows toast** через `windows-toasts` (актуальный форк `winrt-Windows.UI.Notifications`); клик открывает `http://127.0.0.1:8000/message/{id}`
  - **TG self-ping** через тот же Telethon-клиент в "Saved Messages" (`client.send_message('me', ...)`) с превью: автор/чат/первые 200 символов + ссылка `http://127.0.0.1:8000/message/{id}`
- Вне активных часов — копить, при наступлении 10:00 отправить один сводный toast/TG-сообщение «N новых задач»

## Портативность (перенос на другой ПК)
1. Скопировать всю папку проекта (включая `data/`)
2. Установить uv: `winget install astral-sh.uv` (или `irm https://astral.sh/uv/install.ps1 | iex`)
3. `pwsh setup.ps1` → проверит наличие `data/tg.session` → если нет, запросит логин (phone + код)
4. `pwsh run.ps1` → стартует ingest+classifier+web
5. Документировать: одновременно НЕ запускать на двух ПК (TG отзовёт сессию)

## Скрипты
- `setup.ps1`: проверка uv → `uv sync` → если нет `data/tg.session` → запуск интерактивного логина (`python -m app.cli login`)
- `run.ps1`: запускает `python -m app.cli run` который через `asyncio.gather` крутит ingestion + classifier + uvicorn в одном процессе (так проще управлять; для прод-варианта позже разнесём)
- `python -m app.cli backup` → zip `data/` в `backups/data_YYYYMMDD_HHMM.zip`
- `python -m app.cli reclassify --topic=<slug>` → пересчитать темы для сообщений топика (после merge/rename)

## Структура проекта
```
aichatpom/
  pyproject.toml         # uv-based, deps: telethon, openai, fastapi, uvicorn, jinja2, htmx (статикой), pico-css (статикой), rapidfuzz, windows-toasts, python-dotenv, tomli, aiosqlite
  uv.lock
  .env.example
  config.toml
  setup.ps1
  run.ps1
  README.md              # на русском, с инструкцией переноса
  data/                  # gitignore: db, session, logs
  app/
    __init__.py
    cli.py               # entry: login | run | backup | reclassify
    config.py            # загрузка .env + config.toml
    db.py                # SQLite connection, schema apply, FTS triggers
    schema.sql
  ingestion/
    telegram_listener.py
    backfill.py
    context.py
  classifier/
    grok_worker.py
    prompts.py
    topic_normalizer.py
    consolidator.py      # ночной job
  web/
    app.py               # FastAPI app factory
    routes/
      inbox.py
      message.py
      search.py
      actions.py
      topics.py
      digest.py
      health.py
    templates/
      base.html
      inbox.html
      message.html
      search.html
      topics.html
      digest.html
      partials/...
    static/
      pico.min.css
      htmx.min.js
      app.css
  notifications/
    toast.py
    tg_self.py
    scheduler.py         # snooze sweeper + active-hours gate
```

## Что НЕ делать на MVP
- Не делать миграции БД (пересоздавать `data/app.db` если меняется схема — данные дешёвые на старте)
- Не делать мультипользовательский режим / авторизацию
- Не интегрировать Notion/Todoist
- Не делать Docker — только нативный запуск
- Не делать embeddings-кластеризацию (только LLM-as-classifier + fuzzy merge label_ru)
- Не делать тесты сверх минимума: smoke-test ingest на одном чате + unit-тест на topic_normalizer

## Definition of Done по фазам

**Phase 1 (Ingest+Store):** `setup.ps1` логинит, `run.ps1` стартует, через 5 мин в `data/app.db` есть строки в `messages` из реальных чатов, edit/delete отражаются, backfill 30 дней завершается без падений.

**Phase 2 (Classifier):** все строки получают `priorities` и `message_topics` в течение 30 сек после ingest. Таксономия за неделю содержит 10–30 осмысленных тем на русском. Дневной бюджет соблюдается.

**Phase 3 (Web):** `http://127.0.0.1:8000` показывает инбокс на русском, фильтры/поиск работают, контекст разворачивается, deep-link открывает TG.

**Phase 4 (Actions):** done/snooze/archive работают, snooze возвращает сообщения, toast+TG-уведомления приходят только в 10:00–18:30, `/digest` показывает вчерашнее, `backup` создаёт zip.

## Открытые мелочи к уточнению при реализации
- Точный часовой пояс пользователя (для active-hours и `date_utc → local`)
- TG `api_id`/`api_hash` пользователь получит на my.telegram.org → положит в `.env`
- Если grok-4.1-fast вернёт нестабильный JSON — fallback на tool-calling режим с тем же schema

---
Это финальный билд-промт. Реализовывать строго по фазам, в конце каждой — самопроверка по DoD. Все строки UI и LLM-prompts — на русском.
