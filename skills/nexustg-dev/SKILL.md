---
name: nexustg-dev
description: Use when working on the NexusTG repository — navigating modules, running the service, shipping changes, updating README, checking errors, preparing for publication. Triggers on requests like "запусти проект", "обнови readme nexustg", "подготовь к публикации", "почему не работает ingestion", "выложи изменения".
---

# nexustg-dev

Скилл-помощник по репозиторию **NexusTG** (локальный Telegram-агрегатор, Python 3.12 + uv + FastAPI + Telethon + SQLite).

## 1. Карта проекта

| Папка | Что внутри |
|---|---|
| `app/` | CLI (`app.cli`), config, db, `schema.sql` |
| `ingestion/` | Telethon-листенер, backfill, chats-sync, context ±5 |
| `classifier/` | LLM-воркер (Gemini/Grok), промты, нормализация тем, консолидация |
| `web/` | FastAPI + Jinja-шаблоны (`web/templates`) + статика (`web/static`) + роуты (`web/routes/*`) |
| `bot/` | Telegram-бот для inbox-уведомлений |
| `notifications/` | Windows toast, self-ЛС, планировщик, outbox-sender |
| `data/` | `app.db`, `tg.session`, `logs/` (gitignored) |
| `skills/` | Этот и связанные скиллы |
| `config.toml` | таймзона, окна, LLM-настройки |
| `.env` | секреты (gitignored) |

## 2. Запуск сервиса

Всё через PowerShell-launcher'ы. **Не редактировать** `.\setup.ps1` без причины — он же делает первичный логин.

```powershell
.\setup.ps1   # uv sync + login (один раз)
.\run.ps1     # ingestion + classifier + bot (если задан TG_BOT_TOKEN)
.\web.ps1     # http://127.0.0.1:8000
.\migrate.ps1 # миграции схемы
```

CLI-эквиваленты:
```powershell
uv run python -m app.cli {login|run|web|bot|backup}
```

Веб-сервер для теста: запускать в фоне (`Start-Process ... -WindowStyle Hidden`), пробить `Invoke-WebRequest http://127.0.0.1:8000/` со статусом 200. После теста — **обязательно остановить** процесс python.exe с `app.cli web`.

## 3. Внесение изменений

- Логика ingestion → `ingestion/telegram_listener.py`, `ingestion/backfill.py`, `ingestion/context.py`
- LLM-промты → `classifier/prompts.py`. Если меняешь — проверь, не сломал ли `topic_normalizer.py`
- Веб-роуты → `web/routes/<feature>.py`. Шаблон рядом: `web/templates/<feature>.html`
- Схема БД → `app/schema.sql` + миграция в `migrate.ps1`/`MIGRATE.md`
- Настройки → `config.toml` (никогда не хардкодить в коде то, что должно быть тут)

Workflow:
1. Прочитать соответствующий модуль и `GUIDE.md` при необходимости
2. Внести точечные правки (без рефакторингов без запроса)
3. Локально проверить (см. §4)
4. Закоммитить (`/commit` или вручную, осмысленное сообщение)

## 4. Проверка ошибок

Перед заявлением «работает»:

```powershell
# Синтаксис Python
uv run python -m compileall app ingestion classifier web bot notifications

# Импорты CLI
uv run python -c "from app import cli"

# Логи рантайма
Get-Content data\run.err.log -Tail 50
Get-Content data\run.log -Tail 80
Get-ChildItem data\logs\ | Sort LastWriteTime -Desc | Select -First 3
```

Если меняли веб — поднять `.\web.ps1` и потыкать соответствующий роут. Если ingestion — смотреть `data/run.err.log` после старта.

## 5. Обновление README

`README.md` секционирован:
- Возможности → Стек → Скриншоты → Требования → Быстрый старт → Конфигурация → CLI → Веб-интерфейс → Бот → Структура → Безопасность → Перенос → Статус → Лицензия

Правила:
- При добавлении нового веб-роута — обновить таблицу маршрутов
- При добавлении CLI-команды — добавить в раздел CLI
- При изменении `config.toml` — синхронизировать пример в README
- Скриншоты в `docs/screenshots/` **должны быть заблюрены** (gaussian blur radius ~7) перед коммитом, чтобы не утекли личные данные. Делается через `uv run --with Pillow python -c "..."`

