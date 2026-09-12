"""Настройки сервиса. Всё берётся из переменных окружения с рабочими
значениями по умолчанию — сервис должен запускаться без конфигурации."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DB_PATH = Path(os.getenv("KILLER_DB", BASE_DIR / "data" / "killer.db"))
BACKUP_DIR = Path(os.getenv("KILLER_BACKUPS", BASE_DIR / "data" / "backups"))
MIGRATIONS_DIR = BASE_DIR / "migrations"

SESSION_COOKIE = "killer_session"
CSRF_COOKIE = "killer_csrf"
SESSION_DAYS = int(os.getenv("KILLER_SESSION_DAYS", "60"))

MIN_PLAYERS = 3
DEVICE_CODE_TTL_SECONDS = 120
DEVICE_CODE_ATTEMPTS = 3
RESET_CODE_TTL_MINUTES = 30

# Ограничения частоты: (сколько попыток, за сколько секунд)
RATE_LIMITS = {
    "login": (10, 300),
    "register": (5, 3600),
    "support": (5, 3600),
    "claim": (30, 3600),
}

DEFAULT_SETTINGS = {
    "platform_name": "Киллер",
    "support_telegram": "",
    "allow_anyone_create_game": True,
    "allow_multiple_active_games": True,
    "maintenance_message": "",
    "subnet_allowlist": "",  # пусто = выключено
}

GAME_COLORS = [
    "#D9A441", "#D9483B", "#5FA88B", "#6A8CC7",
    "#B07BC4", "#C77B5F", "#7FA8C9", "#A8B84F",
]
