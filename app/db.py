"""Работа с SQLite: подключение, миграции, транзакции.

Соединение одно на процесс и защищено блокировкой: SQLite не любит
параллельную запись, а применение игровых событий обязано быть
последовательным (см. раздел «Гонка событий» в ТЗ).
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from . import config

_local = threading.local()
_write_lock = threading.RLock()


def now() -> str:
    """Единая точка времени: всегда UTC, ISO-8601 с секундами."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def reset_connection() -> None:
    """Закрыть соединение потока — нужно тестам, чтобы сменить файл базы."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Запись всегда идёт через эту обёртку: одна пишущая транзакция за раз."""
    conn = connect()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    """Одиночная запись вне явной транзакции."""
    with transaction() as conn:
        return conn.execute(sql, params)


def migrate() -> list[str]:
    """Нумерованные .sql-файлы применяются по одному разу.

    Обновление сервиса посреди недельной игры не должно требовать
    пересоздания базы, поэтому версия схемы хранится в самой базе.
    """
    conn = connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migration")}
    fresh: list[str] = []
    for path in sorted(config.MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        # Миграция и отметка о её применении неразделимы: при сбое она не
        # должна примениться дважды.
        script = "BEGIN IMMEDIATE;\n{}\nINSERT INTO schema_migration (name, applied_at)"\
                 " VALUES ('{}', '{}');\nCOMMIT;".format(
                     path.read_text(encoding="utf-8"), path.name.replace("'", "''"), now())
        with _write_lock:
            conn.executescript(script)
        fresh.append(path.name)
    return fresh


def audit(actor_user_id: int | None, action: str, target_type: str = "",
          target_id: int | None = None, payload: dict[str, Any] | None = None,
          conn: sqlite3.Connection | None = None) -> None:
    """Все административные действия обязаны оставлять след."""
    import json
    sql = ("INSERT INTO audit_log (actor_user_id, action, target_type, target_id,"
           " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)")
    args = (actor_user_id, action, target_type, target_id,
            json.dumps(payload or {}, ensure_ascii=False), now())
    if conn is not None:
        conn.execute(sql, args)
    else:
        execute(sql, args)
