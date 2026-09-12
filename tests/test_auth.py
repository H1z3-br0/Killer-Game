"""Вход, регистрация, сессии, восстановление доступа."""
from __future__ import annotations

import pytest
from conftest import PASSWORD, csrf, register, sign_in

from app import config, db


def test_first_user_becomes_sysadmin(make_client):
    register(make_client(), "boss", "Шефов", "Борис")
    register(make_client(), "anna", "Соколова", "Анна")
    roles = {r["login"]: r["role"] for r in db.query("SELECT login, role FROM user")}
    assert roles == {"boss": "sysadmin", "anna": "user"}


def test_login_is_case_insensitive(make_client):
    register(make_client(), "maks", "Дорн", "Максим")
    c = sign_in(make_client(), "MAKS")
    assert "Выйти" in c.get("/").text


@pytest.mark.parametrize("login", ["ab", "с_кириллицей", "с пробелом", "a" * 21, "-старт"])
def test_bad_logins_are_refused(make_client, login):
    c = make_client()
    c.get("/register")
    r = c.post("/register", data={"csrf": csrf(c), "login": login, "last_name": "Тестов",
                                  "first_name": "Тест", "password": PASSWORD,
                                  "password2": PASSWORD})
    assert "Логин:" in r.text or "Придумайте логин" in r.text
    assert db.query_one("SELECT COUNT(*) AS n FROM user")["n"] == 0


def test_duplicate_login_refused(make_client):
    register(make_client(), "anna", "Соколова", "Анна")
    c = make_client()
    c.get("/register")
    r = c.post("/register", data={"csrf": csrf(c), "login": "ANNA", "last_name": "Иная",
                                  "first_name": "Анна", "password": PASSWORD,
                                  "password2": PASSWORD})
    assert "занят" in r.text
    assert db.query_one("SELECT COUNT(*) AS n FROM user")["n"] == 1


def test_namesakes_allowed(make_client):
    """С логином полные тёзки перестали конфликтовать."""
    register(make_client(), "ivan1", "Иванов", "Иван")
    register(make_client(), "ivan2", "Иванов", "Иван")
    assert db.query_one("SELECT COUNT(*) AS n FROM user")["n"] == 2


def test_short_password_and_mismatch_refused(make_client):
    c = make_client()
    c.get("/register")
    r = c.post("/register", data={"csrf": csrf(c), "login": "anna", "last_name": "С",
                                  "first_name": "А", "password": "123", "password2": "123"})
    assert "короче" in r.text
    r = c.post("/register", data={"csrf": csrf(c), "login": "anna", "last_name": "С",
                                  "first_name": "А", "password": PASSWORD,
                                  "password2": "другой"})
    assert "не совпадают" in r.text


def test_wrong_password_is_rejected(make_client):
    register(make_client(), "anna", "Соколова", "Анна")
    c = make_client()
    c.get("/login")
    r = c.post("/login", data={"csrf": csrf(c), "login": "anna", "password": "неверный"})
    assert "Не сходится" in r.text and "Выйти" not in r.text


def test_password_is_hashed_not_stored(make_client):
    register(make_client(), "anna", "Соколова", "Анна")
    stored = db.query_one("SELECT password_hash FROM user")["password_hash"]
    assert PASSWORD not in stored and stored.startswith("$argon2")


def test_login_rate_limit(make_client, monkeypatch):
    monkeypatch.setitem(config.RATE_LIMITS, "login", (3, 300))
    register(make_client(), "anna", "Соколова", "Анна")
    c = make_client()
    c.get("/login")
    for _ in range(3):
        c.post("/login", data={"csrf": csrf(c), "login": "anna", "password": "нет"})
    r = c.post("/login", data={"csrf": csrf(c), "login": "anna", "password": PASSWORD})
    assert "Слишком много попыток" in r.text, "перебор пароля не ограничен"


def test_csrf_required_on_every_post(make_client):
    c = register(make_client(), "anna", "Соколова", "Анна")
    for url, data in [("/profile", {"avatar_emoji": "😀"}),
                      ("/logout", {}),
                      ("/manage/games/new", {"title": "Без токена"})]:
        assert c.post(url, data=data).status_code == 403, f"{url} принял запрос без токена"


def test_logout_kills_session(make_client):
    c = register(make_client(), "anna", "Соколова", "Анна")
    c.post("/logout", data={"csrf": csrf(c)})
    assert "Войти" in c.get("/").text


def test_device_code_logs_in_second_device(make_client):
    import re

    first = register(make_client(), "anna", "Соколова", "Анна")
    page = first.post("/devices/code", data={"csrf": csrf(first)}).text
    assert "Код действует" in page
    raw = re.search(r">(\d{6})<", page).group(1)

    second = make_client()
    second.get("/login/device")
    r = second.post("/login/device", data={"csrf": csrf(second), "login": "anna",
                                           "code": raw})
    assert "Выйти" in r.text, "вход по коду с другого устройства не сработал"


def test_device_code_burns_after_three_tries(make_client):
    first = register(make_client(), "anna", "Соколова", "Анна")
    first.post("/devices/code", data={"csrf": csrf(first)})
    second = make_client()
    second.get("/login/device")
    for _ in range(3):
        second.post("/login/device", data={"csrf": csrf(second), "login": "anna",
                                           "code": "000000"})
    r = second.post("/login/device", data={"csrf": csrf(second), "login": "anna",
                                           "code": "000000"})
    assert "код сгорел" in r.text


def test_sysadmin_reset_code_lets_user_set_new_password(make_client):
    boss = register(make_client(), "boss", "Шефов", "Борис")
    register(make_client(), "anna", "Соколова", "Анна")
    uid = db.query_one("SELECT id FROM user WHERE login = 'anna'")["id"]
    page = boss.post(f"/admin/users/{uid}/reset", data={"csrf": csrf(boss)}).text
    code = page.split('letter-spacing:.16em;margin:14px 0">')[1].split("<")[0].strip()

    c = make_client()
    c.get("/login/reset")
    r = c.post("/login/reset", data={"csrf": csrf(c), "login": "anna", "code": code,
                                     "password": "новыйпароль", "password2": "новыйпароль"})
    assert "Выйти" in r.text
    assert "Выйти" in sign_in(make_client(), "anna", "новыйпароль").get("/").text


def test_blocked_user_cannot_log_in(make_client):
    boss = register(make_client(), "boss", "Шефов", "Борис")
    register(make_client(), "anna", "Соколова", "Анна")
    uid = db.query_one("SELECT id FROM user WHERE login = 'anna'")["id"]
    boss.post(f"/admin/users/{uid}/block", data={"csrf": csrf(boss)})

    c = make_client()
    c.get("/login")
    r = c.post("/login", data={"csrf": csrf(c), "login": "anna", "password": PASSWORD})
    assert "заблокирована" in r.text


def test_support_request_gets_code(make_client):
    c = make_client()
    c.get("/support")
    r = c.post("/support", data={"csrf": csrf(c), "type_": "password",
                                 "claimed_name": "Анна Соколова", "message": "забыла"})
    assert "SR-" in r.text
    assert db.query_one("SELECT COUNT(*) AS n FROM support_request")["n"] == 1
