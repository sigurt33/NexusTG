"""Classifier worker: polling SQLite → Grok → priorities/message_topics."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from openai import AsyncOpenAI
from openai import APIError, RateLimitError

from classifier import prompts as P
from classifier import topic_normalizer as TN

log = logging.getLogger(__name__)

# Default fallback pricing (config overrides at runtime)
INPUT_USD_PER_M = 1.25
OUTPUT_USD_PER_M = 5.00

BATCH_SIZE = 30          # сколько сообщений берём за один раунд воркера
BATCH_PER_CALL = 5       # сколько сообщений упаковываем в один LLM-запрос
POLL_SLEEP = 10
PARALLEL = 6
MAX_FAILURES_PER_MSG = 3
DEFAULT_CONFIDENCE = 1.0

# Pre-filter тривиала — точные совпадения (после strip+lower) НЕ попадают в LLM.
_TRIVIAL_TOKENS: set[str] = {
    "ok", "ок", "окей", "окk", "kk", "k", "+", "-", "++", "+1",
    "да", "нет", "ага", "угу", "хорошо", "понятно", "ясно", "договорились",
    "спасибо", "спс", "спсб", "благодарю", "пасибо", "сяп",
    "👍", "👌", "✅", "🙏", "❤", "❤️", "🔥", "💯", "🤝",
    "доброе утро", "добрый день", "добрый вечер", "доброй ночи",
    "привет", "приветик", "здравствуйте", "хай", "хелло",
    "пока", "до встречи", "до связи", "хорошего дня",
}


def _is_trivial(text: str | None) -> bool:
    if not text:
        return True
    t = text.strip().lower()
    if not t:
        return True
    # короткие фразы без вопроса/восклицания
    if t in _TRIVIAL_TOKENS:
        return True
    if len(t) <= 4 and "?" not in t and "!" not in t:
        return True
    # только эмодзи/пунктуация
    if all(not ch.isalnum() for ch in t):
        return True
    return False


_TRIVIAL_TOPIC_ID: int | None = None


async def _trivial_topic_id(conn: aiosqlite.Connection) -> int:
    global _TRIVIAL_TOPIC_ID
    if _TRIVIAL_TOPIC_ID is not None:
        return _TRIVIAL_TOPIC_ID
    cur = await conn.execute("SELECT id FROM topics WHERE slug='trivial'")
    row = await cur.fetchone()
    await cur.close()
    if row:
        _TRIVIAL_TOPIC_ID = int(row[0])
        return _TRIVIAL_TOPIC_ID
    await conn.execute(
        "INSERT INTO topics(slug, label_ru, description, hidden) "
        "VALUES ('trivial', 'Тривиальное', 'Короткие подтверждения, эмодзи, приветствия — без задачи.', 0)"
    )
    await conn.commit()
    cur = await conn.execute("SELECT id FROM topics WHERE slug='trivial'")
    row = await cur.fetchone(); await cur.close()
    _TRIVIAL_TOPIC_ID = int(row[0])
    return _TRIVIAL_TOPIC_ID


async def _handle_trivial(conn: aiosqlite.Connection, msg: dict) -> None:
    """Записать priorities+topic без вызова LLM."""
    mid = msg["id"]
    tid = await _trivial_topic_id(conn)
    await conn.execute(
        """INSERT OR IGNORE INTO message_topics(message_id, topic_id, confidence)
           VALUES (?, ?, 1.0)""",
        (mid, tid),
    )
    await conn.execute("UPDATE topics SET message_count = message_count + 1 WHERE id=?", (tid,))
    await conn.execute(
        """INSERT OR REPLACE INTO priorities
           (message_id, urgency, importance, score, rationale, classified_at, model_version)
           VALUES (?, 1, 1, 1.0, 'pre-filter: trivial', datetime('now'), 'prefilter')""",
        (mid,),
    )
    await conn.commit()

# Кеш счётчика неудач по message_id (in-memory; перезапуск — сброс, не критично)
_failure_counts: dict[str, int] = {}


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def _today_token_usage(conn: aiosqlite.Connection) -> tuple[int, float]:
    today = _today_utc()
    cur = await conn.execute(
        "SELECT prompt_tokens, completion_tokens, cost_usd FROM grok_usage WHERE date = ?",
        (today,),
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return 0, 0.0
    return int(row[0]) + int(row[1]), float(row[2])


async def _record_usage(
    conn: aiosqlite.Connection,
    prompt_tokens: int,
    completion_tokens: int,
    input_per_m: float = INPUT_USD_PER_M,
    output_per_m: float = OUTPUT_USD_PER_M,
) -> None:
    cost = (
        prompt_tokens * input_per_m / 1_000_000
        + completion_tokens * output_per_m / 1_000_000
    )
    today = _today_utc()
    await conn.execute(
        """
        INSERT INTO grok_usage (date, prompt_tokens, completion_tokens, cost_usd)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            prompt_tokens = prompt_tokens + excluded.prompt_tokens,
            completion_tokens = completion_tokens + excluded.completion_tokens,
            cost_usd = cost_usd + excluded.cost_usd
        """,
        (today, prompt_tokens, completion_tokens, cost),
    )
    await conn.commit()


async def _pending_messages(conn: aiosqlite.Connection, limit: int) -> list[dict]:
    cur = await conn.execute(
        """
        SELECT m.id, m.chat_id, m.chat_title, m.sender_name, m.text, m.date_utc
        FROM messages m
        LEFT JOIN chats c ON c.chat_id = m.chat_id
        WHERE m.is_context_only = 0
          AND m.deleted_at IS NULL
          AND COALESCE(c.archived, 0) = 0
          AND COALESCE(c.processing, 1) = 1
          AND COALESCE(m.media_status, '') <> 'pending'
          AND m.id NOT IN (SELECT message_id FROM priorities)
        ORDER BY m.date_utc DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _fetch_context(conn: aiosqlite.Connection, message_id: str) -> list[dict]:
    cur = await conn.execute(
        """
        SELECT cl.position, m.sender_name, m.text
        FROM context_links cl
        JOIN messages m ON m.id = cl.context_msg_id
        WHERE cl.message_id = ?
          AND cl.position IN (-2, -1, 1, 2)
        ORDER BY cl.position ASC
        """,
        (message_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


def _make_client(api_key: str, base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/") -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


async def _call_grok_with_retry(
    client: AsyncOpenAI, model: str, system_prompt: str, user_prompt: str
) -> tuple[dict, int, int]:
    """Возвращает (parsed_json, prompt_tokens, completion_tokens)."""
    backoffs = [2, 4, 8, 16, 60]
    last_err: Exception | None = None
    for attempt, delay in enumerate([0] + backoffs):
        if delay:
            await asyncio.sleep(delay)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            content = resp.choices[0].message.content or "{}"
            parsed = json.loads(content)
            usage = resp.usage
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            return parsed, pt, ct
        except RateLimitError as e:
            last_err = e
            log.warning("Grok 429 (попытка %s): %s", attempt + 1, e)
            continue
        except (APIError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("Grok ошибка (попытка %s): %s", attempt + 1, e)
            continue
    log.error("Grok недоступен после ретраев, отложу на 5 минут. Последняя ошибка: %s", last_err)
    await asyncio.sleep(300)
    raise RuntimeError(f"Grok unavailable: {last_err}")


async def _insert_failed_placeholder(conn: aiosqlite.Connection, message_id: str, model: str) -> None:
    await conn.execute(
        """
        INSERT OR IGNORE INTO priorities
            (message_id, urgency, importance, score, rationale, classified_at, model_version)
        VALUES (?, 1, 1, ?, 'classification_failed', datetime('now'), ?)
        """,
        (message_id, 1 * 0.6 + 1 * 0.4, model),
    )
    await conn.commit()


async def classify_one(
    conn: aiosqlite.Connection,
    client: AsyncOpenAI,
    model: str,
    msg: dict,
    system_prompt: str,
) -> tuple[bool, dict | None]:
    """Классифицировать одно сообщение. Возвращает (ok, parsed)."""
    mid = msg["id"]
    context_msgs = await _fetch_context(conn, mid)
    user_prompt = P.build_user_prompt(
        text=msg.get("text") or "",
        chat_title=msg.get("chat_title"),
        sender_name=msg.get("sender_name"),
        context_msgs=context_msgs,
    )
    try:
        parsed, pt, ct = await _call_grok_with_retry(client, model, system_prompt, user_prompt)
    except Exception as e:
        log.error("Не удалось классифицировать %s: %s", mid, e)
        _failure_counts[mid] = _failure_counts.get(mid, 0) + 1
        if _failure_counts[mid] >= MAX_FAILURES_PER_MSG:
            log.warning("Превышен лимит ошибок для %s — вставляю заглушку", mid)
            await _insert_failed_placeholder(conn, mid, model)
            _failure_counts.pop(mid, None)
        return False, None

    await _record_usage(
        conn, pt, ct,
        input_per_m=getattr(client, "_input_per_m", INPUT_USD_PER_M),
        output_per_m=getattr(client, "_output_per_m", OUTPUT_USD_PER_M),
    )

    # Валидация полей
    try:
        topics = parsed["topics"]
        urgency = int(parsed["urgency"])
        importance = int(parsed["importance"])
        rationale = str(parsed.get("rationale") or "").strip()
        assert 1 <= urgency <= 5 and 1 <= importance <= 5
        assert isinstance(topics, list) and 1 <= len(topics) <= 3
    except (KeyError, ValueError, AssertionError, TypeError) as e:
        log.warning("Невалидный JSON для %s: %s | %s", mid, e, parsed)
        _failure_counts[mid] = _failure_counts.get(mid, 0) + 1
        if _failure_counts[mid] >= MAX_FAILURES_PER_MSG:
            await _insert_failed_placeholder(conn, mid, model)
            _failure_counts.pop(mid, None)
        return False, None

    # Резолвинг тем
    topic_ids: list[int] = []
    for t in topics:
        tid = await TN.resolve_or_create(
            conn,
            slug=str(t.get("slug") or ""),
            label_ru=str(t.get("label_ru") or ""),
            description=t.get("description"),
            is_new=bool(t.get("is_new", False)),
        )
        topic_ids.append(tid)

    # Вставка message_topics + bump
    for tid in set(topic_ids):
        await conn.execute(
            """
            INSERT OR IGNORE INTO message_topics (message_id, topic_id, confidence)
            VALUES (?, ?, ?)
            """,
            (mid, tid, DEFAULT_CONFIDENCE),
        )
        await TN.bump_message_count(conn, tid)

    score = urgency * 0.6 + importance * 0.4
    await conn.execute(
        """
        INSERT OR REPLACE INTO priorities
            (message_id, urgency, importance, score, rationale, classified_at, model_version)
        VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
        """,
        (mid, urgency, importance, score, rationale, model),
    )
    await conn.commit()
    _failure_counts.pop(mid, None)
    return True, parsed


async def classify_batch(
    conn: aiosqlite.Connection,
    client: AsyncOpenAI,
    model: str,
    msgs: list[dict],
    system_prompt: str,
) -> int:
    """Классифицировать пачку сообщений одним LLM-запросом. Возвращает кол-во ОК."""
    if not msgs:
        return 0
    # подготовим context для каждого
    items = []
    for idx, m in enumerate(msgs):
        ctx = await _fetch_context(conn, m["id"])
        items.append({
            "idx": idx,
            "text": m.get("text") or "",
            "chat_title": m.get("chat_title"),
            "sender_name": m.get("sender_name"),
            "context_msgs": ctx,
        })
    user_prompt = P.build_batch_user_prompt(items)
    try:
        parsed, pt, ct = await _call_grok_with_retry(client, model, system_prompt, user_prompt)
    except Exception as e:
        log.error("Батч из %s упал: %s", len(msgs), e)
        for m in msgs:
            mid = m["id"]
            _failure_counts[mid] = _failure_counts.get(mid, 0) + 1
            if _failure_counts[mid] >= MAX_FAILURES_PER_MSG:
                await _insert_failed_placeholder(conn, mid, model)
                _failure_counts.pop(mid, None)
        return 0

    await _record_usage(
        conn, pt, ct,
        input_per_m=getattr(client, "_input_per_m", INPUT_USD_PER_M),
        output_per_m=getattr(client, "_output_per_m", OUTPUT_USD_PER_M),
    )

    results = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(results, list):
        log.warning("Батч вернул не {results:[...]}: %s", parsed)
        return 0

    ok = 0
    by_idx = {int(r.get("idx", -1)): r for r in results if isinstance(r, dict)}
    for idx, m in enumerate(msgs):
        r = by_idx.get(idx)
        if r is None:
            continue
        mid = m["id"]
        try:
            topics = r["topics"]
            urgency = int(r["urgency"])
            importance = int(r["importance"])
            rationale = str(r.get("rationale") or "").strip()
            assert 1 <= urgency <= 5 and 1 <= importance <= 5
            assert isinstance(topics, list) and 1 <= len(topics) <= 3
        except Exception as e:
            log.warning("Невалидный элемент батча для %s: %s | %s", mid, e, r)
            continue

        topic_ids: list[int] = []
        for t in topics:
            tid = await TN.resolve_or_create(
                conn,
                slug=str(t.get("slug") or ""),
                label_ru=str(t.get("label_ru") or ""),
                description=t.get("description"),
                is_new=bool(t.get("is_new", False)),
            )
            topic_ids.append(tid)
        for tid in set(topic_ids):
            await conn.execute(
                """INSERT OR IGNORE INTO message_topics (message_id, topic_id, confidence)
                   VALUES (?, ?, ?)""",
                (mid, tid, DEFAULT_CONFIDENCE),
            )
            await TN.bump_message_count(conn, tid)

        score = urgency * 0.6 + importance * 0.4
        await conn.execute(
            """INSERT OR REPLACE INTO priorities
                (message_id, urgency, importance, score, rationale, classified_at, model_version)
               VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
            (mid, urgency, importance, score, rationale, model),
        )
        _failure_counts.pop(mid, None)
        ok += 1
    await conn.commit()
    return ok


async def fetch_recent_examples(conn: aiosqlite.Connection, limit: int = 5) -> list[dict]:
    cur = await conn.execute(
        "SELECT urgency, importance, topics_json, note FROM classification_examples "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def reclassify_one(
    conn: aiosqlite.Connection,
    cfg,
    client: AsyncOpenAI,
    message_id: str,
) -> bool:
    """Удалить текущие priorities/message_topics, прогнать заново."""
    # уменьшим counters перед удалением
    cur = await conn.execute("SELECT topic_id FROM message_topics WHERE message_id=?", (message_id,))
    tids = [int(r[0]) for r in await cur.fetchall()]
    await cur.close()
    for tid in tids:
        await conn.execute(
            "UPDATE topics SET message_count = MAX(0, message_count - 1) WHERE id=?",
            (tid,),
        )
    await conn.execute("DELETE FROM message_topics WHERE message_id=?", (message_id,))
    await conn.execute("DELETE FROM priorities WHERE message_id=?", (message_id,))
    await conn.commit()

    # достанем сообщение
    cur = await conn.execute(
        "SELECT id, chat_id, chat_title, sender_name, text, date_utc FROM messages WHERE id=?",
        (message_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return False
    msg = dict(row)
    topics = await P.fetch_top_topics(conn, limit=50)
    examples = await fetch_recent_examples(conn, limit=5)
    rules = await P.fetch_active_rules(conn)
    system_prompt = P.build_system_prompt(topics, examples=examples, rules=rules)
    ok, _ = await classify_one(conn, client, cfg.grok_model, msg, system_prompt)
    return ok


async def run_one_round(
    conn: aiosqlite.Connection, cfg, client: AsyncOpenAI, limit: int = BATCH_SIZE
) -> int:
    """Один проход: до `limit` сообщений. Возвращает число классифицированных."""
    used_tokens, _cost = await _today_token_usage(conn)
    if used_tokens >= cfg.grok_daily_token_budget:
        log.warning(
            "Дневной бюджет Grok исчерпан (%s/%s токенов), пропускаю раунд.",
            used_tokens, cfg.grok_daily_token_budget,
        )
        return 0

    msgs = await _pending_messages(conn, limit)
    if not msgs:
        return 0

    # 1) Pre-filter: тривиал — без LLM
    trivial_done = 0
    real_msgs: list[dict] = []
    for m in msgs:
        if _is_trivial(m.get("text")):
            try:
                await _handle_trivial(conn, m)
                trivial_done += 1
            except Exception as e:
                log.warning("Trivial-обработка %s упала: %s", m["id"], e)
                real_msgs.append(m)
        else:
            real_msgs.append(m)

    if not real_msgs:
        if trivial_done:
            log.info("Тривиал (без LLM): %s", trivial_done)
        return trivial_done

    # 2) Реальные — пачками по BATCH_PER_CALL, параллельно
    topics = await P.fetch_top_topics(conn, limit=50)
    examples = await fetch_recent_examples(conn, limit=5)
    rules = await P.fetch_active_rules(conn)
    system_prompt = P.build_system_prompt(topics, examples=examples, rules=rules)

    batches: list[list[dict]] = [
        real_msgs[i:i + BATCH_PER_CALL]
        for i in range(0, len(real_msgs), BATCH_PER_CALL)
    ]
    sem = asyncio.Semaphore(PARALLEL)

    async def _batch(b):
        async with sem:
            return await classify_batch(conn, client, cfg.grok_model, b, system_prompt)

    results = await asyncio.gather(*(_batch(b) for b in batches), return_exceptions=True)
    llm_done = sum(r for r in results if isinstance(r, int))
    if trivial_done:
        log.info("Тривиал (без LLM): %s", trivial_done)
    used_tokens, _ = await _today_token_usage(conn)
    if used_tokens >= cfg.grok_daily_token_budget:
        log.warning("Бюджет исчерпан в середине раунда.")
    return trivial_done + llm_done


async def run_worker(conn: aiosqlite.Connection, cfg) -> None:
    """Бесконечный воркер для запуска в asyncio.gather."""
    if not cfg.xai_api_key:
        log.error("XAI_API_KEY не задан — классификатор не запущен.")
        return
    client = _make_client(cfg.xai_api_key, cfg.llm_base_url)
    client._input_per_m = cfg.llm_input_usd_per_m
    client._output_per_m = cfg.llm_output_usd_per_m
    log.info("Classifier-воркер запущен (модель=%s, base=%s).", cfg.grok_model, cfg.llm_base_url)
    while True:
        try:
            n = await run_one_round(conn, cfg, client)
            if n:
                log.info("Классифицировано %s сообщений.", n)
        except asyncio.CancelledError:
            log.info("Classifier остановлен.")
            return
        except Exception as e:
            log.exception("Ошибка в раунде классификации: %s", e)
        await asyncio.sleep(POLL_SLEEP)
