"""Ночной консолидатор: скрывает мёртвые темы, логирует merge-предложения."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from rapidfuzz import fuzz

from app.config import DATA_DIR, LOG_DIR

log = logging.getLogger(__name__)

LAST_RUN_FILE = DATA_DIR / "last_consolidate.txt"
MERGE_LOG = LOG_DIR / "merge_suggestions.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_last_run() -> datetime | None:
    if not LAST_RUN_FILE.exists():
        return None
    try:
        return datetime.fromisoformat(LAST_RUN_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _write_last_run() -> None:
    LAST_RUN_FILE.write_text(_now_iso(), encoding="utf-8")


async def nightly_consolidate(conn: aiosqlite.Connection, force: bool = False) -> None:
    """Прогон консолидации. Не чаще раза в 24 часа, если force=False."""
    last = _read_last_run()
    if not force and last is not None:
        delta = datetime.now(timezone.utc) - last
        if delta.total_seconds() < 24 * 3600:
            return

    log.info("Запуск ночной консолидации тем…")

    # 1. Скрыть мёртвые: message_count < 3 AND created_at < now-14d AND hidden=0
    cur = await conn.execute(
        """
        UPDATE topics
        SET hidden = 1
        WHERE hidden = 0
          AND message_count < 3
          AND created_at < datetime('now', '-14 days')
        """
    )
    hidden_count = cur.rowcount or 0
    await cur.close()
    await conn.commit()
    if hidden_count:
        log.info("Скрыто мёртвых тем: %s", hidden_count)

    # 2. Найти близкие пары (fuzz.ratio > 90) — записать в лог
    cur = await conn.execute(
        "SELECT id, slug, label_ru, message_count FROM topics WHERE hidden = 0"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()

    pairs: list[tuple[dict, dict, float]] = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a = rows[i]
            b = rows[j]
            score = fuzz.ratio((a["label_ru"] or "").lower(), (b["label_ru"] or "").lower())
            if score > 90:
                pairs.append((a, b, score))

    if pairs:
        MERGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MERGE_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {_now_iso()} — {len(pairs)} предложений ===\n")
            for a, b, score in pairs:
                f.write(
                    f"  score={score:.0f} | {a['slug']} ({a['label_ru']}, n={a['message_count']}) "
                    f"<-> {b['slug']} ({b['label_ru']}, n={b['message_count']})\n"
                )
        log.info("Записано предложений на merge: %s → %s", len(pairs), MERGE_LOG)

    _write_last_run()
    log.info("Консолидация завершена.")
