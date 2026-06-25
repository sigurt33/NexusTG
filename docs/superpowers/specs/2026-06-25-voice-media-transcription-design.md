# NexusTG — Транскрипция голосовых и обработка медиа

**Дата:** 2026-06-25
**Статус:** одобрено пользователем, готово к плану

## Контекст и цель

Сейчас голосовые и медиа захватываются с пустым `text` → классифицируются как «тривиальные» (score 1.0), не вызывают уведомлений, фактически «слепые». Нужно:

- **Голосовые ≤3 мин** — транскрибировать через Gemini, подписать «это транскрипция голосового», дальше обрабатывать как обычный текст.
- **Голосовые/аудио >3 мин** — не транскрибировать, подписать «голосовое >3 мин, прослушай», приоритет 5.
- **Прочее медиа** (фото/видео/документ) — приоритет 5, подписать что есть медиа (по ссылке пользователь смотрит сам).
- **Стикеры/GIF/анимации** — тривиальные (низкий приоритет, без пуша).
- **Медиа с подписью (caption)** — классифицировать по подписи как обычный текст.

Стек: Python 3.12, Telethon (user-сессия), aiosqlite, существующий OpenAI-совместимый клиент Gemini (`generativelanguage.googleapis.com/v1beta/openai/`, ключ `XAI_API_KEY`). Тестов нет — проверка `compileall` + assert-скрипты + живой E2E.

## Архитектурное решение

Классификатор (`classifier/grok_worker.run_worker`) получает только `conn`+`cfg` — **без Telethon-клиента**. Скачивание медиа требует клиента, который есть у листенера/`cmd_run`. Поэтому:

- **Новый воркер** `ingestion/media.py: run_media_worker(client, conn, cfg)` — регистрируется в `app/cli.py:cmd_run` рядом с `run_worker` (у него есть `client`). Поллит очередь медиа в БД каждые ~15с. **Restart-safe** (очередь `media_status='pending'` переживает рестарт).

## Поток данных

### 1. Захват (листенер, `ingestion/telegram_listener.py`)
В `_capture_message` определяем тип медиа из объекта Telethon и пишем в новые поля:
- `media_kind`: одно из `voice|video_note|audio|photo|video|document|sticker|gif` или `NULL` (чистый текст).
- `media_duration`: длительность в секундах для voice/video_note/audio (из `message.file.duration`), иначе `NULL`.
- `media_status`: `'pending'` если `media_kind` определён, иначе `NULL`.

Детект (Telethon convenience-свойства, проверять в порядке): `message.voice`→voice, `message.video_note`→video_note, `message.audio`→audio, `message.sticker`→sticker, `message.gif`→gif, `message.photo`→photo, `message.video`→video, `message.document`→document.

Текст остаётся `message.message or ""` (для медиа с подписью — это и есть caption).

### 2. Классификатор (`classifier/grok_worker.py`)
`_pending_messages` добавляет условие `AND COALESCE(m.media_status,'') <> 'pending'` — не трогает медиа, пока медиа-воркер не доведёт его до `'done'`.

### 3. Медиа-воркер (`ingestion/media.py`)
Поллит `SELECT ... FROM messages m LEFT JOIN chats c ... WHERE m.media_status='pending' AND m.deleted_at IS NULL AND COALESCE(c.archived,0)=0 AND COALESCE(c.processing,1)=1 ORDER BY date_utc DESC LIMIT N`. Для каждого — решение по таблице:

**Порядок проверок в коде строго такой (первое совпадение выигрывает):**

| # | Условие | text | Приоритет (u/i/score) | Дальше |
|---|---|---|---|---|
| 1 | voice/video_note/audio, `duration ≤ max` (3 мин) | `🎤 Транскрипция голосового сообщения:\n\n<transcript>` (+ `\n\nПодпись: <caption>` если есть) | — | `status='done'`, **не скорим** → классификатор |
| 2 | voice/video_note/audio, `duration > max` | `🎤 Голосовое сообщение длиннее 3 минут — прослушай в Telegram` | 5/5/5.0 | `status='done'`, **форс-скор**, без LLM |
| 3 | sticker/gif | (как тривиал) | 1/1/1.0 | `status='done'`, тривиал-топик, без LLM |
| 4 | photo/video/document, caption непустой | `📎 [медиа: <kind>] <caption>` | — | `status='done'`, **не скорим** → классификатор |
| 5 | photo/video/document, без caption | `📎 <Тип> — открой в Telegram, чтобы посмотреть` | 5/5/5.0 | `status='done'`, **форс-скор**, без LLM |
| — | ошибка скачивания/транскрипции (в ветке 1) | `🎤 Не удалось расшифровать — прослушай в Telegram` | 5/5/5.0 | `status='error'`, форс-скор, без повторов |

