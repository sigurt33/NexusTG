"""Резолвинг и создание тем с fuzzy-нормализацией."""
from __future__ import annotations

import logging
import re

import aiosqlite
from rapidfuzz import fuzz

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85


def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "topic"


async def _all_topics(conn: aiosqlite.Connection) -> list[dict]:
    cur = await conn.execute("SELECT id, slug, label_ru FROM topics")
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _slug_exists(conn: aiosqlite.Connection, slug: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM topics WHERE slug = ?", (slug,))
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def resolve_or_create(
    conn: aiosqlite.Connection,
    slug: str,
    label_ru: str,
    description: str | None,
    is_new: bool,
) -> int:
    """Найти существующий topic_id по slug или label_ru (fuzzy) либо создать новый."""
    slug = _slugify(slug)
    label_ru = (label_ru or slug).strip()

    # 1. exact slug match — всегда переиспользуем
    cur = await conn.execute("SELECT id FROM topics WHERE slug = ?", (slug,))
    row = await cur.fetchone()
    await cur.close()
    if row is not None:
        return int(row[0])

    # 2. fuzzy на label_ru
    existing = await _all_topics(conn)
    best_id = None
    best_score = 0
    for t in existing:
        score = fuzz.token_set_ratio(label_ru.lower(), (t["label_ru"] or "").lower())
        if score > best_score:
            best_score = score
            best_id = int(t["id"])
    if best_id is not None and best_score >= FUZZY_THRESHOLD:
        log.info("Тема '%s' сопоставлена с существующей id=%s (score=%s)", label_ru, best_id, best_score)
        return best_id

    # 3. создаём новую (slug может коллидировать с зарезервированным под другим label — добавим суффикс)
    final_slug = slug
    suffix = 2
    while await _slug_exists(conn, final_slug):
        final_slug = f"{slug}_{suffix}"
        suffix += 1

    cur = await conn.execute(
        "INSERT INTO topics (slug, label_ru, description) VALUES (?, ?, ?)",
        (final_slug, label_ru, (description or "").strip() or None),
    )
    new_id = cur.lastrowid
    await cur.close()
    await conn.commit()
    log.info("Создана новая тема: %s | %s (id=%s)", final_slug, label_ru, new_id)
    return int(new_id)


async def bump_message_count(conn: aiosqlite.Connection, topic_id: int) -> None:
    await conn.execute(
        "UPDATE topics SET message_count = message_count + 1 WHERE id = ?",
        (topic_id,),
    )
