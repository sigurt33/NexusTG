# Деплой NexusTG на Linux VPS (доступ через Tailscale)

Цель: NexusTG работает 24/7 на сервере. Бот — твой телефонный «апп» для быстрого закрытия
вопросов в Telegram. Веб-инбокс доступен с телефона/ноута через приватную сеть Tailscale
(бесплатно), устанавливается на домашний экран как приложение (PWA).

> ⚠️ **Главное правило.** Сессия Telethon (`data/tg.session`) — это полный доступ к твоему
> Telegram-аккаунту, и она одна. Нельзя гонять `app.cli run` на сервере И на ноутбуке
> одновременно — Telegram отзовёт сессию. После запуска на сервере **на Windows `run`
> больше не запускаем.** Сервер — единственный дом NexusTG.

---

## Архитектура

```
        телефон / ноут (приложение Tailscale)
                    │  приватный HTTPS (tailnet)
                    ▼
        VPS  ──  tailscale serve  ──►  127.0.0.1:8000  (nexustg-web / uvicorn)
              └─ nexustg-run (ingestion + classifier + bot)  ──►  Telegram
                    │
                    ▼
              data/app.db (SQLite, WAL)
```

- Веб слушает **только localhost** — наружу его отдаёт `tailscale serve`. Портов в интернет нет.
- Бот пишет тебе в Telegram сам — работает откуда угодно, без Tailscale.
- Tailscale free tier: до 100 устройств, для личного использования — $0.

---

## Фаза 1 — Провижн сервера

1. **Создать VPS.** Любой дешёвый: Hetzner CX22 (~€4/мес), Timeweb, Aeza, и т.п.
   - ОС: **Ubuntu 24.04 LTS**.
   - RAM 1–2 ГБ хватит (классификатор — внешний API, локально модель не крутится).
2. **Зайти по SSH, создать пользователя `nexustg`** (не работать под root):
   ```bash
   adduser --disabled-password --gecos "" nexustg
   usermod -aG sudo nexustg
   mkdir -p /home/nexustg/.ssh && cp ~/.ssh/authorized_keys /home/nexustg/.ssh/
   chown -R nexustg:nexustg /home/nexustg/.ssh && chmod 700 /home/nexustg/.ssh
   ```
3. **Файрвол** (наружу нужен только SSH; веб ходит через Tailscale):
   ```bash
   ufw allow 22/tcp && ufw --force enable
   ```
4. **Swap** (если RAM 1 ГБ — чтобы `uv sync` не словил OOM):
   ```bash
   fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
   echo '/swapfile none swap sw 0 0' >> /etc/fstab
   ```

## Фаза 2 — Tailscale

1. На VPS:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   Открой выданную ссылку, авторизуйся (Google/email — тот же аккаунт, что поставишь на телефон).
2. На телефоне и ноуте — поставь приложение **Tailscale**, войди тем же аккаунтом.
3. Запомни имя машины в tailnet: `tailscale status` покажет адрес вида
   `nexustg.<твой-хвост>.ts.net`.

## Фаза 3 — Код и зависимости

Под пользователем `nexustg`:

1. **uv:**
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   source ~/.bashrc   # чтобы uv появился в PATH (~/.local/bin)
   ```
2. **Репозиторий** (публичный — клонируется без ключей):
   ```bash
   cd ~ && git clone https://github.com/sigurt33/NexusTG.git
   cd NexusTG && uv sync
   ```
3. **Секреты** — создать `.env` (НЕ из git):
   ```bash
   cp .env.example .env && nano .env
   ```
   Заполнить: `TG_API_ID`, `TG_API_HASH`, `XAI_API_KEY`, `TG_BOT_TOKEN`, `TG_MY_ID`.
   Значения с `$` — в одинарных кавычках.
4. **config.toml — два изменения под сервер:**
   ```toml
   notify_windows_toast = false   # на Linux тостов нет (и так no-op, но честнее false)
   notify_tg_bot = true           # ВКЛючить пуш инбокса в бота — иначе бот молчит
   ```

## Фаза 4 — Telegram-сессия и история

1. **Перенести историю** (опционально, но желательно — сохранит темы, задачи, правила
   «Алина-режим», обучающие примеры). С Windows-машины скопировать **только `app.db`**
   (НЕ `tg.session`):
   ```powershell
   # на Windows, NexusTG остановлен:
   scp .\data\app.db nexustg@<tailscale-ip>:/home/nexustg/NexusTG/data/app.db
   ```
   Если истории не жалко — пропусти, сервер сделает backfill за `backfill_days` сам.
2. **Логин на сервере** (интерактивный — нужен телефон + код + 2FA). Создаёт **новую**
   сессию на сервере, независимую от Windows:
   ```bash
   mkdir -p data
   uv run python -m app.cli login
   ```
3. **На Windows — остановить NexusTG навсегда** (иначе два листенера на один аккаунт):
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     ? { $_.CommandLine -like "*app.cli*" } | % { Stop-Process -Id $_.ProcessId -Force }
   ```

