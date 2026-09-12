"""Сквозной сценарий: от регистрации до победителя.

Проверяем не только счастливый путь, но и то, ради чего вся конструкция:
инвариант круга после каждого события и запрет на чужие данные.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["KILLER_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, game_logic  # noqa: E402
from app.main import app  # noqa: E402

# В тесте все клиенты приходят с одного адреса, а боевой лимит — пять
# регистраций в час с адреса. Поднимаем его, чтобы проверять логику игры.
config.RATE_LIMITS["register"] = (500, 3600)
config.RATE_LIMITS["login"] = (500, 300)


TR = {"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i",
      "й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t",
      "у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"",
      "э":"e","ю":"yu","я":"ya"}


def login_for(last_name: str) -> str:
    """В тестах логин выводим из фамилии — людям его придумывает сам человек."""
    return "".join(TR.get(ch, ch) for ch in last_name.lower())


def client() -> TestClient:
    return TestClient(app, follow_redirects=True)


def register(c: TestClient, last: str, first: str, password: str = "secret123") -> None:
    c.get("/register")
    r = c.post("/register", data={"csrf": c.cookies.get("killer_csrf"), "login": login_for(last),
                                  "last_name": last, "first_name": first,
                                  "password": password, "password2": password})
    assert r.status_code == 200, r.status_code


def login(c: TestClient, last: str, first: str = "", password: str = "secret123") -> None:
    c.cookies.clear()
    c.get("/login")
    r = c.post("/login", data={"csrf": c.cookies.get("killer_csrf"), "login": login_for(last),
                               "password": password})
    assert "Выйти" in r.text, "вход не удался"


def csrf(c: TestClient) -> str:
    return c.cookies.get("killer_csrf")


def test_full_game() -> None:
    with TestClient(app) as boot:
        boot.get("/healthz")

    players = [("Соколова", "Анна"), ("Дорн", "Максим"), ("Хайруллина", "Лейла"),
               ("Ивашов", "Пётр"), ("Абрамова", "Дина")]

    admin = client()
    register(admin, "Ведущий", "Игорь")          # первый — сисадмин
    for last, first in players:
        c = client()
        register(c, last, first)

    # ── создание игры ────────────────────────────────────────────────
    admin.get("/manage/games/new")
    r = admin.post("/manage/games/new", data={
        "csrf": csrf(admin), "title": "Осенний отстрел", "visibility": "open",
        "color": "#D9A441", "weapon": "наклейка", "safe_zones": "столовая"})
    assert "Игра создана" in r.text
    game_id = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]

    # ── приглашаем всех, кроме админа ───────────────────────────────
    ids = [r["id"] for r in db.query("SELECT id FROM user WHERE last_name != 'Ведущий'")]
    r = admin.post(f"/manage/games/{game_id}/invite",
                   data={"csrf": csrf(admin), "mode": "selected",
                         "user_ids": [str(i) for i in ids]})
    assert "Приглашено: 5" in r.text

    # ── старт без принявших невозможен ──────────────────────────────
    r = admin.post(f"/manage/games/{game_id}/start", data={"csrf": csrf(admin)})
    assert "минимум" in r.text, "игра стартовала без принявших приглашение"

    # ── принимают четверо, пятый молчит ─────────────────────────────
    for last, first in players[:4]:
        c = client()
        login(c, last, first)
        c.post(f"/games/{game_id}/join", data={"csrf": csrf(c)})

    r = admin.post(f"/manage/games/{game_id}/start", data={"csrf": csrf(admin)})
    assert "Игра началась" in r.text, r.text[:400]

    silent = db.query_one(
        "SELECT p.status FROM participant p JOIN user u ON u.id = p.user_id"
        " WHERE p.game_id = ? AND u.last_name = ?", (game_id, players[4][0]))
    assert silent["status"] == "declined", "молчание не засчиталось отказом"

    check = game_logic.verify_chain(db.connect(), game_id)
    assert check["ok"] and check["walked"] == 4, check

    # ── цель приходит только отдельным запросом и только своему ─────
    killer = client()
    login(killer, *players[0])
    page = killer.get(f"/games/{game_id}")
    me = db.query_one(
        "SELECT p.* FROM participant p JOIN user u ON u.id = p.user_id"
        " WHERE p.game_id = ? AND u.last_name = ?", (game_id, players[0][0]))
    victim = db.query_one("SELECT * FROM participant WHERE id = ?", (me["target_id"],))
    assert victim["display_name_snapshot"] not in page.text, "имя цели утекло в HTML"

    r = killer.post(f"/games/{game_id}/target/reveal", data={"csrf": csrf(killer)})
    assert r.json()["target"] == victim["display_name_snapshot"]

    # чужую цель не отдаём
    outsider = client()
    login(outsider, *players[3])
    assert outsider.post(f"/games/{game_id}/target/reveal",
                         data={"csrf": csrf(outsider)}).json()["target"] != \
        victim["display_name_snapshot"] or me["target_id"] == outsider.cookies.get("x")

    # ── заявка и подтверждение ──────────────────────────────────────
    killer.post(f"/games/{game_id}/claims", data={"csrf": csrf(killer)})
    claim = db.query_one("SELECT * FROM kill_claim WHERE game_id = ? AND status = 'pending'",
                         (game_id,))
    assert claim is not None

    # автор подтвердить не может
    r = killer.post(f"/claims/{claim['id']}/confirm", data={"csrf": csrf(killer)})
    assert "вторая сторона" in r.text

    victim_user = db.query_one("SELECT * FROM user WHERE id = ?", (victim["user_id"],))
    vc = client()
    login(vc, victim_user["last_name"], victim_user["first_name"])
    assert "Подтвердите устранение" in vc.get("/").text, "заявка не встретила жертву на главной"
    r = vc.post(f"/claims/{claim['id']}/confirm", data={"csrf": csrf(vc)})
    assert "Подтверждено" in r.text

    check = game_logic.verify_chain(db.connect(), game_id)
    assert check["ok"] and check["walked"] == 3, check
    assert db.query_one("SELECT status FROM participant WHERE id = ?",
                        (victim["id"],))["status"] == "dead"

    # ── сисадмин выводит участника, круг пересшивается ──────────────
    alive_rows = db.query("SELECT * FROM participant WHERE game_id = ? AND status = 'alive'",
                          (game_id,))
    r = admin.post(f"/admin/games/{game_id}/withdraw",
                   data={"csrf": csrf(admin), "participant_id": str(alive_rows[0]["id"]),
                         "reason": "уволился"})
    assert "выведен" in r.text
    check = game_logic.verify_chain(db.connect(), game_id)
    assert check["ok"] and check["walked"] == 2, check

    # ── финал ───────────────────────────────────────────────────────
    last_two = db.query("SELECT * FROM participant WHERE game_id = ? AND status = 'alive'",
                        (game_id,))
    hunter = last_two[0]
    hunter_user = db.query_one("SELECT * FROM user WHERE id = ?", (hunter["user_id"],))
    hc = client()
    login(hc, hunter_user["last_name"], hunter_user["first_name"])
    hc.post(f"/games/{game_id}/claims", data={"csrf": csrf(hc)})
    final_claim = db.query_one("SELECT * FROM kill_claim WHERE game_id = ?"
                               " AND status = 'pending'", (game_id,))
    fv = db.query_one("SELECT u.* FROM participant p JOIN user u ON u.id = p.user_id"
                      " WHERE p.id = ?", (final_claim["victim_id"],))
    fc = client()
    login(fc, fv["last_name"], fv["first_name"])
    fc.post(f"/claims/{final_claim['id']}/confirm", data={"csrf": csrf(fc)})

    game = db.query_one("SELECT * FROM game WHERE id = ?", (game_id,))
    assert game["status"] == "finished", game["status"]
    assert game["winner_participant_id"] == hunter["id"]

    # разбор открыт после финала
    assert "Разбор" in hc.get(f"/games/{game_id}").text

    print("✓ сквозной сценарий пройден: круг цел на каждом шаге, победитель определён")


def test_private_game_is_closed() -> None:
    admin = client()
    login(admin, "Ведущий", "Игорь")
    admin.get("/manage/games/new")
    admin.post("/manage/games/new", data={"csrf": csrf(admin), "title": "Тихий отдел",
                                          "visibility": "private", "color": "#5FA88B"})
    gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]

    stranger = client()
    login(stranger, "Абрамова", "Дина")
    page = stranger.get(f"/games/{gid}")
    assert "Закрытая игра" in page.text
    assert "Ход игры" not in page.text, "посторонний видит ленту закрытой игры"
    assert "targetBox" not in page.text
    # в каталог закрытая игра не попадает
    assert "Тихий отдел" not in stranger.get("/catalog").text
    # но факт игры виден в ленте платформы
    r = stranger.post(f"/games/{gid}/join", data={"csrf": csrf(stranger)})
    assert "только по приглашению" in r.text
    print("✓ закрытая игра: вход только по приглашению, содержание скрыто")


def test_csrf_required() -> None:
    c = client()
    login(c, "Дорн", "Максим")
    r = c.post("/profile", data={"avatar_emoji": "😀"})
    assert r.status_code == 403, "форма без CSRF-токена прошла"
    print("✓ запрос без CSRF-токена отклонён")


if __name__ == "__main__":
    test_full_game()
    test_private_game_is_closed()
    test_csrf_required()
    print("\nвсе проверки пройдены")