## 6. Подготовка к публикации

Чек-лист перед `git push` в публичный репо:

- [ ] `.env`, `data/`, `backups/`, `.venv/`, `.claude/` в `.gitignore`
- [ ] `git status` — нет случайных файлов (`batch_out.txt`, временные дампы)
- [ ] `git diff --cached | grep -iE "(xai-[A-Za-z0-9]{10,}|AAH[A-Za-z0-9_-]{30,})"` — пусто
- [ ] Скриншоты заблюрены
- [ ] README актуален с фактическим состоянием кода
- [ ] Версия в `pyproject.toml` поднята при значимых изменениях
- [ ] Коммит в conventional-стиле (`feat:`, `fix:`, `docs:`, `refactor:`)
- [ ] `gh repo view sigurt33/NexusTG` доступен; пуш через `git push`

Если в скринах/доках видны имена/тексты переписки — **остановиться и спросить пользователя**, а не пушить.

## 7. Cookbook (типовые правки)

> Подробности и полный inventory — в `docs/PROJECT_JOURNAL.md` §11–12.

### + Новая веб-страница
1. `web/routes/<x>.py` с `router = APIRouter()` (паттерн: `tasks.py`)
2. `web/templates/<x>.html` extends `base.html` + partials в `partials/`
3. Зарегистрировать в `web/app.py:create_app()` (импорт + `include_router`)
4. Добавить вкладку в `web/templates/base.html` с `{% if active=='<x>' %}active{% endif %}`

### + Новая колонка / таблица
1. `app/db.py:ensure_columns()` — `_table_columns(...)` + `ALTER ADD COLUMN`, либо `CREATE TABLE IF NOT EXISTS`
2. Зеркально в `app/schema.sql` (для свежих установок)
3. Перезапуск `run` или `web` — `init_db()` применит

### + Команда в боте
1. `bot/main.py:register_handlers` — `@bot.on(events.NewMessage(pattern=r"^/x"))` + `if not _allowed(event): return`
2. Inline-кнопки: `Button.inline("...", b"prefix:payload")`, ветка в `_cb` под `action == "prefix"`
3. Обновить `bot/pin_help.py` (`HELP_TEXT`, `COMMANDS`) и запустить `uv run python -m bot.pin_help` (с `PYTHONIOENCODING=utf-8`)

### Сменить LLM-провайдера
`config.toml`: `llm_base_url`, `grok_model`, `llm_input_usd_per_m`, `llm_output_usd_per_m`. Ключ — в `.env:XAI_API_KEY` (имя историческое, не переименовывать). Формат — OpenAI-совместимый.

### Изменить промт классификатора
`classifier/prompts.py:build_system_prompt` — сборка финального промта. Динамика: `fetch_top_topics`, `fetch_active_rules`, последние 5 `classification_examples`. Проверка — кнопка «🔁 Переклассифицировать» на `/message/{id}` или POST `/message/{id}/reclassify`.

### HTMX-кнопка на карточке сообщения
В `web/templates/message.html` (блок `#msg-actions`) или `partials/message_row.html`:
```html
<button hx-post="/route/{{ target.id }}" hx-target="#msg-actions" hx-swap="outerHTML"
        title="Что делает">🎯 Текст</button>
```
Эндпоинт возвращает HTML-фрагмент либо `Response(204)` с `HX-Redirect`.

---

## 8. Что НЕ делать

- Не коммитить `data/tg.session` — это полный доступ к Telegram
- Не запускать `app.cli run` параллельно с уже работающим инстансом (отзовёт сессию)
- Не редактировать `uv.lock` руками
- Не добавлять зависимости без обновления `pyproject.toml`
- Не использовать `git add -A` без предварительного `git status`

---

## 9. С чего начать в новой сессии

1. Прочитать `docs/PROJECT_JOURNAL.md` целиком — там карта проекта, последние изменения, грабли, TODO
2. `git status && git log --oneline -10` — что есть локально и в репо
3. Проверить запущен ли сервис: `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? { $_.CommandLine -like "*app.cli*" }`
4. Если предстоит правка фичи — сначала прочитать соответствующий модуль (см. карту §11 журнала), потом редактировать
5. Лог сессии: записать ключевые изменения в журнал в §6 «Хронология»