## Фаза 5 — systemd-сервисы

```bash
sudo cp deploy/nexustg-run.service deploy/nexustg-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexustg-run nexustg-web
systemctl status nexustg-run nexustg-web --no-pager
```
Логи: `journalctl -u nexustg-run -f` и `journalctl -u nexustg-web -f`.

> Юниты рассчитаны на пользователя `nexustg`, путь `/home/nexustg/NexusTG` и
> `uv` в `~/.local/bin`. Если иначе — поправь `User`/`WorkingDirectory`/`ExecStart`.

## Фаза 6 — Отдать веб в tailnet

```bash
sudo tailscale serve --bg 8000
```
Это публикует `127.0.0.1:8000` как `https://nexustg.<хвост>.ts.net` **только внутри твоей
tailnet** (с автоматическим TLS). Проверь: `tailscale serve status`.

С телефона (с включённым Tailscale) открой `https://nexustg.<хвост>.ts.net` →
**«Добавить на главный экран»** → получишь иконку-приложение (PWA).

## Фаза 7 — Бэкапы

`app.cli backup` уже умеет паковать `data/` в zip. Поставить cron (ежедневно в 4:00):
```bash
crontab -e
# добавить:
0 4 * * * cd /home/nexustg/NexusTG && /home/nexustg/.local/bin/uv run python -m app.cli backup
```
Ротация и выгрузка в облако (S3/B2) — по желанию; **бэкап считается рабочим только после
проверенного восстановления** (распакуй zip в тест и открой `app.db`).

---

## Обновление кода (деплой новой версии)

```bash
ssh nexustg@<tailscale-ip>
cd ~/NexusTG && bash deploy/update.sh
```
`update.sh`: pull → `uv sync` (если менялся манифест) → restart сервисов → healthcheck.

Поток разработки: правки на Windows → коммит → `git push` → на сервере `bash deploy/update.sh`.

---

## Вариант: хост на своём ПК (Windows) — для пробы перед VPS

Тот же подход, но «сервером» работает твой Windows-ПК. Переезд на VPS потом — 1:1
(меняется только то, где крутятся `run`/`web`; Tailscale и PWA идентичны).

> ⚠️ Бот и приложение работают, **только пока ПК включён и не спит**. Это главное
> отличие от VPS. Поставь в схеме питания «не уходить в сон», если хочешь стабильности.

1. **Запустить обе программы** (в двух окнах PowerShell, из корня репо):
   ```powershell
   .\run.ps1    # бот + ingestion + classifier
   .\web.ps1    # веб/приложение на http://127.0.0.1:8000
   ```
   Для бота проверь `config.toml`: `notify_tg_bot = true`.
2. **Tailscale на Windows + телефон** (один аккаунт):
   - Поставить с https://tailscale.com/download/windows, войти.
   - На телефоне — приложение Tailscale, тот же аккаунт.
3. **Отдать веб в свою tailnet** (даёт HTTPS, нужный для установки PWA):
   ```powershell
   tailscale serve --bg 8000
   tailscale serve status   # покажет адрес https://<имя-пк>.<хвост>.ts.net
   ```
4. На телефоне (с включённым Tailscale) открыть этот `https://…ts.net` →
   «Добавить на главный экран» → иконка-приложение. На ноутбуке — тот же адрес в браузере.
5. **Всегда-онлайн на ПК (опционально):** прописать `run`/`web` в «Планировщик заданий»
   с триггером «При входе в систему», либо обернуть через NSSM в службы Windows.

Когда захочешь «всегда онлайн без зависимости от ПК» — переноси на VPS по фазам 1–7 выше
(скопировать `data/app.db`, заново `login` на сервере, на ПК `run`/`web` остановить).

---

## Грабли (специфичные для NexusTG)

- **Две сессии одного аккаунта.** Не запускать `app.cli run` на сервере и на ноуте разом.
  После переезда — Windows-инстанс не трогаем.
- **Веб-кнопка «Синхронизировать чаты» (`/chats/sync`)** открывает ту же `tg.session`, что и
  `run`. На одной машине одновременно это может конфликтовать. Sync и так идёт в `run` —
  кнопкой без нужды не пользоваться.
- **CP1251 / кириллица в stdout** — на сервере неактуально (Linux UTF-8), но в юнитах на
  всякий случай задан `PYTHONIOENCODING=utf-8`.
- **`tg.session` — это доступ к аккаунту.** Лежит в `data/` (gitignored). Бэкапы с ней
  хранить так же бережно, как пароль.
- **Веб без аутентификации.** Поэтому он и не выставляется в интернет напрямую — только
  через приватную tailnet. Не делай `tailscale funnel` (это публикует наружу) и не открывай
  порт 8000 в `ufw`.