Голос/аудио транскрибируется **независимо** от наличия caption (caption дописывается в конец). Caption-ветка (4) — только для photo/video/document.

**Форс-скор** — вставка в `priorities` (urgency, importance, score, rationale=`media: <kind>`, model_version=`media-prefilter`, classified_at=now) + назначение системного топика `media` (хелпер `_media_topic_id`, по аналогии с `_trivial_topic_id`). Стикеры/GIF идут через топик `trivial`.

**Скачивание/транскрипция** только для short-voice без caption: `msg = await client.get_messages(chat_id, ids=msg_id)` → `data = await client.download_media(msg, file=bytes)` → `transcribe_audio(...)`.

### Транскрипция (`ingestion/media.py:transcribe_audio`)
Через `AsyncOpenAI` (как в `grok_worker._make_client`), запрос с content-частью:
```python
{"type": "input_audio", "input_audio": {"data": base64(data), "format": "ogg"}}
```
+ текстовая инструкция «Расшифруй это голосовое сообщение дословно, на языке оригинала. Верни только текст.».
Telegram voice = OGG/Opus. **На этапе реализации тестом проверить**, что Gemini принимает `format:"ogg"`; если отклонит — переключиться на нативный `POST .../v1beta/models/<model>:generateContent` с `inline_data{mime_type:"audio/ogg"}` (ogg поддерживается нативно), через `httpx`. Без ffmpeg/транскодинга.
Расход токенов из `resp.usage` писать в `grok_usage` (как в классификаторе). Лимит inline ~20MB — голос ≤3 мин укладывается с запасом.

### Конфиг
`config.toml`: `voice_transcribe_max_minutes = 3`. В `app/config.py` — поле `voice_transcribe_max_minutes: int` (default 3). Опционально позже — на вкладку «Настройки» (вне скоупа этой итерации).

## Изменения схемы
`messages` + три аддитивные колонки: `media_kind TEXT`, `media_status TEXT`, `media_duration INTEGER`. В `app/schema.sql` (для свежих установок) и `app/db.py:ensure_columns` (ALTER для существующих). Индекс `idx_messages_media_pending ON messages(media_status) WHERE media_status='pending'` для дешёвого поллинга. Старые строки (NULL) считаются не-медиа — не реобрабатываются.

## Файлы

| Файл | Изменение |
|---|---|
| `app/schema.sql` | 3 колонки + индекс |
| `app/db.py` (`ensure_columns`) | миграция колонок + индекс |
| `app/config.py`, `config.toml` | `voice_transcribe_max_minutes` |
| `ingestion/telegram_listener.py` | детект media_kind/duration + `media_status='pending'` в `_capture_message` |
| `ingestion/media.py` | **новый**: `run_media_worker`, `transcribe_audio`, `_decide`, форс-скор + топик-хелперы |
| `app/cli.py` (`cmd_run`) | `asyncio.create_task(run_media_worker(client, conn, cfg))` + отмена в finally |
| `classifier/grok_worker.py` | `_pending_messages`: исключить `media_status='pending'` |
| `docs/PROJECT_JOURNAL.md` | запись в §3 (поля), §6 (хроника) |

## Обработка ошибок
- Не удалось скачать/транскрибировать → текст-метка + force score 5 + `media_status='error'`. Без бесконечных ретраев (одна попытка; `'error'` исключается из `pending`-выборки).
- Транскрипция вернула пусто → трактуем как ошибку (метка + score 5).
- `get_messages`/download исключения логируются `log.warning`, воркер не падает (try/except на каждый элемент).
- Воркер не блокирует листенер (отдельная корутина); конкурентность транскрипций ограничить `asyncio.Semaphore(2)`.

## Проверка (нет pytest)
- `uv run python -m compileall -q app ingestion classifier web bot notifications`
- `uv run python -c "from app import cli; from ingestion import media"`
- Assert-скрипт: `_decide(...)` возвращает правильную ветку для каждого kind/duration/caption.
- Тест ogg-транскрипции на реальном файле (на этапе реализации).
- **Живой E2E**: пользователь шлёт себе короткое голосовое → в инбоксе появляется «🎤 Транскрипция…» с классификацией; длинное голосовое → метка + score 5.

## Вне скоупа
- Реобработка уже захваченных (старых) голосовых/медиа.
- Вынос `voice_transcribe_max_minutes` на вкладку «Настройки».
- Хранение скачанных аудиофайлов (скачиваем во временный буфер, не сохраняем).
- Транскрипция видео-дорожки обычных видео (только voice/video_note/audio).
