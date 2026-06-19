"""Общие helpers для построения ссылок на Telegram-сообщения."""
from __future__ import annotations


def telegram_deep_link(message_id: str, is_dm: bool = False) -> str | None:
    """Ссылка, открывающая Telegram на конкретном сообщении.

    - ЛС:                    tg://openmessage?user_id=...&message_id=...
    - супергруппы/каналы:    https://t.me/c/{raw_channel_id}/{msg_id}

    `message_id` — композитный "{chat_id}:{msg_id}". Возвращает None при ошибке парсинга.
    """
    try:
        chat_id_s, msg_id_s = message_id.split(":", 1)
        chat_id_int = int(chat_id_s)
    except (ValueError, AttributeError):
        return None
    if is_dm:
        return f"tg://openmessage?user_id={abs(chat_id_int)}&message_id={msg_id_s}"
    raw = chat_id_int
    if chat_id_int < 0:
        s = str(chat_id_int)
        raw = int(s[4:]) if s.startswith("-100") else abs(chat_id_int)
    return f"https://t.me/c/{raw}/{msg_id_s}"
