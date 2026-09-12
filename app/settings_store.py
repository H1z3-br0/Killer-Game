"""Настройки платформы: хранятся в базе, читаются с кешем в процессе."""
from __future__ import annotations

import json

from . import config
from .db import execute, now, query

_cache: dict | None = None


def all_settings() -> dict:
    global _cache
    if _cache is None:
        stored = {r["key"]: json.loads(r["value_json"]) for r in query("SELECT * FROM setting")}
        _cache = {**config.DEFAULT_SETTINGS, **stored}
    return _cache


def reset_cache() -> None:
    """Сбросить кеш настроек — тестам и после восстановления базы."""
    global _cache
    _cache = None


def get(key: str):
    return all_settings().get(key, config.DEFAULT_SETTINGS.get(key))


def set_value(key: str, value, actor_user_id: int | None = None) -> None:
    global _cache
    execute(
        "INSERT INTO setting (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
        " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (key, json.dumps(value, ensure_ascii=False), now(), actor_user_id))
    _cache = None
