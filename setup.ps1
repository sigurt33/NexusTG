#requires -Version 5.1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "[NexusTG] Checking uv..."
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found. Install: winget install astral-sh.uv  (or: irm https://astral.sh/uv/install.ps1 | iex)" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "[NexusTG] Created .env from .env.example - fill TG_API_ID, TG_API_HASH, XAI_API_KEY and run setup.ps1 again." -ForegroundColor Yellow
        exit 1
    }
}

Write-Host "[NexusTG] uv sync..."
uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

New-Item -ItemType Directory -Force -Path "data" | Out-Null

if (-not (Test-Path "data/tg.session")) {
    Write-Host "[NexusTG] Telegram session not found - starting interactive login."
    uv run python -m app.cli login
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    $size = (Get-Item "data/tg.session").Length
    Write-Host "[NexusTG] Session found: data/tg.session ($size bytes)"
    # Сессия меньше 4 KB почти наверняка пустая (Telethon создаёт файл до завершения логина)
    if ($size -lt 4096) {
        Write-Host "[NexusTG] Session looks incomplete (too small)." -ForegroundColor Yellow
        $ans = Read-Host "Re-login from scratch? [Y/n]"
        if ($ans -eq "" -or $ans -match "^[Yy]") {
            Remove-Item "data/tg.session" -Force
            uv run python -m app.cli login
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
    } else {
        $ans = Read-Host "Use existing session? [Y/n]"
        if ($ans -match "^[Nn]") {
            Remove-Item "data/tg.session" -Force
            uv run python -m app.cli login
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
    }
}

Write-Host "[NexusTG] Done. Now run: .\run.ps1"
