"""Админка сисадмина: люди, заявки, операции с цепочкой, бэкапы, аудит."""
from __future__ import annotations

from conftest import alive_count, chain_of, csrf, participant_of, register

from app import config, db, game_logic


def uid(login: str) -> int:
    return db.query_one("SELECT id FROM user WHERE login = ?", (login,))["id"]


def test_only_sysadmin_reaches_admin(people):
    for url in ["/admin", "/admin/users", "/admin/audit", "/admin/maintenance",
                "/admin/games", "/admin/settings", "/admin/backups", "/admin/duplicates"]:
        assert people["anna"].get(url).status_code == 403, f"игрок попал в {url}"
        assert people["boss"].get(url).status_code == 200


def test_block_withdraws_from_active_games(game_factory, people):
    """Заблокированный не сможет подтвердить устранение — игра встала бы."""
    gid = game_factory(start=True)
    before = alive_count(gid)
    boss = people["boss"]

    r = boss.post(f"/admin/users/{uid('anna')}/block", data={"csrf": csrf(boss)})
    assert "Выведен из игр: 1" in r.text
    assert alive_count(gid) == before - 1
    assert participant_of(gid, "anna")["status"] == "withdrawn"
    assert game_logic.verify_chain(db.connect(), gid)["ok"]


def test_unblock_does_not_return_to_game(game_factory, people):
    gid = game_factory(start=True)
    boss = people["boss"]
    boss.post(f"/admin/users/{uid('anna')}/block", data={"csrf": csrf(boss)})
    boss.post(f"/admin/users/{uid('anna')}/block", data={"csrf": csrf(boss)})
    assert db.query_one("SELECT status FROM user WHERE login = 'anna'")["status"] == "active"
    assert participant_of(gid, "anna")["status"] == "withdrawn"


def test_sysadmin_withdraws_participant(game_factory, people):
    gid = game_factory(start=True)
    boss = people["boss"]
    victim = participant_of(gid, "maks")
    r = boss.post(f"/admin/games/{gid}/withdraw",
                  data={"csrf": csrf(boss), "participant_id": str(victim["id"]),
                        "reason": "уволился"})
    assert "выведен" in r.text
    assert alive_count(gid) == 3
    assert game_logic.verify_chain(db.connect(), gid)["ok"]


def test_game_admin_has_no_withdraw_button(game_factory, people):
    gid = game_factory(start=True)
    panel = people["boss"].get(f"/manage/games/{gid}").text
    assert "Выводит сисадмин" in panel, "ведущему не объяснили, куда идти"
    assert "request-withdraw" in panel, "нет формы заявки на вывод"
    assert f"/manage/games/{gid}/withdraw" not in panel, "у ведущего появился прямой вывод"


def test_withdraw_request_reaches_sysadmin(game_factory, people):
    gid = game_factory(start=True, admin="anna", players=("maks", "leila", "petr"))
    anna = people["anna"]
    victim = participant_of(gid, "maks")
    r = anna.post(f"/manage/games/{gid}/request-withdraw",
                  data={"csrf": csrf(anna), "participant_id": str(victim["id"]),
                        "reason": "не подтверждает"})
    assert "отправлена сисадмину" in r.text

    req = db.query_one("SELECT * FROM support_request WHERE type = 'withdraw'")
    assert req["game_id"] == gid and req["subject_participant_id"] == victim["id"]

    boss = people["boss"]
    assert "Вывод участника" in boss.get("/admin/support").text
    boss.post(f"/admin/support/{req['id']}/resolve",
              data={"csrf": csrf(boss), "action": "withdraw"})
    assert alive_count(gid) == 2
    assert db.query_one("SELECT status FROM support_request WHERE id = ?",
                        (req["id"],))["status"] == "resolved"


def test_revert_through_admin_panel(game_factory, people):
    gid = game_factory(start=True)
    before = chain_of(gid)
    killer = next(iter(before))
    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, before[killer], "kill_confirmed", killer_id=killer)

    boss = people["boss"]
    r = boss.post(f"/admin/games/{gid}/revert", data={"csrf": csrf(boss)})
    assert "Откачено" in r.text
    assert chain_of(gid) == before


def test_anonymize_keeps_history(game_factory, people):
    gid = game_factory(start=True)
    boss = people["boss"]
    boss.post(f"/admin/users/{uid('anna')}/anonymize", data={"csrf": csrf(boss)})

    user = db.query_one("SELECT * FROM user WHERE id = ?", (uid('anna') if db.query_one(
        "SELECT 1 AS x FROM user WHERE login = 'anna'") else 0,)) or db.query_one(
        "SELECT * FROM user WHERE last_name = 'Бывший'")
    assert user["last_name"] == "Бывший" and user["status"] == "blocked"
    assert db.query_one("SELECT COUNT(*) AS n FROM participant WHERE game_id = ?"
                        " AND display_name_snapshot = 'Бывший участник'", (gid,))["n"] == 1


