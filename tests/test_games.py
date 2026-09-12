"""Жизненный цикл игры: набор, видимость, вместимость, правила вступления."""
from __future__ import annotations

from conftest import csrf, participant_of

from app import db, settings_store


def status_of(game_id: int) -> str:
    return db.query_one("SELECT status FROM game WHERE id = ?", (game_id,))["status"]


def test_anyone_can_create_a_game(people):
    anna = people["anna"]
    anna.get("/manage/games/new")
    anna.post("/manage/games/new", data={"csrf": csrf(anna), "title": "Своя игра",
                                         "visibility": "open", "color": "#D9A441"})
    game = db.query_one("SELECT * FROM game ORDER BY id DESC")
    assert game["title"] == "Своя игра"
    assert game["admin_user_id"] == db.query_one(
        "SELECT id FROM user WHERE login = 'anna'")["id"]


def test_game_creation_can_be_restricted(people):
    settings_store.set_value("allow_anyone_create_game", False)
    anna = people["anna"]
    assert anna.get("/manage/games/new").status_code == 403


def test_invited_player_must_accept(game_factory, people):
    boss = people["boss"]
    boss.get("/manage/games/new")
    boss.post("/manage/games/new", data={"csrf": csrf(boss), "title": "Набор",
                                         "visibility": "open", "color": "#D9A441"})
    gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]
    uid = db.query_one("SELECT id FROM user WHERE login = 'anna'")["id"]
    boss.post(f"/manage/games/{gid}/invite",
              data={"csrf": csrf(boss), "mode": "selected", "user_ids": [str(uid)]})

    assert participant_of(gid, "anna")["status"] == "invited"
    anna = people["anna"]
    assert "Приглашение" in anna.get(f"/games/{gid}").text
    anna.post(f"/games/{gid}/join", data={"csrf": csrf(anna)})
    assert participant_of(gid, "anna")["status"] == "joined"


def test_player_can_decline(game_factory, people):
    gid = game_factory()
    anna = people["anna"]
    anna.post(f"/games/{gid}/decline", data={"csrf": csrf(anna)})
    assert participant_of(gid, "anna")["status"] == "declined"


def test_invite_all_registered(people):
    boss = people["boss"]
    boss.get("/manage/games/new")
    boss.post("/manage/games/new", data={"csrf": csrf(boss), "title": "Все",
                                         "visibility": "open", "color": "#D9A441"})
    gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]
    r = boss.post(f"/manage/games/{gid}/invite", data={"csrf": csrf(boss), "mode": "all"})
    assert "Приглашено: 6" in r.text


def test_capacity_stops_self_signup(people):
    boss = people["boss"]
    boss.get("/manage/games/new")
    boss.post("/manage/games/new", data={"csrf": csrf(boss), "title": "Тесная",
                                         "visibility": "open", "color": "#D9A441",
                                         "capacity": "2"})
    gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]
    for who in ("anna", "maks"):
        c = people[who]
        c.post(f"/games/{gid}/join", data={"csrf": csrf(c)})
    late = people["leila"]
    r = late.post(f"/games/{gid}/join", data={"csrf": csrf(late)})
    assert "Мест больше нет" in r.text
    assert participant_of(gid, "leila") is None


def test_visibility_switches_before_start_only(game_factory, people):
    boss = people["boss"]
    gid = game_factory()
    boss.post(f"/manage/games/{gid}/visibility",
              data={"csrf": csrf(boss), "visibility": "private"})
    def visibility() -> str:
        return db.query_one("SELECT visibility FROM game WHERE id = ?", (gid,))["visibility"]

    assert visibility() == "private"

    boss.post(f"/manage/games/{gid}/start", data={"csrf": csrf(boss)})
    r = boss.post(f"/manage/games/{gid}/visibility",
                  data={"csrf": csrf(boss), "visibility": "open"})
    assert "только до старта" in r.text
    assert visibility() == "private"


