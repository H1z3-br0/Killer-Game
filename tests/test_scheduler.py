"""Фоновые задачи: дедлайны, уборка, проверка кругов, бэкапы."""
from __future__ import annotations

from conftest import chain_of

from app import config, db, scheduler


def test_deadline_finishes_game(game_factory, people):
    gid = game_factory(start=True, deadline_at="2000-01-01")
    assert db.query_one("SELECT status FROM game WHERE id = ?", (gid,))["status"] == "running"

    finished = scheduler.close_expired_games()
    game = db.query_one("SELECT * FROM game WHERE id = ?", (gid,))
    assert finished and game["status"] == "finished" and game["winner_participant_id"]


def test_future_deadline_is_left_alone(game_factory):
    gid = game_factory(start=True, deadline_at="2999-01-01")
    scheduler.close_expired_games()
    assert db.query_one("SELECT status FROM game WHERE id = ?", (gid,))["status"] == "running"


def test_game_without_deadline_is_left_alone(game_factory):
    gid = game_factory(start=True)
    scheduler.close_expired_games()
    assert db.query_one("SELECT status FROM game WHERE id = ?", (gid,))["status"] == "running"


def test_cleanup_keeps_fresh_and_drops_old():
    scheduler.log_error("/fresh", "свежая")
    db.execute("INSERT INTO error_log (path, message, created_at)"
               " VALUES ('/ancient', 'старая', '2020-01-01T00:00:00+00:00')")
    scheduler.cleanup()
    paths = [r["path"] for r in db.query("SELECT path FROM error_log")]
    assert paths == ["/fresh"]


def test_cleanup_removes_expired_codes(people):
    db.execute("INSERT INTO device_code (user_id, source_session_id, code_hash, expires_at)"
               " VALUES (1, 's', 'h', '2020-01-01T00:00:00+00:00')")
    db.execute("INSERT INTO reset_code (user_id, code_hash, expires_at)"
               " VALUES (1, 'h', '2020-01-01T00:00:00+00:00')")
    scheduler.cleanup()
    assert db.query_one("SELECT COUNT(*) AS n FROM device_code")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM reset_code")["n"] == 0


def test_chain_check_reports_broken_circle(game_factory):
    gid = game_factory(start=True)
    assert scheduler.check_chains() == []

    # ломаем круг намеренно: цель ведёт в пустоту
    victim = next(iter(chain_of(gid)))
    db.execute("UPDATE participant SET target_id = NULL WHERE id = ?", (victim,))
    broken = scheduler.check_chains()
    assert broken and "Игра" in broken[0]
    assert db.query_one("SELECT COUNT(*) AS n FROM error_log")["n"] >= 1


def test_daily_backup_writes_once_a_day(tmp_path, monkeypatch, people):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "b")
    first = scheduler.daily_backup()
    assert first and (tmp_path / "b" / first).exists()
    assert scheduler.daily_backup() is None, "вторая копия за сутки не нужна"


def test_backup_rotation_keeps_last_n(tmp_path, monkeypatch, people):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "b")
    monkeypatch.setattr(scheduler, "BACKUP_KEEP", 3)
    (tmp_path / "b").mkdir(parents=True)
    for day in range(1, 8):
        (tmp_path / "b" / f"killer-auto-2026-01-0{day}.db").write_text("x")
    scheduler.daily_backup()
    left = sorted(p.name for p in (tmp_path / "b").glob("killer-auto-*.db"))
    assert len(left) == 3, f"ротация не сработала: {left}"


def test_scheduler_survives_broken_game(game_factory):
    """Сбой в одной игре не должен ронять фоновую задачу целиком."""
    gid = game_factory(start=True, deadline_at="2000-01-01")
    game_factory(title="Здоровая", players=("anna", "maks", "leila"),
                 start=True, deadline_at="2000-01-01")
    db.execute("UPDATE participant SET target_id = NULL WHERE game_id = ?", (gid,))

    finished = scheduler.close_expired_games()   # не должно выбросить исключение
    assert "Здоровая" in finished, "сломанная игра помешала обработать остальные"
