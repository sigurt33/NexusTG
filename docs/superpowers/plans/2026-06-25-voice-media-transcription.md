# Voice Transcription + Media Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transcribe short Telegram voice messages via Gemini and label/prioritise other media, so voice/media stop being invisible in the inbox.

**Architecture:** A new `run_media_worker` (in `ingestion/media.py`) runs inside `cmd_run` (which owns the Telethon client). The listener tags media rows `media_status='pending'`; the worker drains the queue — transcribing short voice via the existing Gemini OpenAI-compatible client, force-scoring long voice / photos / videos / documents, and trivial-scoring stickers/GIFs. The classifier skips `pending` rows.

**Tech Stack:** Python 3.12, Telethon, aiosqlite, `openai` AsyncOpenAI → Gemini, base64, httpx (fallback only).

**Verification convention:** No pytest. Verify with `compileall`, assert-scripts via `uv run python` (temp files under `data/`, deleted after), and a live E2E voice test. Code uses Russian comments/strings to match the codebase.

---

### Task 1: Schema + migration for media columns

**Files:**
- Modify: `app/schema.sql`
- Modify: `app/db.py` (`ensure_columns`)

- [ ] **Step 1: Add columns to schema.sql**

In `app/schema.sql`, find `CREATE TABLE IF NOT EXISTS messages (...)`. Add these columns before the closing `)` (after the last existing column, fix the trailing comma so SQL stays valid):
```sql
    media_kind     TEXT,
    media_status   TEXT,
    media_duration INTEGER
```
After the messages `CREATE TABLE`/its indexes, add:
```sql
CREATE INDEX IF NOT EXISTS idx_messages_media_pending ON messages(media_status) WHERE media_status='pending';
```

- [ ] **Step 2: Add additive migration in db.py**

In `app/db.py:ensure_columns`, near where other message columns are guarded (search for `_table_columns(conn, "messages")` or the messages block), add:
```python
    mcols = await _table_columns(conn, "messages")
    if "media_kind" not in mcols:
        await conn.execute("ALTER TABLE messages ADD COLUMN media_kind TEXT")
    if "media_status" not in mcols:
        await conn.execute("ALTER TABLE messages ADD COLUMN media_status TEXT")
    if "media_duration" not in mcols:
        await conn.execute("ALTER TABLE messages ADD COLUMN media_duration INTEGER")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_media_pending ON messages(media_status) WHERE media_status='pending'"
    )
```
(Match the exact async/aiosqlite style of the surrounding guards.)

- [ ] **Step 3: Apply + verify**

```
uv run python -m app.cli backup
uv run python -c "import asyncio; from app.db import init_db; asyncio.run(init_db())"
```
Write `data/_chk.py`:
```python
import sqlite3
cols = [r[1] for r in sqlite3.connect('data/app.db').execute("PRAGMA table_info(messages)")]
for c in ("media_kind","media_status","media_duration"):
    assert c in cols, (c, cols)
print("OK media columns present")
```
Run `uv run python data\_chk.py` → `OK media columns present`. Delete `data/_chk.py`.

- [ ] **Step 4: Commit**

```
git add app/schema.sql app/db.py
git commit -m "feat(db): media_kind/media_status/media_duration on messages"
```

---

### Task 2: Config — `voice_transcribe_max_minutes`

**Files:**
- Modify: `config.toml`
- Modify: `app/config.py`

- [ ] **Step 1: config.toml**

Append to `config.toml`:
```toml
voice_transcribe_max_minutes = 3
```

- [ ] **Step 2: Config dataclass + loader**

In `app/config.py`, add to `@dataclass Config` (after `task_reminder_hours_before: int`):
```python
    voice_transcribe_max_minutes: int
```
In `load_config()` return (after the `task_reminder_hours_before=...` line):
```python
        voice_transcribe_max_minutes=int(toml_cfg.get("voice_transcribe_max_minutes", 3)),
```

- [ ] **Step 3: Verify + commit**

```
uv run python -c "from app.config import load_config; print(load_config().voice_transcribe_max_minutes)"
```
Expected: `3`.
```
git add config.toml app/config.py
git commit -m "feat(config): voice_transcribe_max_minutes"
```

