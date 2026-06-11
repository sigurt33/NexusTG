#requires -Version 5.1
# Подготовить проект к переносу: остановить процессы, flush SQLite WAL, упаковать zip.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "[NexusTG migrate] 1/4 Stopping python processes..."
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 2

Write-Host "[NexusTG migrate] 2/4 Flushing SQLite WAL..."
if (Test-Path "data\app.db") {
    try {
        uv run python -c "import sqlite3; c=sqlite3.connect(r'data\app.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close(); print('WAL ok')"
    } catch {
        Write-Host "WAL checkpoint failed (probably fine): $_" -ForegroundColor Yellow
    }
}

Write-Host "[NexusTG migrate] 3/4 Building migration zip..."
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$out = "NexusTG_migrate_$stamp.zip"
$exclude = @('.venv', '__pycache__', 'backups', '*.pyc', 'logs', 'NexusTG_migrate_*.zip')

# собираем список файлов аккуратно (без .venv и кешей)
$files = Get-ChildItem -Force -Recurse | Where-Object {
    $rel = $_.FullName.Substring($root.Length).TrimStart('\','/')
    $skip = $false
    foreach ($p in $exclude) {
        if ($rel -like "$p*" -or $rel -like "*\$p\*" -or $rel -like "*/$p/*") { $skip = $true; break }
    }
    -not $skip -and -not $_.PSIsContainer
}

if (Test-Path $out) { Remove-Item $out -Force }
Compress-Archive -Path $files.FullName -DestinationPath $out -CompressionLevel Optimal -Force

Write-Host "[NexusTG migrate] 4/4 Done." -ForegroundColor Green
Write-Host "Archive: $out  ($([math]::Round((Get-Item $out).Length/1MB,1)) MB)"
Write-Host ""
Write-Host "Перенос на новый ПК:" -ForegroundColor Cyan
Write-Host "  1. Скопировать $out на новый ПК"
Write-Host "  2. Распаковать в любую папку"
Write-Host "  3. winget install astral-sh.uv  (если не стоит)"
Write-Host "  4. cd в распакованную папку"
Write-Host "  5. .\setup.ps1"
Write-Host "  6. .\run.ps1 в одном окне + .\web.ps1 в другом"
Write-Host ""
Write-Host "ВАЖНО: НЕ запускай старую копию параллельно — Telegram отзовёт сессию." -ForegroundColor Yellow
