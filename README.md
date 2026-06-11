# NexusTG

Локальный агрегатор сообщений Telegram, требующих внимания (@упоминания, ответы вам, ЛС). Phase 1 — только сбор и хранение в SQLite. Классификация (Grok), веб-интерфейс и уведомления — следующие фазы.

## Phase 5 — новые возможности

- `/chats` — список чатов и переключатель обработки (allow/block).
- `/reports`, `/reports/weekly`, `/reports/topics`, `/reports/chats`, `/reports/senders` — отчёты и сводки.
- `/export/messages.csv?from=&to=&topic=&chat=&min_score=`, `/export/topics.csv` — CSV-экспорт (UTF-8 BOM, разделитель `;`).
- `/topics?view=tree` — иерархия тем; bulk-merge, set-parent, история сообщений темы.
- `/message/{id}/reclassify`, `/message/{id}/priority`, `/message/{id}/save-example` — переклассификация, ручная правка u/i, сохранение примера для промта.
- `/examples` — управление обучающими примерами (последние 5 подмешиваются в системный промт).
- TG-бот: создайте через @BotFather (`/newbot`), положите токен в `.env` (`TG_BOT_TOKEN=...`) и ваш user_id в `TG_MY_ID=...` (его подскажет @userinfobot). Запуск только бота: `uv run python -m app.cli bot`. В `run`-режиме бот стартует автоматически, если токен задан.
- В `config.toml` добавлен `notify_tg_bot = false` (включите при желании).

## Что это
- Один Telethon-клиент слушает все ваши чаты (кроме broadcast-каналов).
- Захватываются три типа сообщений: ЛС, упоминания вас в группах, ответы на ваши сообщения.
- Вокруг каждого захваченного сообщения сохраняются ±5 соседних сообщений как контекст.
- Edit/delete отслеживаются: правки добавляются в `raw_json.edits[]`, удаления — soft-delete (`deleted_at`).
- Backfill за последние 30 дней при первом запуске, устойчив к перезапуску.
- Всё под `./data/` (`app.db`, `tg.session`, `logs/`). Ничего не пишется в `%APPDATA%`.

## Требования
- Windows 11, PowerShell 5.1+
- Python 3.12 (поставит `uv`)
- `uv` — менеджер окружения: `winget install astral-sh.uv` или `irm https://astral.sh/uv/install.ps1 | iex`
- Telegram `api_id` и `api_hash` — получить на https://my.telegram.org

## Установка
```powershell
# 1. Заполнить .env
copy .env.example .env
# Открыть .env и вписать TG_API_ID, TG_API_HASH (XAI_API_KEY понадобится позже)

# 2. Запустить установщик (uv sync + первичный логин)
.\setup.ps1
# Введите телефон (+...) и код из Telegram. При 2FA — пароль.

# 3. Запустить сбор
.\run.ps1
```

После 5 минут работы проверьте: в `data/app.db` должны появиться строки в таблице `messages`.

## Перенос на другой ПК
1. Скопировать целиком папку проекта **вместе с `data/`** (там сессия и БД).
2. Поставить `uv` (см. выше).
3. Запустить `.\setup.ps1` — он увидит существующую сессию и не будет спрашивать код.
4. `.\run.ps1`.

**ВАЖНО:** не запускайте сервис одновременно на двух ПК с одной сессией — Telegram её отзовёт, придётся логиниться заново.

## Безопасность сессии
- Файл `data/tg.session` — это полный доступ к вашему Telegram. Не коммитить в git (он уже в `.gitignore`), не выкладывать в общий доступ.
- `.env` с API-ключами тоже игнорируется git'ом.
- Бэкап: `uv run python -m app.cli backup` → создаст `backups/data_YYYYMMDD_HHMM.zip`.

## Команды CLI
```powershell
uv run python -m app.cli login    # вход в Telegram
uv run python -m app.cli run      # ingestion (backfill + realtime)
uv run python -m app.cli backup   # zip-бэкап data/
```

## Структура (Phase 1)
```
NexusTG/
  app/         — config, db, cli, schema.sql
  ingestion/   — telegram_listener, backfill, context
  data/        — app.db, tg.session, logs/ (gitignored)
  config.toml  — таймзона, окно бэкфилла и т.п.
  .env         — секреты (gitignored)
```
