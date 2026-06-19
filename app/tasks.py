"""Чистая бизнес-логика задачника без зависимостей от веб/бот-слоёв."""
from __future__ import annotations


async def build_task_from_message(conn, message_id: str) -> tuple[str, str] | None:
    """Сгенерировать (title, priority) из сообщения. None если сообщение не найдено."""
    cur = await conn.execute(
        """SELECT m.text, p.urgency, p.importance
           FROM messages m LEFT JOIN priorities p ON p.message_id=m.id
           WHERE m.id=?""",
        (message_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    text = (row["text"] or "").strip()
    title = text.split("\n", 1)[0][:120] or "Без названия"
    u = row["urgency"] or 0
    i = row["importance"] or 0
    if u >= 4:
        prio = "high"
    elif u <= 2 and i <= 2:
        prio = "low"
    else:
        prio = "normal"
    return title, prio


async def create_task_from_message(conn, message_id: str) -> int | None:
    """Создать задачу из сообщения. Возвращает task_id или None если сообщение не найдено."""
    built = await build_task_from_message(conn, message_id)
    if built is None:
        return None
    title, prio = built
    cur = await conn.execute(
        "INSERT INTO tasks(title, priority, source_message_id) VALUES (?,?,?)",
        (title, prio, message_id),
    )
    await conn.commit()
    return cur.lastrowid