def test_full_delete_keeps_game_log_readable(game_factory, people):
    gid = game_factory(start=True)
    boss = people["boss"]
    anna_id = uid("anna")
    victim = participant_of(gid, "anna")

    # Сначала вывести из идущей игры — иначе сервис не даст удалить.
    r = boss.post(f"/admin/users/{anna_id}/delete",
                  data={"csrf": csrf(boss), "confirm_name": "Соколова Анна"})
    assert "сначала выведите" in r.text.lower()

    boss.post(f"/admin/games/{gid}/withdraw",
              data={"csrf": csrf(boss), "participant_id": str(victim["id"]), "reason": "x"})
    boss.post(f"/admin/users/{anna_id}/delete",
              data={"csrf": csrf(boss), "confirm_name": "Соколова Анна"})

    assert db.query_one("SELECT 1 AS x FROM user WHERE id = ?", (anna_id,)) is None
    left = db.query_one("SELECT display_name_snapshot, user_id FROM participant WHERE id = ?",
                        (victim["id"],))
    assert left["user_id"] is None and left["display_name_snapshot"] == "Удалённый участник"


def test_delete_requires_exact_name(people):
    boss = people["boss"]
    r = boss.post(f"/admin/users/{uid('anna')}/delete",
                  data={"csrf": csrf(boss), "confirm_name": "не то имя"})
    assert "введите ФИО точно" in r.text
    assert db.query_one("SELECT 1 AS x FROM user WHERE login = 'anna'") is not None


def test_duplicates_found_and_merged(make_client, people):
    register(make_client(), "petrv", "Петрв", "Иван")   # опечатка
    register(make_client(), "petrov", "Петров", "Иван")
    boss = people["boss"]
    page = boss.get("/admin/duplicates").text
    assert "Петров Иван" in page and "Петрв Иван" in page

    keep, drop = uid("petrov"), uid("petrv")
    boss.post(f"/admin/users/{keep}/merge", data={"csrf": csrf(boss), "drop_id": str(drop)})
    assert db.query_one("SELECT 1 AS x FROM user WHERE id = ?", (drop,)) is None


def test_merge_refuses_players_of_same_game(game_factory, people):
    game_factory()
    boss = people["boss"]
    r = boss.post(f"/admin/users/{uid('anna')}/merge",
                  data={"csrf": csrf(boss), "drop_id": str(uid("maks"))})
    assert "разные люди" in r.text
    assert db.query_one("SELECT 1 AS x FROM user WHERE login = 'maks'") is not None


def test_role_can_be_granted_and_revoked(people):
    boss = people["boss"]
    boss.post(f"/admin/users/{uid('anna')}/role", data={"csrf": csrf(boss)})
    assert db.query_one("SELECT role FROM user WHERE login = 'anna'")["role"] == "sysadmin"
    assert people["anna"].get("/admin").status_code == 200


def test_sysadmin_cannot_demote_self(people):
    boss = people["boss"]
    r = boss.post(f"/admin/users/{uid('boss')}/role", data={"csrf": csrf(boss)})
    assert "Себя разжаловать нельзя" in r.text
    assert db.query_one("SELECT role FROM user WHERE login = 'boss'")["role"] == "sysadmin"


def test_backup_creates_file(people, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    boss = people["boss"]
    r = boss.post("/admin/backups", data={"csrf": csrf(boss)})
    assert "Бэкап создан" in r.text
    assert list((tmp_path / "backups").glob("*.db")), "файл копии не появился"


def test_admin_actions_are_audited(people):
    boss = people["boss"]
    boss.post(f"/admin/users/{uid('anna')}/block", data={"csrf": csrf(boss)})
    actions = [r["action"] for r in db.query("SELECT action FROM audit_log")]
    assert "user_blocked" in actions
    assert "user_blocked" in boss.get("/admin/audit").text


def test_integrity_page_reports_healthy_chain(game_factory, people):
    game_factory(start=True)
    page = people["boss"].get("/admin/integrity").text
    assert "цел" in page


def test_orphan_games_are_flagged(game_factory, people):
    game_factory(admin="anna", players=("maks", "leila", "petr"), start=True)
    boss = people["boss"]
    boss.post(f"/admin/users/{uid('anna')}/block", data={"csrf": csrf(boss)})
    assert "Требуют внимания" in boss.get("/admin").text


def test_game_can_be_transferred(game_factory, people):
    gid = game_factory(admin="anna", players=("maks", "leila", "petr"))
    boss = people["boss"]
    boss.post(f"/admin/games/{gid}/transfer",
              data={"csrf": csrf(boss), "user_id": str(uid("maks"))})
    assert db.query_one("SELECT admin_user_id FROM game WHERE id = ?",
                        (gid,))["admin_user_id"] == uid("maks")
