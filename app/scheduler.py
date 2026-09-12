"""Фоновые задачи: дедлайны, уборка, проверка кругов.

Живёт в том же процессе, что и приложение: сервис однопроцессный
(--workers 1), потому что применение игровых событий должно быть строго
последовательным.
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import UTC, datetime, timedelta

from . import config, game_logic
from .db import audit, execute, now, query, transaction

TICK_SECONDS = 60
BACKUP_KEEP = 14        # сколько ежедневных копий храним


def close_expired_games() -> list[str]:
    """Игра с наступившим дедлайном завершается сама.

    Победителем становится лучший по устранениям, при равенстве — устранивший
    раньше. Ведущий может продлить дедлайн заранее, если игра не доиграна.
    """
    done: list[str] = []
    overdue = query(
        "SELECT id, title FROM game WHERE status = 'running' AND deadline_at IS NOT NULL"
        " AND deadline_at != '' AND deadline_at <= ?", (now()[:10],))
    for g in overdue:
        try:
            with transaction() as conn:
                game_logic.force_finish(conn, g["id"])
                audit(None, "game_finished_by_deadline", "game", g["id"], conn=conn)
            done.append(g["title"])
        except Exception:
            log_error(f"deadline:{g['id']}", traceback.format_exc())
    return done


ERROR_LOG_DAYS = 30
RATE_HIT_DAYS = 1


def _ago(days: int) -> str:
    """Момент N дней назад в том же формате, в каком мы храним время."""
    return (datetime.now(UTC) - timedelta(days=days)).replace(
        microsecond=0).isoformat()


def cleanup() -> int:
    """Истёкшие одноразовые коды, старые счётчики и давние ошибки."""
    removed = 0
    for sql, params in (
        ("DELETE FROM device_code WHERE expires_at < ?", (now(),)),
        ("DELETE FROM reset_code WHERE expires_at < ? AND used_at IS NULL", (now(),)),
        ("DELETE FROM rate_hit WHERE created_at < ?", (_ago(RATE_HIT_DAYS),)),
        ("DELETE FROM error_log WHERE created_at < ?", (_ago(ERROR_LOG_DAYS),)),
    ):
        try:
            removed += execute(sql, params).rowcount
        except Exception:  # noqa: S110 — журнал ошибок не должен всё ронять
            pass
    return removed


def check_chains() -> list[str]:
    """Регулярная проверка инварианта: круг обязан оставаться одним циклом."""
    from .db import connect
    broken = []
    conn = connect()
    for g in query("SELECT id, title FROM game WHERE status IN ('running','paused')"):
        res = game_logic.verify_chain(conn, g["id"])
        if not res["ok"]:
            broken.append(f"{g['title']}: {res['reason']}")
            log_error(f"chain:{g['id']}", f"{g['title']}: {res['reason']}")
    return broken


def log_error(path: str, message: str) -> None:
    try:
        execute("INSERT INTO error_log (path, message, traceback, created_at)"
                " VALUES (?, ?, ?, ?)", (path[:200], message.splitlines()[-1][:300],
                                         message[-4000:], now()))
    except Exception:  # noqa: S110 — журнал ошибок не должен всё ронять
        pass


def daily_backup() -> str | None:
    """Копия раз в сутки. Игра идёт неделю — потерять её на ровном месте нельзя.

    sqlite3.backup снимает консистентную копию на живой базе, останавливать
    сервис не нужно.
    """
    import sqlite3

    from .db import connect

    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"killer-auto-{now()[:10]}.db"
    target = config.BACKUP_DIR / name
    if target.exists():
        return None
    dst = sqlite3.connect(target)
    try:
        connect().backup(dst)
    finally:
        dst.close()
    # Чистим старые автокопии, ручные не трогаем.
    autos = sorted(config.BACKUP_DIR.glob("killer-auto-*.db"))
    for old in autos[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    audit(None, "backup_auto", "backup", None, {"file": name})
    return name


async def run_forever() -> None:
    while True:
        try:
            finished = close_expired_games()
            if finished:
                print(f"[киллер] завершены по дедлайну: {', '.join(finished)}")
            cleanup()
            broken = check_chains()
            if broken:
                print(f"[киллер] ВНИМАНИЕ, нарушен круг: {'; '.join(broken)}")
            made = daily_backup()
            if made:
                print(f"[киллер] бэкап: {made}")
        except Exception:
            log_error("scheduler", traceback.format_exc())
        await asyncio.sleep(TICK_SECONDS)
