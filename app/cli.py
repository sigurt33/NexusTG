"""CLI: login | run | backup."""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
)

from app.config import DATA_DIR, SESSION_PATH, load_config
from app.db import connect, init_db

LOG_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FMT, stream=sys.stdout)


def _make_client(cfg) -> TelegramClient:
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        print("Ошибка: TG_API_ID и TG_API_HASH должны быть заданы в .env", file=sys.stderr)
        sys.exit(2)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(SESSION_PATH.with_suffix("")), cfg.tg_api_id, cfg.tg_api_hash)


async def cmd_login() -> int:
    cfg = load_config()
    client = _make_client(cfg)
    print("Вход в Telegram. Сейчас спросят телефон и код подтверждения.")
    await client.start(
        phone=lambda: input("Телефон (с +): ").strip(),
        code_callback=lambda: input("Код из Telegram: ").strip(),
        password=lambda: input("Пароль двухфакторной (или Enter, если нет): ").strip() or None,
    )
    me = await client.get_me()
    print(f"Успешно: вошёл как {me.first_name} (@{me.username}, id={me.id})")
    await client.disconnect()
    return 0


async def cmd_login_sms() -> int:
    cfg = load_config()
    # remove stale session so sign_in works cleanly
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
    client = _make_client(cfg)
    print("Login: requesting code from Telegram.")
    await client.connect()
    phone = input("Phone (+375...): ").strip()
    sent = await client.send_code_request(phone)
    delivery = sent.type.__class__.__name__
    print(f"Code delivery channel chosen by Telegram: {delivery}")
    print("  SentCodeTypeApp   -> open TG app, chat 'Telegram' (official)")
    print("  SentCodeTypeSms   -> SMS on your phone")
    print("  SentCodeTypeCall  -> phone call reading the digits")
    print("  SentCodeTypeFlashCall -> incoming call, code = last digits of caller number")
    for attempt in range(3):
        code = input("Code from SMS: ").strip()
        try:
            await client.sign_in(phone, code)
            break
        except SessionPasswordNeededError:
            pwd = input("2FA password: ").strip()
            await client.sign_in(password=pwd)
            break
        except PhoneCodeInvalidError:
            print("Wrong code, try again.")
        except PhoneCodeExpiredError:
            print("Code expired. Re-requesting...")
            sent = await client.send_code_request(phone)
    me = await client.get_me()
    if me is None:
        print("Login failed.", file=sys.stderr)
        await client.disconnect()
        return 2
    print(f"OK: signed in as {me.first_name} (@{me.username}, id={me.id})")
    await client.disconnect()
    return 0


async def cmd_run() -> int:
    from ingestion.backfill import run_backfill
    from ingestion.telegram_listener import State, run_listener
    from ingestion.chats_sync import sync_chats, archived_chat_ids
    from classifier.grok_worker import run_worker
    from classifier.consolidator import nightly_consolidate
    from ingestion.media import run_media_worker
    from notifications.scheduler import run_notifications
    from notifications.outbox_sender import run_outbox_sender

    cfg = load_config()
    await init_db()
    client = _make_client(cfg)

    if not SESSION_PATH.exists():
        print("Сессия не найдена. Сначала выполните: uv run python -m app.cli login", file=sys.stderr)
        return 2

    conn = await connect()
    state = State()

    print("Подключаюсь к Telegram...")
    await client.connect()
    if not await client.is_user_authorized():
        print("Сессия не авторизована. Запустите: uv run python -m app.cli login", file=sys.stderr)
        await client.disconnect()
        await conn.close()
        return 2

    await state.refresh_me(client)

    # синк списка чатов + кеш archived
    try:
        await sync_chats(client, conn)
        state.archived_chat_ids = await archived_chat_ids(conn)
        # processing=0 чаты тоже пропускаем
        cur = await conn.execute("SELECT chat_id FROM chats WHERE COALESCE(processing,1)=0")
        state.processing_off = {int(r[0]) for r in await cur.fetchall()}
        await cur.close()
        print(f"Архивных чатов: {len(state.archived_chat_ids)}, выключенных: {len(state.processing_off)} — пропускаем их.")
    except Exception as e:
        logging.getLogger(__name__).warning("sync_chats упал: %s", e)

    # запускаем консолидатор однократно при старте (раз в 24 часа внутри проверка)
    try:
        await nightly_consolidate(conn)
    except Exception as e:
        logging.getLogger(__name__).warning("Консолидатор при старте упал: %s", e)

    # backfill + classifier в фоне, параллельно с realtime listener
    backfill_task = asyncio.create_task(
        run_backfill(client, conn, state, cfg.backfill_days, cfg.context_window),
        name="backfill",
    )
    classifier_task = asyncio.create_task(
        run_worker(conn, cfg),
        name="classifier",
    )
    media_task = asyncio.create_task(
        run_media_worker(client, conn, cfg),
        name="media",
    )
    notify_task = asyncio.create_task(
        run_notifications(client, conn, cfg),
        name="notifications",
    )
    outbox_task = asyncio.create_task(
        run_outbox_sender(client, conn),
        name="outbox",
    )
    bot_task = None
    if cfg.tg_bot_token:
        from bot.main import run_bot
        bot_task = asyncio.create_task(run_bot(conn, cfg), name="tg_bot")
    else:
        logging.getLogger(__name__).info("TG_BOT_TOKEN не задан — TG-бот не запускается.")
    try:
        await run_listener(client, conn, state, cfg.context_window)
    except KeyboardInterrupt:
        print("Остановка по Ctrl+C...")
    finally:
        tasks_to_cancel = [backfill_task, classifier_task, media_task, notify_task, outbox_task]
        if bot_task is not None:
            tasks_to_cancel.append(bot_task)
        for t in tasks_to_cancel:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await client.disconnect()
        await conn.close()
    return 0


