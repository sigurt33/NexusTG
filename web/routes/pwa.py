"""PWA-роуты: отдают service worker и манифест с корня сайта.

Service worker обязан обслуживаться с корня (`/sw.js`), иначе его scope
ограничится `/static/` и он не сможет контролировать навигацию по всему
сайту. Манифест тоже отдаём с корня, чтобы `scope: "/"` был валиден.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from web.app import STATIC_DIR

router = APIRouter()


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )
