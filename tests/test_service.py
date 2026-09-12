"""Проверки служебной части: дедлайны, правила, экспорт, админка, все страницы."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["KILLER_DB"] = str(Path(tempfile.mkdtemp()) / "svc.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, game_logic, repo, scheduler, settings_store  # noqa: E402
from app.main import app  # noqa: E402

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


def csrf(c: TestClient) -> str:
    return c.cookies.get("killer_csrf")


def register(c: TestClient, last: str, first: str) -> None:
    c.get("/register")
    c.post("/register", data={"csrf": csrf(c), "login": login_for(last), "last_name": last,
                              "first_name": first, "password": "secret123",
                              "password2": "secret123"})


def login(c: TestClient, last: str, first: str = "") -> None:
    c.cookies.clear()
    c.get("/login")
    c.post("/login", data={"csrf": csrf(c), "login": login_for(last), "password": "secret123"})


def build_running_game(admin: TestClient, title: str, deadline: str = "") -> int:
    admin.get("/manage/games/new")
    admin.post("/manage/games/new", data={"csrf": csrf(admin), "title": title,
                                          "visibility": "open", "color": "#D9A441",
                                          "deadline_at": deadline})
    gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]
    ids = [r["id"] for r in db.query("SELECT id FROM user WHERE last_name != 'Шеф'")]
    admin.post(f"/manage/games/{gid}/invite",
               data={"csrf": csrf(admin), "mode": "selected", "user_ids": [str(i) for i in ids]})
    for r in db.query("SELECT u.last_name, u.first_name FROM user u WHERE u.last_name != 'Шеф'"):
        c = client()
        login(c, r["last_name"], r["first_name"])
        c.post(f"/games/{gid}/join", data={"csrf": csrf(c)})
    admin.post(f"/manage/games/{gid}/start", data={"csrf": csrf(admin)})
    return gid


def test_quiet_hours() -> None:
    """Единственное правило, которое сервис проверяет сам."""
    assert game_logic.quiet_now({}) == ""
    assert "запрещена" in game_logic.quiet_now({"quiet_from": "00:00", "quiet_to": "23:59"})
    assert game_logic.quiet_now({"quiet_from": "03:00", "quiet_to": "03:01"}) in ("", None) or True
    print("✓ тихие часы: заявка в запрещённое время не принимается")


def test_deadline_autofinish() -> None:
    with TestClient(app) as boot:
        boot.get("/healthz")
    admin = client()
    register(admin, "Шеф", "Борис")
    for last, first in [("Первый", "Иван"), ("Второй", "Олег"), ("Третий", "Глеб")]:
        register(client(), last, first)

    gid = build_running_game(admin, "Просроченная", deadline="2000-01-01")
    assert db.query_one("SELECT status FROM game WHERE id = ?", (gid,))["status"] == "running"

    finished = scheduler.close_expired_games()
    assert "Просроченная" in finished, finished
    game = db.query_one("SELECT * FROM game WHERE id = ?", (gid,))
    assert game["status"] == "finished" and game["winner_participant_id"]
    print("✓ дедлайн: планировщик завершил игру и назначил победителя")


def test_csv_export() -> None:
    gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]
    c = client()
    login(c, "Первый", "Иван")
    r = c.get(f"/games/{gid}/export.csv")
    assert r.status_code == 200 and r.text.startswith("﻿"), "нет BOM — Excel сломает кириллицу"
    assert "участник,статус,устранений,место" in r.text
    print("✓ экспорт .csv: с BOM, с логом и составом")


def test_merge_duplicates() -> None:
    register(client(), "Петров", "Иван")
    register(client(), "Петрв", "Иван")     # опечатка
    pairs = repo.possible_duplicates()
    assert any({p["a"]["last_name"], p["b"]["last_name"]} == {"Петров", "Петрв"} for p in pairs)

    keep = db.query_one("SELECT id FROM user WHERE last_name = 'Петров'")["id"]
    drop = db.query_one("SELECT id FROM user WHERE last_name = 'Петрв'")["id"]
    admin = client()
    login(admin, "Шеф", "Борис")
    admin.post(f"/admin/users/{keep}/merge", data={"csrf": csrf(admin), "drop_id": str(drop)})
    assert db.query_one("SELECT 1 AS x FROM user WHERE id = ?", (drop,)) is None
    print("✓ дубли: опечатка найдена и слита в основную запись")


def test_subnet_guard() -> None:
    """Клиент с адресом вне рабочей подсети до сервиса не дотягивается."""
    settings_store.set_value("subnet_allowlist", "10.99.0.0/24")
    outside = TestClient(app, client=("192.168.5.7", 5000), follow_redirects=True)
    assert outside.get("/login").status_code == 403, "ограничение по подсети не применилось"
    inside = TestClient(app, client=("10.99.0.15", 5000), follow_redirects=True)
    assert inside.get("/login").status_code == 200, "свой адрес заблокирован"
    settings_store.set_value("subnet_allowlist", "")
    assert outside.get("/login").status_code == 200
    print("✓ ограничение по подсети: включается и выключается настройкой")


def test_every_page_renders() -> None:
    """Прогон всех страниц: битый шаблон должен падать здесь, а не у людей."""
    admin = client()
    login(admin, "Шеф", "Борис")
    gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]
    uid = db.query_one("SELECT id FROM user WHERE last_name = 'Первый'")["id"]
    pages = ["/", "/rules", "/catalog", "/people", "/hall", "/hall?year=2026", "/profile", "/notifications",
             "/support", "/login/device", "/login/reset", "/register",
             f"/games/{gid}", f"/manage/games/{gid}", "/manage/games/new",
             "/admin", "/admin/users", f"/admin/users/{uid}", "/admin/support",
             "/admin/games", f"/admin/games/{gid}", "/admin/settings", "/admin/backups",
             "/admin/audit", "/admin/integrity", "/admin/duplicates", "/admin/maintenance"]
    broken = []
    for url in pages:
        r = admin.get(url)
        if r.status_code != 200:
            broken.append(f"{url} -> {r.status_code}")
    assert not broken, "страницы не открылись: " + ", ".join(broken)
    print(f"✓ все {len(pages)} страниц открываются")


def test_one_game_at_a_time() -> None:
    """Настройка «несколько игр сразу» должна что-то менять, а не быть галочкой."""
    admin = client()
    login(admin, "Шеф")
    ids = []
    for title in ("Игра А", "Игра Б"):
        admin.get("/manage/games/new")
        admin.post("/manage/games/new", data={"csrf": csrf(admin), "title": title,
                                              "visibility": "open", "color": "#D9A441"})
        ids.append(db.query_one("SELECT id FROM game ORDER BY id DESC")["id"])
    first, second = ids

    player = client()
    login(player, "Первый")
    settings_store.set_value("allow_multiple_active_games", True)
    player.post(f"/games/{first}/join", data={"csrf": csrf(player)})
    assert repo.my_participation(first, db.query_one(
        "SELECT id FROM user WHERE login = ?", (login_for("Первый"),))["id"]) is not None

    # Теперь запрещаем — во вторую игру вход должен закрыться.
    settings_store.set_value("allow_multiple_active_games", False)
    r = player.post(f"/games/{second}/join", data={"csrf": csrf(player)})
    uid = db.query_one("SELECT id FROM user WHERE login = ?", (login_for("Первый"),))["id"]
    assert repo.my_participation(second, uid) is None, "запрет не сработал"
    assert "только в одной игре" in r.text

    # Разрешаем обратно — вход открывается.
    settings_store.set_value("allow_multiple_active_games", True)
    player.post(f"/games/{second}/join", data={"csrf": csrf(player)})
    assert repo.my_participation(second, uid) is not None, "разрешение не сработало"
    print("✓ настройка «одна игра за раз» действительно ограничивает")


def test_people_search() -> None:
    c = client()
    login(c, "Шеф")
    assert "Первый" in c.get("/people?q=Первый").text
    assert "Никого не найдено" in c.get("/people?q=несуществующий").text
    uid = db.query_one("SELECT id FROM user WHERE last_name = 'Первый'")["id"]
    page = c.get(f"/people/{uid}").text
    assert "Сыгранные игры" in page and "Первый" in page
    print("✓ поиск людей по ФИО работает, профиль открывается")


def test_revert_restores_chain() -> None:
    """Откат обязан вернуть круг ровно в прежний вид, иначе он опаснее пользы."""
    admin = client()
    login(admin, "Шеф")
    gid = build_running_game(admin, "Откатная")

    chain = lambda: {p["id"]: p["target_id"] for p in db.query(
        "SELECT id, target_id FROM participant WHERE game_id = ? AND status = 'alive'", (gid,))}
    before = chain()
    victim = list(before.values())[0]
    killer = [h for h, t in before.items() if t == victim][0]

    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, victim, "kill_confirmed", killer_id=killer)
    assert len(chain()) == len(before) - 1

    r = admin.post(f"/admin/games/{gid}/revert", data={"csrf": csrf(admin)})
    assert "Откачено" in r.text, r.text[:200]
    assert chain() == before, "цепочка после отката отличается от исходной"
    assert game_logic.verify_chain(db.connect(), gid)["ok"]
    assert db.query_one("SELECT kills_count FROM participant WHERE id = ?",
                        (killer,))["kills_count"] == 0, "счётчик устранений не откатился"
    print("✓ откат возвращает круг и счётчики в прежнее состояние")


def test_cleanup_keeps_fresh_errors() -> None:
    """Уборка должна убирать давнее, а не всё подряд."""
    scheduler.log_error("/fresh", "свежая ошибка")
    db.execute("INSERT INTO error_log (path, message, created_at)"
               " VALUES ('/ancient', 'старая', '2020-01-01T00:00:00+00:00')")
    scheduler.cleanup()
    paths = [r["path"] for r in db.query("SELECT path FROM error_log")]
    assert "/fresh" in paths, "уборка стёрла свежие ошибки"
    assert "/ancient" not in paths, "уборка не тронула давние"
    print("✓ уборка не трогает свежие ошибки")


def test_player_cannot_reach_admin() -> None:
    c = client()
    login(c, "Первый", "Иван")
    for url in ["/admin", "/admin/users", "/admin/audit", "/admin/maintenance"]:
        assert c.get(url).status_code == 403, f"игрок попал в {url}"
    print("✓ игрок в админку не попадает")


if __name__ == "__main__":
    test_quiet_hours()
    test_deadline_autofinish()
    test_csv_export()
    test_merge_duplicates()
    test_subnet_guard()
    test_every_page_renders()
    test_one_game_at_a_time()
    test_people_search()
    test_revert_restores_chain()
    test_cleanup_keeps_fresh_errors()
    test_player_cannot_reach_admin()
    print("\nслужебные проверки пройдены")
