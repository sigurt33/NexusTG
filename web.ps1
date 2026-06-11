#!/usr/bin/env pwsh
# Запуск веб-дашборда: http://127.0.0.1:8000
Set-Location -Path $PSScriptRoot
uv run python -m app.cli web
