"""Windows toast notifications via windows-toasts.

Falls back to no-op if library missing or not on Windows.
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

_TOASTER = None
_AVAILABLE: bool | None = None


def _ensure_toaster():
    global _TOASTER, _AVAILABLE
    if _AVAILABLE is not None:
        return _TOASTER
    if sys.platform != "win32":
        _AVAILABLE = False
        log.info("windows-toasts: не Windows, тосты отключены")
        return None
    try:
        from windows_toasts import WindowsToaster  # type: ignore
    except Exception as e:
        _AVAILABLE = False
        log.warning("windows-toasts недоступен: %s — тосты отключены", e)
        return None
    try:
        _TOASTER = WindowsToaster("NexusTG")
        _AVAILABLE = True
    except Exception as e:
        _AVAILABLE = False
        log.warning("WindowsToaster init упал: %s", e)
        return None
    return _TOASTER


async def notify_toast(title: str, body: str, url: str | None) -> None:
    toaster = _ensure_toaster()
    if toaster is None:
        return
    try:
        from windows_toasts import Toast  # type: ignore
        t = Toast()
        t.text_fields = [title, body]
        if url:
            def _open(_evt, _url=url):
                try:
                    os.startfile(_url)  # noqa
                except Exception as e:
                    log.warning("startfile упал: %s", e)
            t.on_activated = _open
        toaster.show_toast(t)
    except Exception as e:
        log.warning("notify_toast упал: %s", e)