---

### Task 3: Listener tags media at capture

**Files:**
- Modify: `ingestion/telegram_listener.py`

- [ ] **Step 1: Add a media-detection helper**

In `ingestion/telegram_listener.py`, add near the top-level helpers (e.g. after `msg_id`):
```python
def _media_info(message: Any) -> tuple[str | None, int | None]:
    """Определить тип медиа и длительность (сек). Порядок важен: voice — это тоже document."""
    kind = None
    if getattr(message, "voice", None):
        kind = "voice"
    elif getattr(message, "video_note", None):
        kind = "video_note"
    elif getattr(message, "audio", None):
        kind = "audio"
    elif getattr(message, "sticker", None):
        kind = "sticker"
    elif getattr(message, "gif", None):
        kind = "gif"
    elif getattr(message, "photo", None):
        kind = "photo"
    elif getattr(message, "video", None):
        kind = "video"
    elif getattr(message, "document", None):
        kind = "document"
    duration = None
    if kind in ("voice", "video_note", "audio", "video"):
        f = getattr(message, "file", None)
        if f is not None and getattr(f, "duration", None):
            duration = int(f.duration)
    return kind, duration
```

- [ ] **Step 2: Write media fields on INSERT**

In `_capture_message`, just before the `await conn.execute("""INSERT INTO messages ...` call, compute:
```python
    media_kind, media_duration = _media_info(message)
    media_status = "pending" if media_kind else None
```
Then change the INSERT statement to include the three columns. Replace the existing INSERT (columns list + VALUES + ON CONFLICT) with:
```python
    await conn.execute(
        """
        INSERT INTO messages
        (id, chat_id, chat_title, sender_id, sender_name, text, date_utc, reply_to_id,
         is_dm, is_mention, is_reply_to_me, is_context_only, raw_json,
         media_kind, media_status, media_duration)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            text=excluded.text,
            is_dm=excluded.is_dm,
            is_mention=excluded.is_mention,
            is_reply_to_me=excluded.is_reply_to_me,
            is_context_only=0,
            raw_json=excluded.raw_json,
            media_kind=excluded.media_kind,
            media_duration=excluded.media_duration
        """,
        (mid, chat_id, chat_title, sender_id, sender_name, message.message or "",
         date_utc, reply_to_id, int(is_dm), int(is_mention), int(is_reply_to_me),
         _serialize(message), media_kind, media_status, media_duration),
    )
```
Note: ON CONFLICT deliberately does NOT overwrite `media_status` (so an edit doesn't re-queue an already-processed item).

- [ ] **Step 3: Verify detection logic in isolation**

Write `data/_chk.py`:
```python
from ingestion.telegram_listener import _media_info

class F:  # fake .file
    def __init__(self, d): self.duration = d
class M:  # fake message
    voice=video_note=audio=sticker=gif=photo=video=document=None
    file=None

m = M(); m.voice = object(); m.file = F(42)
assert _media_info(m) == ("voice", 42), _media_info(m)
m2 = M(); m2.photo = object()
assert _media_info(m2) == ("photo", None), _media_info(m2)
m3 = M()  # pure text
assert _media_info(m3) == (None, None), _media_info(m3)
m4 = M(); m4.document = object()
assert _media_info(m4) == ("document", None)
print("OK media detection")
```
Run `uv run python data\_chk.py` → `OK media detection`. Delete it. Then `uv run python -m compileall -q ingestion`.

- [ ] **Step 4: Commit**

```
git add ingestion/telegram_listener.py
git commit -m "feat(ingest): tag media kind/duration/status at capture"
```

---

### Task 4: Classifier skips pending media

**Files:**
- Modify: `classifier/grok_worker.py`

- [ ] **Step 1: Exclude pending in `_pending_messages`**

In `classifier/grok_worker.py:_pending_messages`, add a WHERE condition. Change the query's WHERE block to include:
```python
          AND COALESCE(m.media_status, '') <> 'pending'
```
Place it alongside the existing `AND m.is_context_only = 0` etc. (within the same WHERE). The full condition list becomes:
```
WHERE m.is_context_only = 0
  AND m.deleted_at IS NULL
  AND COALESCE(c.archived, 0) = 0
  AND COALESCE(c.processing, 1) = 1
  AND COALESCE(m.media_status, '') <> 'pending'
  AND m.id NOT IN (SELECT message_id FROM priorities)
```

- [ ] **Step 2: Verify + commit**

```
uv run python -m compileall -q classifier
uv run python -c "from classifier import grok_worker"
```
Both clean.
```
git add classifier/grok_worker.py
git commit -m "feat(classifier): skip pending media until media worker handles it"
```

---

### Task 5: Media worker module

**Files:**
- Create: `ingestion/media.py`

- [ ] **Step 1: Create the module**

Create `ingestion/media.py` with EXACTLY:
```python
"""Медиа-воркер: транскрипция голосовых (Gemini) + форс-приоритет для остального медиа."""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import aiosqlite
from openai import AsyncOpenAI
from telethon import TelegramClient

from classifier.grok_worker import _record_usage, _trivial_topic_id

log = logging.getLogger(__name__)

POLL_SLEEP = 15
BATCH = 20
TRANSCRIBE_CONCURRENCY = 2

VOICE_KINDS = {"voice", "video_note", "audio"}
TRIVIAL_KINDS = {"sticker", "gif"}
KIND_LABELS = {
    "voice": "Голосовое", "video_note": "Видео-кружок", "audio": "Аудио",
    "photo": "Фото", "video": "Видео", "document": "Документ",
    "sticker": "Стикер", "gif": "GIF",
}

_MEDIA_TOPIC_ID: int | None = None


def _make_client(cfg) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=cfg.xai_api_key, base_url=cfg.llm_base_url)


async def _media_topic_id(conn: aiosqlite.Connection) -> int:
    global _MEDIA_TOPIC_ID
    if _MEDIA_TOPIC_ID is not None:
        return _MEDIA_TOPIC_ID
    cur = await conn.execute("SELECT id FROM topics WHERE slug='media'")
    row = await cur.fetchone(); await cur.close()
    if row:
        _MEDIA_TOPIC_ID = int(row[0]); return _MEDIA_TOPIC_ID
    await conn.execute(
        "INSERT INTO topics(slug, label_ru, description, hidden) "
        "VALUES ('media', 'Медиа', 'Голосовые/фото/видео/документы — требуют просмотра в Telegram.', 0)"
    )
    await conn.commit()
    cur = await conn.execute("SELECT id FROM topics WHERE slug='media'")
    row = await cur.fetchone(); await cur.close()
    _MEDIA_TOPIC_ID = int(row[0]); return _MEDIA_TOPIC_ID


def _decide(kind: str, duration: int | None, caption: str, max_minutes: int) -> str:
    """Маршрутизация (чистая функция). Возвращает action."""
    cap = (caption or "").strip()
    max_sec = max_minutes * 60
    if kind in VOICE_KINDS:
        if duration is not None and duration > max_sec:
            return "force_long_voice"
        return "transcribe"
    if kind in TRIVIAL_KINDS:
        return "trivial"
    # photo / video / document
    if cap:
        return "caption"
    return "force_media"


async def _set_done_text(conn, mid: str, text: str, status: str = "done") -> None:
    """Текст готов — пусть классификатор обработает обычным путём (не скорим тут)."""
    await conn.execute("UPDATE messages SET text=?, media_status=? WHERE id=?", (text, status, mid))
    await conn.commit()


async def _force_priority(conn, mid: str, text: str, urgency: int, importance: int,
                          score: float, rationale: str, slug: str, status: str = "done") -> None:
    await conn.execute("UPDATE messages SET text=?, media_status=? WHERE id=?", (text, status, mid))
    tid = await (_media_topic_id(conn) if slug == "media" else _trivial_topic_id(conn))
    await conn.execute(
        "INSERT OR IGNORE INTO message_topics(message_id, topic_id, confidence) VALUES (?,?,1.0)",
        (mid, tid),
    )
    await conn.execute("UPDATE topics SET message_count = message_count + 1 WHERE id=?", (tid,))
    await conn.execute(
        """INSERT OR REPLACE INTO priorities
           (message_id, urgency, importance, score, rationale, classified_at, model_version)
           VALUES (?,?,?,?,?, datetime('now'), 'media-prefilter')""",
        (mid, urgency, importance, score, rationale),
    )
    await conn.commit()


_TRANSCRIBE_PROMPT = "Расшифруй это голосовое сообщение дословно, на языке оригинала. Верни только текст расшифровки, без комментариев."


async def transcribe_audio(openai_client: AsyncOpenAI, cfg, data: bytes, conn) -> str:
    """Транскрипция OGG/Opus. Сначала OpenAI-совместимый input_audio, при ошибке — нативный generateContent."""
    b64 = base64.b64encode(data).decode("ascii")
    try:
        resp = await openai_client.chat.completions.create(
            model=cfg.grok_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _TRANSCRIBE_PROMPT},
                    {"type": "input_audio", "input_audio": {"data": b64, "format": "ogg"}},
                ],
            }],
            temperature=0.0,
        )
        try:
            u = resp.usage
            if u:
                await _record_usage(conn, u.prompt_tokens or 0, u.completion_tokens or 0)
        except Exception:
            pass
        return resp.choices[0].message.content or ""
    except Exception as e:
        log.warning("input_audio транскрипция не удалась (%s) — пробую нативный generateContent", e)
        return await _transcribe_native(cfg, b64)


async def _transcribe_native(cfg, b64: str) -> str:
    import httpx
    root = cfg.llm_base_url.split("/openai")[0].rstrip("/")  # .../v1beta
    url = f"{root}/models/{cfg.grok_model}:generateContent?key={cfg.xai_api_key}"
    payload = {"contents": [{"parts": [
        {"text": _TRANSCRIBE_PROMPT},
        {"inline_data": {"mime_type": "audio/ogg", "data": b64}},
    ]}]}
    async with httpx.AsyncClient(timeout=120) as cli:
        rr = await cli.post(url, json=payload)
        rr.raise_for_status()
        j = rr.json()
    return j["candidates"][0]["content"]["parts"][0]["text"]


async def _process_one(client: TelegramClient, openai_client: AsyncOpenAI, conn, cfg,
                       row: dict, max_minutes: int, sem: asyncio.Semaphore) -> None:
    mid = row["id"]
    kind = row["media_kind"] or ""
    dur = row["media_duration"]
    caption = row["text"] or ""
    label = KIND_LABELS.get(kind, "Медиа")
    try:
        action = _decide(kind, dur, caption, max_minutes)
        if action == "transcribe":
            chat_id = int(mid.split(":")[0]); msg_id = int(mid.split(":")[1])
            async with sem:
                msg = await client.get_messages(chat_id, ids=msg_id)
                if msg is None:
                    raise RuntimeError("сообщение не найдено для скачивания")
                data = await client.download_media(msg, file=bytes)
            if not data:
                raise RuntimeError("пустое аудио")
            transcript = (await transcribe_audio(openai_client, cfg, data, conn) or "").strip()
            if not transcript:
                raise RuntimeError("пустая транскрипция")
            text = "🎤 Транскрипция голосового сообщения:\n\n" + transcript
            cap = caption.strip()
            if cap:
                text += "\n\nПодпись: " + cap
            await _set_done_text(conn, mid, text)
        elif action == "force_long_voice":
            await _force_priority(
                conn, mid, f"🎤 Голосовое сообщение длиннее {max_minutes} минут — прослушай в Telegram",
                5, 5, 5.0, f"media: {kind} >max", "media")
        elif action == "trivial":
            await _force_priority(conn, mid, caption.strip() or f"[{label}]",
                                  1, 1, 1.0, f"media: {kind}", "trivial")
        elif action == "caption":
            await _set_done_text(conn, mid, f"📎 [медиа: {label}] " + caption.strip())
        else:  # force_media
            await _force_priority(conn, mid, f"📎 {label} — открой в Telegram, чтобы посмотреть",
                                  5, 5, 5.0, f"media: {kind}", "media")
    except Exception as e:
        log.warning("media %s (%s) failed: %s", mid, kind, e)
        try:
            txt = ("🎤 Не удалось расшифровать — прослушай в Telegram"
                   if kind in VOICE_KINDS else f"📎 {label} — открой в Telegram")
            await _force_priority(conn, mid, txt, 5, 5, 5.0, f"media error: {kind}", "media", status="error")
        except Exception as e2:
            log.warning("media %s error-fallback failed: %s", mid, e2)


async def run_media_worker(client: TelegramClient, conn: aiosqlite.Connection, cfg) -> None:
    openai_client = _make_client(cfg)
    sem = asyncio.Semaphore(TRANSCRIBE_CONCURRENCY)
    max_minutes = getattr(cfg, "voice_transcribe_max_minutes", 3)
    log.info("Media worker запущен (порог транскрипции %s мин).", max_minutes)
    while True:
        try:
            cur = await conn.execute(
                """SELECT m.id, m.media_kind, m.media_duration, m.text
                   FROM messages m LEFT JOIN chats c ON c.chat_id=m.chat_id
                   WHERE m.media_status='pending' AND m.deleted_at IS NULL
                     AND COALESCE(c.archived,0)=0 AND COALESCE(c.processing,1)=1
                   ORDER BY m.date_utc DESC LIMIT ?""",
                (BATCH,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            await cur.close()
            for r in rows:
                await _process_one(client, openai_client, conn, cfg, r, max_minutes, sem)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("media worker round failed: %s", e)
        await asyncio.sleep(POLL_SLEEP)
```

- [ ] **Step 2: Unit-test the routing**

Write `data/_chk.py`:
```python
from ingestion.media import _decide
assert _decide("voice", 60, "", 3) == "transcribe"
assert _decide("voice", 200, "", 3) == "force_long_voice"        # >180s
assert _decide("voice", None, "", 3) == "transcribe"             # unknown duration -> try
assert _decide("audio", 181, "", 3) == "force_long_voice"
assert _decide("sticker", None, "", 3) == "trivial"
assert _decide("gif", None, "", 3) == "trivial"
assert _decide("photo", None, "", 3) == "force_media"
assert _decide("photo", None, "смотри отчёт", 3) == "caption"
assert _decide("document", None, "договор", 3) == "caption"
assert _decide("video", None, "", 3) == "force_media"
print("OK media routing")
```
Run `uv run python data\_chk.py` → `OK media routing`. Delete it.

- [ ] **Step 3: compileall + import check**

```
uv run python -m compileall -q ingestion
uv run python -c "from ingestion import media; print('import ok')"
```
Both clean / prints `import ok`.

- [ ] **Step 4: Commit**

```
git add ingestion/media.py
git commit -m "feat(media): worker for voice transcription + media prioritisation"
```

---

### Task 6: Register media worker in cmd_run

**Files:**
- Modify: `app/cli.py` (`cmd_run`)

- [ ] **Step 1: Import + task**

In `app/cli.py:cmd_run`, add to the imports block at the top of the function (next to `from classifier.grok_worker import run_worker`):
```python
    from ingestion.media import run_media_worker
```
After the `classifier_task = asyncio.create_task(run_worker(conn, cfg), name="classifier")` block, add:
```python
    media_task = asyncio.create_task(
        run_media_worker(client, conn, cfg),
        name="media",
    )
```

- [ ] **Step 2: Cancel on shutdown**

In the `finally:` block, change the `tasks_to_cancel` list to include `media_task`:
```python
        tasks_to_cancel = [backfill_task, classifier_task, media_task, notify_task, outbox_task]
```

- [ ] **Step 3: Verify + commit**

```
uv run python -m compileall -q app
uv run python -c "from app import cli"
```
Both clean.
```
git add app/cli.py
git commit -m "feat(run): start media worker alongside classifier"
```

---

### Task 7: Live transcription test, E2E, journal, finalize

**Files:**
- Modify: `docs/PROJECT_JOURNAL.md`

- [ ] **Step 1: Restart the service so new code + worker load**

```
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*app.cli run*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process -FilePath "powershell" -ArgumentList "-NoProfile","-Command","Set-Location 'C:\Users\User\ClaudeC\aichatpom'; uv run python -m app.cli run" -WindowStyle Hidden
```
Wait ~10s, then confirm `data/run.err.log` tail has no traceback and `data/run.log` shows `Media worker запущен`.

- [ ] **Step 2: Live E2E (requires the user)**

Ask the user to send themselves (Saved Messages or a DM) a SHORT voice message (<3 min) and, separately, a photo. Then within ~30s check the DB — write `data/_chk.py`:
```python
import sqlite3
c = sqlite3.connect('data/app.db'); c.row_factory = sqlite3.Row
for r in c.execute("""SELECT id, media_kind, media_status, substr(text,1,80) t,
                             (SELECT score FROM priorities p WHERE p.message_id=m.id) score
                      FROM messages m WHERE media_kind IS NOT NULL
                      ORDER BY date_utc DESC LIMIT 5"""):
    print(dict(r))
```
Run `uv run python data\_chk.py`. Expected: the voice row has `media_status='done'`, text starting `🎤 Транскрипция…`, and a score (from normal classification); the photo row has `media_status='done'`, text `📎 Фото…`, score 5.0. Delete `data/_chk.py`.

If the voice row shows `media_status='error'` with the "Не удалось расшифровать" text, the `input_audio` ogg path failed AND native fallback failed — inspect `data/run.log` for the error, and if needed adjust `transcribe_audio` (e.g., MIME/format). Do not mark the task complete until a real voice transcribes.

- [ ] **Step 3: Update journal**

In `docs/PROJECT_JOURNAL.md` §3 (messages fields) add a line noting `media_kind`, `media_status`, `media_duration`. In §6 add:
```
| 2026-06-25 | **Транскрипция голосовых + обработка медиа**: новый `ingestion/media.py: run_media_worker` (в `cmd_run`). Короткие голосовые (≤`voice_transcribe_max_minutes`, дефолт 3) транскрибируются Gemini (`input_audio`, fallback native `generateContent`) и идут в классификатор как текст; длинные голосовые/фото/видео/документы — метка + score 5; стикеры/GIF — тривиал; медиа с подписью — по подписи. Колонки `media_kind/status/duration`, классификатор пропускает `pending` | `ingestion/media.py`, `ingestion/telegram_listener.py`, `classifier/grok_worker.py`, `app/cli.py`, `app/db.py`, `app/schema.sql`, `app/config.py` |
```
Update §9 (грабли) if the ogg path needed the native fallback (note which worked).

- [ ] **Step 4: Commit**

```
git add docs/PROJECT_JOURNAL.md
git commit -m "docs: journal — voice transcription + media handling"
```

---

## Self-Review

**Spec coverage:**
- Schema columns + index → Task 1 ✓
- `voice_transcribe_max_minutes` config → Task 2 ✓
- Listener tags media_kind/duration/status → Task 3 ✓
- Classifier skips pending → Task 4 ✓
- Worker: transcribe ≤max, force long-voice, force media, caption→classify, sticker/gif→trivial, error fallback → Task 5 (`_decide` + `_process_one`) ✓
- Gemini input_audio + native fallback, usage recording → Task 5 (`transcribe_audio`, `_transcribe_native`) ✓
- Register in cmd_run + cancel → Task 6 ✓
- E2E voice + photo, journal → Task 7 ✓

**Placeholder scan:** No TBD/TODO. Every code step is complete. The ogg-vs-native choice is resolved at runtime by `transcribe_audio` (try/except), not left as a placeholder; Task 7 Step 2 verifies empirically.

**Type consistency:** `media_kind`/`media_status`/`media_duration` consistent across schema, migration, listener INSERT, worker SELECT. `_decide(kind, duration, caption, max_minutes) -> str` returns one of `transcribe|force_long_voice|trivial|caption|force_media`, all handled in `_process_one`. `transcribe_audio(openai_client, cfg, data, conn)` signature matches its call. `_force_priority(...)`/`_set_done_text(...)` names consistent. Imported `_record_usage`, `_trivial_topic_id` exist in `classifier/grok_worker.py` (verified).