async def cmd_sync_chats() -> int:
    """Обновить таблицу chats (archived-флаги) из Telegram."""
    from ingestion.chats_sync import sync_chats
    cfg = load_config()
    await init_db()
    client = _make_client(cfg)
    if not SESSION_PATH.exists():
        print("Сессия не найдена. Сначала: uv run python -m app.cli login", file=sys.stderr)
        return 2
    conn = await connect()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("Сессия не авторизована.", file=sys.stderr)
            return 2
        total, archived = await sync_chats(client, conn)
        print(f"Чатов всего: {total}, в архиве: {archived} (они теперь будут пропускаться).")
    finally:
        await client.disconnect()
        await conn.close()
    return 0


async def cmd_classify() -> int:
    """Запустить ТОЛЬКО classifier-воркер (без ingestion)."""
    from classifier.grok_worker import run_worker
    from classifier.consolidator import nightly_consolidate

    cfg = load_config()
    await init_db()
    conn = await connect()
    try:
        await nightly_consolidate(conn)
        await run_worker(conn, cfg)
    except KeyboardInterrupt:
        print("Остановка по Ctrl+C...")
    finally:
        await conn.close()
    return 0


async def cmd_classify_batch(limit: int) -> int:
    """One-shot: классифицировать до `limit` сообщений и показать сводку."""
    from classifier.grok_worker import _make_client, run_one_round, classify_one
    from classifier import prompts as P

    cfg = load_config()
    if not cfg.xai_api_key:
        print("Ошибка: XAI_API_KEY не задан в .env", file=sys.stderr)
        return 2
    await init_db()
    conn = await connect()
    try:
        client = _make_client(cfg.xai_api_key, cfg.llm_base_url)
        client._input_per_m = cfg.llm_input_usd_per_m
        client._output_per_m = cfg.llm_output_usd_per_m

        # количество тем до
        cur = await conn.execute("SELECT COUNT(*) FROM topics")
        topics_before = (await cur.fetchone())[0]
        await cur.close()

        # обработать партиями по 10, пока не достигнем limit или не кончатся
        remaining = limit
        total_classified = 0
        processed_ids: list[str] = []
        while remaining > 0:
            batch = min(remaining, 10)
            # сохраним список id, которые будут обработаны (до классификации)
            cur = await conn.execute(
                """
                SELECT id FROM messages
                WHERE is_context_only=0 AND deleted_at IS NULL
                  AND id NOT IN (SELECT message_id FROM priorities)
                ORDER BY date_utc DESC LIMIT ?
                """,
                (batch,),
            )
            ids = [r[0] for r in await cur.fetchall()]
            await cur.close()
            if not ids:
                break
            n = await run_one_round(conn, cfg, client, limit=batch)
            total_classified += n
            processed_ids.extend(ids)
            remaining -= batch
            if n == 0:
                # вероятнее всего 429 / временный сбой — подождём и повторим
                await asyncio.sleep(15)

        cur = await conn.execute("SELECT COUNT(*) FROM topics")
        topics_after = (await cur.fetchone())[0]
        await cur.close()

        # сводка: 3 примера
        cur = await conn.execute(
            """
            SELECT m.id, m.chat_title, m.sender_name, m.text,
                   p.urgency, p.importance, p.score, p.rationale
            FROM priorities p
            JOIN messages m ON m.id = p.message_id
            WHERE p.message_id IN ({})
            ORDER BY p.score DESC
            LIMIT 3
            """.format(",".join("?" for _ in processed_ids) or "''"),
            processed_ids if processed_ids else [],
        )
        samples = [dict(r) for r in await cur.fetchall()]
        await cur.close()

        # темы для каждого примера
        sample_topics: dict[str, list[str]] = {}
        for s in samples:
            cur = await conn.execute(
                """
                SELECT t.label_ru FROM message_topics mt
                JOIN topics t ON t.id = mt.topic_id
                WHERE mt.message_id = ?
                """,
                (s["id"],),
            )
            sample_topics[s["id"]] = [r[0] for r in await cur.fetchall()]
            await cur.close()

        # расход токенов
        cur = await conn.execute(
            "SELECT prompt_tokens, completion_tokens, cost_usd FROM grok_usage WHERE date = date('now')"
        )
        urow = await cur.fetchone()
        await cur.close()
        pt = ct = 0
        cost = 0.0
        if urow:
            pt, ct, cost = int(urow[0]), int(urow[1]), float(urow[2])

        print("\n=== Сводка classify-batch ===")
        print(f"Запрошено к обработке:        {limit}")
        print(f"Классифицировано сообщений:  {total_classified}")
        print(f"Тем было / стало:            {topics_before} -> {topics_after} (+{topics_after - topics_before})")
        print(f"Токены сегодня:              prompt={pt}, completion={ct}, стоимость ${cost:.4f}")
        print(f"Бюджет:                      {cfg.grok_daily_token_budget} токенов/день")
        print("\nПримеры (топ-3 по score):")
        for i, s in enumerate(samples, 1):
            text = (s["text"] or "").replace("\n", " ").strip()
            if len(text) > 160:
                text = text[:160] + "..."
            tops = ", ".join(sample_topics.get(s["id"], [])) or "—"
            print(f"\n[{i}] chat={s['chat_title']} | sender={s['sender_name']}")
            print(f"    text: {text}")
            print(f"    темы: {tops}")
            print(f"    urgency={s['urgency']} importance={s['importance']} score={s['score']:.2f}")
            print(f"    rationale: {s['rationale']}")
    finally:
        await conn.close()
    return 0


