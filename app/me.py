"""Кешируем мини-профиль пользователя (ник + аватар) для веб-шапки."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from telethon import TelegramClient

from app.config import DATA_DIR, SESSION_PATH, load_config

log = logging.getLogger(__name__)

WEB_STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
ME_JSON = DATA_DIR / "me.json"
ME_AVATAR = WEB_STATIC / "me.jpg"


def load_cached_me() -> dict | None:
    if not ME_JSON.exists():
        return None
    try:
        d = json.loads(ME_JSON.read_text(encoding="utf-8"))
        d["avatar_url"] = "/static/me.jpg" if ME_AVATAR.exists() else None
        return d
    except Exception:
        return None


async def fetch_and_cache_me() -> dict | None:
    """Скачать профиль пользователя (юзер-сессия) и положить в data/me.json + web/static/me.jpg."""
    cfg = load_config()
    if not SESSION_PATH.exists():
        return None
    client = TelegramClient(str(SESSION_PATH.with_suffix("")), cfg.tg_api_id, cfg.tg_api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        data = {
            "id": me.id,
            "username": me.username or "",
            "first_name": me.first_name or "",
            "last_name": me.last_name or "",
        }
        try:
            await client.download_profile_photo("me", file=str(ME_AVATAR))
        except Exception as e:
            log.warning("Не удалось скачать аватар: %s", e)
        ME_JSON.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    except Exception as e:
        log.warning("fetch_me упал: %s", e)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
