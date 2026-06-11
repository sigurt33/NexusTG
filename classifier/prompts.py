"""Промпты и JSON Schema для Grok-классификатора."""
from __future__ import annotations

from typing import Any

import aiosqlite

SYSTEM_BASE = (
    "Ты классификатор задач из Telegram. Отвечаешь СТРОГО валидным JSON по схеме.\n"
    "Твоя задача — определить тему/темы сообщения (1–3), оценить срочность (urgency 1-5) "
    "и важность (importance 1-5), и дать краткое обоснование на русском (одна строка).\n\n"
    "Правила выбора тем:\n"
    "1. Если сообщение подходит под одну или несколько ИЗ СУЩЕСТВУЮЩИХ тем ниже — используй их "
    "(укажи их slug и label_ru, поле is_new=false, description можно опустить).\n"
    "2. Если ни одна не подходит — предложи НОВУЮ тему: укажи slug в snake_case (латиница, "
    "короткое), label_ru — человекочитаемое название на русском (2–4 слова), description — "
    "одна строка по-русски, объясняющая что входит в эту тему. is_new=true.\n"
    "3. Не выдумывай больше 3 тем. Лучше меньше и точнее.\n\n"
    "Шкалы:\n"
    "- urgency: 1 = можно игнорировать, 3 = ответить в течение дня, 5 = сейчас же.\n"
    "- importance: 1 = шум, 3 = рабочая рутина, 5 = критично для пользователя или бизнеса.\n"
)


CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topics", "urgency", "importance", "rationale"],
    "properties": {
        "topics": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["slug", "label_ru", "is_new", "description"],
                "properties": {
                    "slug": {"type": "string"},
                    "label_ru": {"type": "string"},
                    "is_new": {"type": "boolean"},
                    "description": {"type": "string"},
                },
            },
        },
        "urgency": {"type": "integer", "minimum": 1, "maximum": 5},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        "rationale": {"type": "string"},
    },
}


async def fetch_top_topics(conn: aiosqlite.Connection, limit: int = 50) -> list[dict]:
    """Top-N тем по message_count за последние 30 дней (с recency-весом по created_at)."""
    cur = await conn.execute(
        """
        SELECT slug, label_ru, COALESCE(description, '') AS description, message_count
        FROM topics
        WHERE hidden = 0
        ORDER BY message_count DESC, created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def fetch_active_rules(conn: aiosqlite.Connection) -> list[str]:
    cur = await conn.execute(
        "SELECT rule_text FROM user_rules WHERE active=1 ORDER BY id ASC"
    )
    rows = await cur.fetchall()
    await cur.close()
    return [r[0] for r in rows if r[0]]


def build_system_prompt(topics: list[dict], examples: list[dict] | None = None, rules: list[str] | None = None) -> str:
    if not topics:
        topics_block = "(Список существующих тем пуст — все темы новые.)"
    else:
        lines = []
        for t in topics:
            desc = (t.get("description") or "").strip().replace("\n", " ")
            lines.append(f"- {t['slug']} | {t['label_ru']} | {desc}")
        topics_block = "\n".join(lines)
    out = (
        SYSTEM_BASE
        + "\nСУЩЕСТВУЮЩИЕ ТЕМЫ (slug | label_ru | description):\n"
        + topics_block
    )
    if rules:
        out += "\n\nПРЕДПОЧТЕНИЯ ПОЛЬЗОВАТЕЛЯ (АЛИНА-РЕЖИМ — учитывай эти правила при оценке тем, urgency, importance):\n"
        for r in rules:
            out += f"- {r.strip()}\n"
    if examples:
        ex_lines = ["", "ПРИМЕРЫ КОРРЕКТНЫХ КЛАССИФИКАЦИЙ (учись на них):"]
        for ex in examples[:5]:
            note = (ex.get("note") or "").strip()
            ex_lines.append(
                f"- urgency={ex.get('urgency')} importance={ex.get('importance')} "
                f"topics={ex.get('topics_json')}" + (f" // {note}" if note else "")
            )
        out += "\n" + "\n".join(ex_lines)
    return out


def build_batch_user_prompt(items: list[dict]) -> str:
    """Запрос на классификацию N сообщений за один вызов.
    items: [{idx, text, chat_title, sender_name, context_msgs}, ...]"""
    parts = [
        f"Классифицируй {len(items)} сообщений ниже. ",
        "Верни СТРОГО валидный JSON одного вида: {\"results\": [..]}, где results — массив "
        f"длиной ровно {len(items)}, по одному объекту на сообщение в исходном порядке. "
        "Каждый объект: "
        '{"idx": int, "topics":[{"slug":"snake_case","label_ru":"Тема","is_new":false,"description":"..."}], '
        '"urgency":1-5, "importance":1-5, "rationale":"одна строка по-русски"}.',
        "",
    ]
    for it in items:
        parts.append(f"=== СООБЩЕНИЕ #{it['idx']} ===")
        parts.append(f"Чат: {it.get('chat_title') or '—'}")
        parts.append(f"Отправитель: {it.get('sender_name') or '—'}")
        ctx = it.get("context_msgs") or []
        if ctx:
            parts.append("Контекст (±2):")
            for c in ctx:
                sn = c.get("sender_name") or "—"
                pos = c.get("position")
                txt = (c.get("text") or "").strip().replace("\n", " ")
                if len(txt) > 250:
                    txt = txt[:250] + "..."
                parts.append(f"  [{pos:+d}] {sn}: {txt}")
        target_text = (it.get("text") or "").strip()
        if len(target_text) > 1500:
            target_text = target_text[:1500] + "..."
        parts.append(f"Текст: {target_text or '(пусто)'}")
        parts.append("")
    parts.append("Верни JSON {\"results\":[...]}.")
    return "\n".join(parts)


def build_user_prompt(
    text: str,
    chat_title: str | None,
    sender_name: str | None,
    context_msgs: list[dict],
) -> str:
    parts = [
        f"Чат: {chat_title or '—'}",
        f"Отправитель: {sender_name or '—'}",
        "",
        "Контекст (соседние сообщения, position от -2 до +2):",
    ]
    if context_msgs:
        for c in context_msgs:
            sn = c.get("sender_name") or "—"
            pos = c.get("position")
            txt = (c.get("text") or "").strip().replace("\n", " ")
            if len(txt) > 300:
                txt = txt[:300] + "…"
            parts.append(f"  [{pos:+d}] {sn}: {txt}")
    else:
        parts.append("  (контекста нет)")
    parts.append("")
    parts.append("ЦЕЛЕВОЕ СООБЩЕНИЕ:")
    target_text = (text or "").strip()
    if len(target_text) > 2000:
        target_text = target_text[:2000] + "…"
    parts.append(target_text or "(пусто)")
    parts.append("")
    parts.append(
        "Верни СТРОГО валидный JSON ровно следующего вида (все поля обязательны, "
        "rationale не может быть пустой строкой — это объяснение почему ты так оценил, 1 строка по-русски):\n"
        '{\n'
        '  "topics": [{"slug": "snake_case", "label_ru": "Название", "is_new": false, "description": "Описание"}],\n'
        '  "urgency": 1-5,\n'
        '  "importance": 1-5,\n'
        '  "rationale": "одна строка по-русски, почему именно такие оценки"\n'
        '}'
    )
    return "\n".join(parts)