def test_no_late_joining(game_factory, people):
    gid = game_factory(players=("anna", "maks", "leila"), start=True)
    dina = people["dina"]
    r = dina.post(f"/games/{gid}/join", data={"csrf": csrf(dina)})
    assert "закрыт" in r.text
    assert participant_of(gid, "dina") is None


def test_one_game_at_a_time_when_restricted(game_factory, people):
    game_factory(title="Игра А", players=("anna",))
    second = game_factory(title="Игра Б", players=())

    settings_store.set_value("allow_multiple_active_games", False)
    anna = people["anna"]
    r = anna.post(f"/games/{second}/join", data={"csrf": csrf(anna)})
    assert "только в одной игре" in r.text
    assert participant_of(second, "anna") is None

    settings_store.set_value("allow_multiple_active_games", True)
    anna.post(f"/games/{second}/join", data={"csrf": csrf(anna)})
    assert participant_of(second, "anna") is not None


def test_mass_invite_skips_busy_players(game_factory, people):
    game_factory(title="Первая", players=("anna",))
    settings_store.set_value("allow_multiple_active_games", False)

    boss = people["boss"]
    boss.get("/manage/games/new")
    boss.post("/manage/games/new", data={"csrf": csrf(boss), "title": "Вторая",
                                         "visibility": "open", "color": "#D9A441"})
    gid = db.query_one("SELECT id FROM game ORDER BY id DESC")["id"]
    r = boss.post(f"/manage/games/{gid}/invite", data={"csrf": csrf(boss), "mode": "all"})
    assert "Пропущено занятых" in r.text
    assert participant_of(gid, "anna") is None


def test_pause_and_resume(game_factory, people):
    boss = people["boss"]
    gid = game_factory(start=True)
    boss.post(f"/manage/games/{gid}/pause", data={"csrf": csrf(boss)})
    assert status_of(gid) == "paused"
    boss.post(f"/manage/games/{gid}/pause", data={"csrf": csrf(boss)})
    assert status_of(gid) == "running"


def test_force_finish_picks_best_hunter(game_factory, people):
    from conftest import chain_of

    from app import game_logic

    boss = people["boss"]
    gid = game_factory(start=True)
    chain = chain_of(gid)
    killer = next(iter(chain))
    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, chain[killer], "kill_confirmed", killer_id=killer)

    boss.post(f"/manage/games/{gid}/finish", data={"csrf": csrf(boss)})
    game = db.query_one("SELECT * FROM game WHERE id = ?", (gid,))
    assert game["status"] == "finished"
    assert game["winner_participant_id"] == killer, "победить должен лучший по устранениям"


def test_deadline_can_be_extended(game_factory, people):
    boss = people["boss"]
    gid = game_factory(start=True)
    boss.post(f"/manage/games/{gid}/deadline",
              data={"csrf": csrf(boss), "deadline_at": "2030-01-01"})
    assert db.query_one("SELECT deadline_at FROM game WHERE id = ?",
                        (gid,))["deadline_at"] == "2030-01-01"


def test_reveal_toggle_opens_private_game(game_factory, people):
    boss = people["boss"]
    gid = game_factory(visibility="private", players=("anna", "maks", "leila"), start=True)
    boss.post(f"/manage/games/{gid}/finish", data={"csrf": csrf(boss)})

    assert "Закрытая игра" in people["dina"].get(f"/games/{gid}").text
    boss.post(f"/manage/games/{gid}/reveal", data={"csrf": csrf(boss)})
    assert "Разбор" in people["dina"].get(f"/games/{gid}").text


def test_only_owner_manages_game(game_factory, people):
    gid = game_factory()
    anna = people["anna"]
    assert "Это не ваша игра" in anna.get(f"/manage/games/{gid}").text


def test_removed_participant_disappears_before_start(game_factory, people):
    boss = people["boss"]
    gid = game_factory()
    pid = participant_of(gid, "anna")["id"]
    boss.post(f"/manage/games/{gid}/remove/{pid}", data={"csrf": csrf(boss)})
    assert participant_of(gid, "anna") is None
