---
name: nexustg-lite
description: Use when the user wants the minimal NexusTG mode — capture only DMs, @mentions, and replies into SQLite. No classifier, no web UI, no bot, no toast, no context window, no backfill. Triggers on "лёгкий режим", "только инбокс", "без веба", "nexustg lite".
---

# nexustg-lite

Лёгкая форма NexusTG: голый ingestion-цикл, только три типа сообщений → SQLite. Ничего больше.

## Что включено

- ЛС (private chats)
- Упоминания вас в группах (`@username`, текстовые упоминания)
- Ответы на ваши сообщения

## Что выключено

- ❌ Веб-интерфейс (`web/`, `web.ps1`)
- ❌ LLM-классификатор (`classifier/`)
- ❌ Telegram-бот (`bot/`)
- ❌ Windows toast, self-ЛС, планировщик (`notifications/`)
- ❌ Контекст ±5 соседних сообщений
- ❌ Backfill за 30 дней
- ❌ Edit/delete-трекинг (опционально)
- ❌ Темы, отчёты, экспорт, правила, шаблоны

## Конфигурация `config.toml` (lite-режим)

```toml
timezone = "Europe/Minsk"

# Окно ingestion — не нужно, режим работает 24/7
# active_hours_start / active_hours_end — игнорировать

backfill_days  = 0       # без истории
context_window = 0       # без соседних сообщений

notify_windows_toast = false
notify_tg_self       = false
notify_tg_bot        = false

# LLM не используется — поля можно не трогать
```

## Минимальный `.env`

```
TG_API_ID=...
TG_API_HASH=...
# XAI_API_KEY, TG_BOT_TOKEN, TG_MY_ID — НЕ нужны в lite-режиме
```

## Запуск

```powershell
.\setup.ps1                              # один раз: uv sync + login
uv run python -m app.cli run --lite      # если флаг реализован
# или, если флага нет:
uv run python -m app.cli run             # с config.toml выше
```

Если флага `--lite` нет — он реализуется так:
1. В `app/cli.py` для `run`-команды добавить `--lite` → выставлять переменные:
   - не запускать `classifier.grok_worker`
   - не запускать `notifications/*`
   - не запускать `bot.main`
   - в `ingestion/telegram_listener.py` отключить захват контекста (`context_window=0` уже достаточно)
   - пропустить `ingestion/backfill.py` (если `backfill_days == 0`)

## Что должно появиться в БД

Только таблица `messages` со строками типов `dm | mention | reply`. Никаких `topics`, `classifications`, `notifications_outbox` записей.

## Проверка работоспособности

```powershell
# Запустить, подождать пару минут после ЛС/упоминания/ответа
uv run python -c "import sqlite3; c=sqlite3.connect('data/app.db'); print(c.execute('select kind, count(*) from messages group by kind').fetchall())"
```

Ожидаем: непустой результат с `dm`/`mention`/`reply`. Если есть только пустота — проверить `data/run.err.log`.

## Когда использовать lite вместо полного NexusTG

- Нужен только архив важных сообщений без UI
- Слабая машина / не хочется держать FastAPI + LLM-воркер
- Готовите экспорт в другой инструмент (Obsidian, Notion, свой бот)
- Тестируете изменения именно в ingestion-слое, не отвлекаясь на классификатор и веб

## Что НЕ делать

- Не оставлять включённым `notify_tg_self` в lite-режиме — иначе всё равно поднимется доп. клиент
- Не запускать `.\web.ps1` параллельно — это уже не lite
- Не путать с отключением листенера: lite ≠ выключенный сбор, lite = минимальный сбор
