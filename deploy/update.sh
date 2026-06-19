#!/usr/bin/env bash
# NexusTG — единая команда деплоя на сервере.
# pull → uv sync (если менялся манифест) → restart сервисов → healthcheck.
# Запускать из корня репозитория:  bash deploy/update.sh
set -euo pipefail

cd "$(dirname "$0")/.."   # корень репо

echo "==> git pull"
BEFORE=$(git rev-parse HEAD)
git pull --ff-only
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    echo "    нет новых коммитов ($AFTER)"
fi

# uv sync только если поменялся pyproject.toml или uv.lock
if [ "$BEFORE" = "$AFTER" ] || git diff --name-only "$BEFORE" "$AFTER" | grep -qE '^(pyproject\.toml|uv\.lock)$'; then
    echo "==> uv sync"
    uv sync
else
    echo "==> зависимости не менялись, uv sync пропущен"
fi

echo "==> restart сервисов"
sudo systemctl restart nexustg-run nexustg-web

echo "==> healthcheck"
ok=0
for i in $(seq 1 10); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        ok=1; break
    fi
    sleep 2
done

if [ "$ok" = "1" ]; then
    echo "    web /health: OK"
else
    echo "    web /health: FAIL — смотри: journalctl -u nexustg-web -n 50" >&2
    exit 1
fi

if systemctl is-active --quiet nexustg-run; then
    echo "    nexustg-run: active"
else
    echo "    nexustg-run: НЕ active — смотри: journalctl -u nexustg-run -n 50" >&2
    exit 1
fi

echo "==> Готово. HEAD=$AFTER"
