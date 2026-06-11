"""Конфигурация: .env + config.toml → dataclass."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SESSION_PATH = DATA_DIR / "tg.session"
DB_PATH = DATA_DIR / "app.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
LOG_DIR = DATA_DIR / "logs"


@dataclass(frozen=True)
class Config:
    tg_api_id: int
    tg_api_hash: str
    xai_api_key: str
    timezone: str
    active_hours_start: str
    active_hours_end: str
    backfill_days: int
    context_window: int
    notify_windows_toast: bool
    notify_tg_self: bool
    ui_lang: str
    grok_model: str
    grok_daily_token_budget: int
    llm_base_url: str
    llm_input_usd_per_m: float
    llm_output_usd_per_m: float
    tg_bot_token: str
    tg_my_id: int
    notify_tg_bot: bool


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    cfg_path = ROOT / "config.toml"
    with cfg_path.open("rb") as f:
        toml_cfg = tomllib.load(f)

    api_id_raw = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    xai_key = os.getenv("XAI_API_KEY", "").strip()
    bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
    my_id_raw = os.getenv("TG_MY_ID", "").strip()
    try:
        my_id = int(my_id_raw) if my_id_raw else 0
    except ValueError:
        my_id = 0

    try:
        api_id = int(api_id_raw) if api_id_raw else 0
    except ValueError:
        api_id = 0

    return Config(
        tg_api_id=api_id,
        tg_api_hash=api_hash,
        xai_api_key=xai_key,
        timezone=toml_cfg.get("timezone", "Europe/Minsk"),
        active_hours_start=toml_cfg.get("active_hours_start", "10:00"),
        active_hours_end=toml_cfg.get("active_hours_end", "18:30"),
        backfill_days=int(toml_cfg.get("backfill_days", 30)),
        context_window=int(toml_cfg.get("context_window", 5)),
        notify_windows_toast=bool(toml_cfg.get("notify_windows_toast", True)),
        notify_tg_self=bool(toml_cfg.get("notify_tg_self", True)),
        ui_lang=toml_cfg.get("ui_lang", "ru"),
        grok_model=toml_cfg.get("grok_model", "gemini-1.5-pro"),
        grok_daily_token_budget=int(toml_cfg.get("grok_daily_token_budget", 200000)),
        llm_base_url=toml_cfg.get("llm_base_url", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        llm_input_usd_per_m=float(toml_cfg.get("llm_input_usd_per_m", 1.25)),
        llm_output_usd_per_m=float(toml_cfg.get("llm_output_usd_per_m", 5.00)),
        tg_bot_token=bot_token,
        tg_my_id=my_id,
        notify_tg_bot=bool(toml_cfg.get("notify_tg_bot", False)),
    )
