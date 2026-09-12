#!/usr/bin/env bash
# Запуск сервиса «Киллер» в локальной сети.
# Слушаем 0.0.0.0, чтобы сервис был виден с телефонов в той же сети.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --quiet -r requirements.txt
fi

PORT="${PORT:-8000}"
echo "Киллер запускается на порту $PORT"
echo "Адрес для телефонов: http://$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1
