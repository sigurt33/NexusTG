"""Чтение/запись плоского config.toml без сторонних зависимостей."""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"


def read_raw() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def write_config_values(updates: dict) -> None:
    """Слить updates в текущий config.toml и переписать (плоский, без таблиц)."""
    data = read_raw()
    data.update(updates)
    lines = [f"{k} = {_fmt(v)}" for k, v in data.items()]
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def live_reminder_hours(default: int = 3) -> int:
    """Прочитать часы до дедлайна заново из файла (для живого применения)."""
    try:
        return int(read_raw().get("task_reminder_hours_before", default))
    except Exception:
        return default