async def cmd_bot() -> int:
    """Запустить только TG-бот (для отладки)."""
    from bot.main import run_bot
    cfg = load_config()
    if not cfg.tg_bot_token:
        print("TG_BOT_TOKEN не задан в .env", file=sys.stderr)
        return 2
    await init_db()
    conn = await connect()
    try:
        await run_bot(conn, cfg)
    except KeyboardInterrupt:
        pass
    finally:
        await conn.close()
    return 0


async def cmd_notify() -> int:
    """Запустить только scheduler уведомлений (для отладки)."""
    from notifications.scheduler import run_notifications

    cfg = load_config()
    if not cfg.notify_tg_self and not cfg.notify_windows_toast:
        print("Notifications disabled in config")
        return 0
    await init_db()
    conn = await connect()
    client = None
    if cfg.notify_tg_self:
        client = _make_client(cfg)
        if not SESSION_PATH.exists():
            print("Сессия не найдена. Сначала: uv run python -m app.cli login", file=sys.stderr)
            return 2
        await client.connect()
        if not await client.is_user_authorized():
            print("Сессия не авторизована.", file=sys.stderr)
            await client.disconnect()
            return 2
    try:
        await run_notifications(client, conn, cfg)
    except KeyboardInterrupt:
        print("Остановка по Ctrl+C...")
    finally:
        if client is not None:
            await client.disconnect()
        await conn.close()
    return 0


def cmd_web() -> int:
    """Запустить uvicorn с веб-дашбордом."""
    import uvicorn
    uvicorn.run(
        "web.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )
    return 0


def cmd_backup() -> int:
    backups = Path(__file__).resolve().parent.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = backups / f"data_{stamp}.zip"
    if not DATA_DIR.exists():
        print("Папка data/ отсутствует — нечего бэкапить.", file=sys.stderr)
        return 1
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in DATA_DIR.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(DATA_DIR.parent))
    print(f"Бэкап создан: {out}")
    return 0


def main() -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="NexusTG", description="Локальный агрегатор Telegram-задач")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Войти в Telegram и сохранить сессию")
    sub.add_parser("login-sms", help="Войти с принудительной отправкой кода по SMS")
    sub.add_parser("run", help="Запустить ingestion + classifier (backfill + realtime)")
    sub.add_parser("backup", help="Создать zip-бэкап папки data/")
    sub.add_parser("web", help="Запустить веб-дашборд на 127.0.0.1:8000")
    sub.add_parser("classify", help="Запустить только classifier-воркер")
    sub.add_parser("sync-chats", help="Обновить список архивных чатов из Telegram")
    sub.add_parser("notify", help="Запустить только scheduler уведомлений")
    sub.add_parser("bot", help="Запустить только TG-бот для inbox-уведомлений")
    p_cb = sub.add_parser("classify-batch", help="Разово классифицировать N сообщений")
    p_cb.add_argument("--limit", type=int, default=20, help="Сколько сообщений обработать (по умолчанию 20)")

    args = parser.parse_args()
    if args.cmd == "login":
        sys.exit(asyncio.run(cmd_login()))
    elif args.cmd == "login-sms":
        sys.exit(asyncio.run(cmd_login_sms()))
    elif args.cmd == "run":
        sys.exit(asyncio.run(cmd_run()))
    elif args.cmd == "backup":
        sys.exit(cmd_backup())
    elif args.cmd == "web":
        sys.exit(cmd_web())
    elif args.cmd == "classify":
        sys.exit(asyncio.run(cmd_classify()))
    elif args.cmd == "classify-batch":
        sys.exit(asyncio.run(cmd_classify_batch(args.limit)))
    elif args.cmd == "sync-chats":
        sys.exit(asyncio.run(cmd_sync_chats()))
    elif args.cmd == "notify":
        sys.exit(asyncio.run(cmd_notify()))
    elif args.cmd == "bot":
        sys.exit(asyncio.run(cmd_bot()))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
