"""Одноразовый скрипт: отправить инструкцию в чат с ботом, закрепить её и обновить
список команд бота (видимый в меню «/» в Telegram).

Запуск:  uv run python -m bot.pin_help
"""
from __future__ import annotations

import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from telethon import TelegramClient
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from app.config import DATA_DIR, load_config

BOT_SESSION_PATH = DATA_DIR / "tg_bot.session"

HELP_TEXT = (
    "📖 *NexusTG · Краткая инструкция*\n"
    "\n"
    "Бот присылает только важное из Telegram: ЛС, упоминания и ответы вам.\n"
    "\n"
    "*Команды:*\n"
    "• /inbox — топ важных сообщений\n"
    "• /tasks — открытые задачи\n"
    "• /digest — сводка за вчера\n"
    "• /help — эта инструкция\n"
    "\n"
    "*Кнопки под сообщением:*\n"
    "✅ Готово · 💤 1 ч · 🌅 Завтра · 📦 Архив · 📋 В задачник · 🌐 Дашборд\n"
    "\n"
    "Веб: http://127.0.0.1:8000/"
)

COMMANDS = [
    ("start", "Приветствие и краткая справка"),
    ("inbox", "Топ-10 активных задач (важные сообщения)"),
    ("tasks", "Открытые задачи из задачника"),
    ("digest", "Сводка важного за вчера"),
    ("help", "Подробная инструкция и список кнопок"),
]


async def main() -> None:
    cfg = load_config()
    if not cfg.tg_bot_token:
        raise SystemExit("TG_BOT_TOKEN не задан в .env — бот не настроен.")
    if not cfg.tg_my_id:
        raise SystemExit("TG_MY_ID не задан в .env — некуда отправлять.")
    if not (cfg.tg_api_id and cfg.tg_api_hash):
        raise SystemExit("TG_API_ID/TG_API_HASH нужны и для бота.")

    bot = TelegramClient(str(BOT_SESSION_PATH.with_suffix("")), cfg.tg_api_id, cfg.tg_api_hash)
    await bot.start(bot_token=cfg.tg_bot_token)
    try:
        # 0. Снять прошлые закрепы (чтобы не плодить пины при повторных запусках)
        try:
            from telethon.tl.functions.messages import UnpinAllMessagesRequest
            await bot(UnpinAllMessagesRequest(peer=cfg.tg_my_id))
        except Exception as e:
            print(f"⚠ unpin_all: {e}")

        # 1. Отправить и закрепить инструкцию
        msg = await bot.send_message(cfg.tg_my_id, HELP_TEXT, parse_mode="markdown", link_preview=False)
        await bot.pin_message(cfg.tg_my_id, msg, notify=False)
        print(f"✅ Инструкция отправлена и закреплена (msg id={msg.id})")

        # 2. Обновить список команд бота (видим в / меню)
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=[BotCommand(command=c, description=d) for c, d in COMMANDS],
        ))
        print(f"✅ Список команд бота обновлён ({len(COMMANDS)} команд)")
    finally:
        await bot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
