"""Общие фикстуры: свежая база на каждый тест, клиенты, фабрики.

Каждый тест получает собственный файл базы, поэтому порядок запуска и
параллельность ничего не ломают.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, settings_store
from app.main import app

PASSWORD = "secret123"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Новая база на каждый тест + щедрые лимиты частоты.

    Боевые лимиты (5 регистраций в час с адреса) в тестах бессмысленны:
    все клиенты приходят с одного адреса.
    """
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setitem(config.RATE_LIMITS, "register", (10_000, 3600))
    monkeypatch.setitem(config.RATE_LIMITS, "login", (10_000, 300))
    monkeypatch.setitem(config.RATE_LIMITS, "claim", (10_000, 3600))
    monkeypatch.setitem(config.RATE_LIMITS, "support", (10_000, 3600))
    db.reset_connection()
    settings_store.reset_cache()
    db.migrate()
    yield
    db.reset_connection()
    settings_store.reset_cache()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, follow_redirects=True)


@pytest.fixture
def make_client():
    """Отдельный браузер: у каждого своя сессия и свой CSRF."""
    def _make() -> TestClient:
        return TestClient(app, follow_redirects=True)
    return _make


# ─────────────────────────── помощники ───────────────────────────


def csrf(c: TestClient) -> str:
    if not c.cookies.get(config.CSRF_COOKIE):
        c.get("/login")
    return c.cookies.get(config.CSRF_COOKIE)


def register(c: TestClient, login: str, last: str = "Тестов", first: str = "Тест",
             password: str = PASSWORD, middle: str = "") -> TestClient:
    c.get("/register")
    c.post("/register", data={"csrf": csrf(c), "login": login, "last_name": last,
                              "first_name": first, "middle_name": middle,
                              "password": password, "password2": password})
    return c


def sign_in(c: TestClient, login: str, password: str = PASSWORD) -> TestClient:
    c.cookies.clear()
    c.get("/login")
    c.post("/login", data={"csrf": csrf(c), "login": login, "password": password})
    return c


@pytest.fixture
def people(make_client):
    """Сисадмин (первый зарегистрированный) и пятеро игроков.

    Возвращает словарь логин → клиент с активной сессией.
    """
    names = [("boss", "Шефов", "Борис"), ("anna", "Соколова", "Анна"),
             ("maks", "Дорн", "Максим"), ("leila", "Хайруллина", "Лейла"),
             ("petr", "Ивашов", "Пётр"), ("dina", "Абрамова", "Дина")]
    out = {}
    for login, last, first in names:
        out[login] = register(make_client(), login, last, first)
    return out


@pytest.fixture
def game_factory(people):
    """Создаёт игру и, по желанию, доводит её до старта."""
    def _make(title: str = "Игра", visibility: str = "open", players: tuple | None = None,
              start: bool = False, admin: str = "boss", **fields) -> int:
        boss = people[admin]
        boss.get("/manage/games/new")
        boss.post("/manage/games/new", data={
            "csrf": csrf(boss), "title": title, "visibility": visibility,
            "color": config.GAME_COLORS[0], **fields})
        gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]

        chosen = ("anna", "maks", "leila", "petr") if players is None else players
        if chosen:
            ids = [db.query_one("SELECT id FROM user WHERE login = ?", (p,))["id"]
                   for p in chosen]
            boss.post(f"/manage/games/{gid}/invite", data={
                "csrf": csrf(boss), "mode": "selected", "user_ids": [str(i) for i in ids]})
        for p in chosen:
            c = people[p]
            c.post(f"/games/{gid}/join", data={"csrf": csrf(c)})
        if start:
            boss.post(f"/manage/games/{gid}/start", data={"csrf": csrf(boss)})
        return gid
    return _make


def participant_of(game_id: int, login: str):
    return db.query_one(
        "SELECT p.* FROM participant p JOIN user u ON u.id = p.user_id"
        " WHERE p.game_id = ? AND u.login = ?", (game_id, login))


def chain_of(game_id: int) -> dict[int, int]:
    return {p["id"]: p["target_id"] for p in db.query(
        "SELECT id, target_id FROM participant WHERE game_id = ? AND status = 'alive'",
        (game_id,))}


def alive_count(game_id: int) -> int:
    return db.query_one("SELECT COUNT(*) AS n FROM participant WHERE game_id = ?"
                        " AND status = 'alive'", (game_id,))["n"]


def hunter_of(game_id: int, victim_participant_id: int):
    return db.query_one("SELECT * FROM participant WHERE game_id = ? AND target_id = ?"
                        " AND status = 'alive'", (game_id, victim_participant_id))
