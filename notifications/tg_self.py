"""Telegram self-ping: send to 'me' (Saved Messages)."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def notify_tg(client, title: str, body: str, url: str) -> None:
    if client is None:
        return
    text = f"🔔 {title}\n{body}\n{url}"
    try:
        await client.send_message("me", text, link_preview=False)
    except Exception as e:
        log.warning("notify_tg упал: %s", e)
