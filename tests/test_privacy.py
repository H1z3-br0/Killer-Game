"""Приватность — главное обещание сервиса: цепочку не видит никто."""
from __future__ import annotations

from conftest import chain_of, csrf, participant_of

from app import db, game_logic


def name_of(participant_id: int) -> str:
    return db.query_one("SELECT display_name_snapshot FROM participant WHERE id = ?",
                        (participant_id,))["display_name_snapshot"]


def login_of(participant_id: int) -> str:
    return db.query_one("SELECT u.login FROM participant p JOIN user u ON u.id = p.user_id"
                        " WHERE p.id = ?", (participant_id,))["login"]


def test_target_name_never_appears_in_page_html(game_factory, people):
    gid = game_factory(start=True)
    for pid, target in chain_of(gid).items():
        c = people[login_of(pid)]
        page = c.get(f"/games/{gid}").text
        assert name_of(target) not in page, "имя цели попало в HTML страницы"


def test_target_arrives_only_by_explicit_request(game_factory, people):
    gid = game_factory(start=True)
    pid, target = next(iter(chain_of(gid).items()))
    c = people[login_of(pid)]
    data = c.post(f"/games/{gid}/target/reveal", data={"csrf": csrf(c)}).json()
    assert data["target"] == name_of(target)


def test_cannot_ask_for_someone_elses_target(game_factory, people):
    """Каждый получает своё имя и только своё."""
    gid = game_factory(start=True)
    chain = chain_of(gid)
    targets = {}
    for pid in chain:
        c = people[login_of(pid)]
        answer = c.post(f"/games/{gid}/target/reveal", data={"csrf": csrf(c)})
        targets[pid] = answer.json()["target"]
    assert all(targets[pid] == name_of(chain[pid]) for pid in chain)


def test_dead_player_gets_no_target(game_factory, people):
    gid = game_factory(start=True)
    chain = chain_of(gid)
    killer = next(iter(chain))
    victim = chain[killer]
    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, victim, "kill_confirmed", killer_id=killer)

    c = people[login_of(victim)]
    r = c.post(f"/games/{gid}/target/reveal", data={"csrf": csrf(c)})
    assert r.status_code == 403


def test_outsider_cannot_get_target(game_factory, people):
    gid = game_factory(players=("anna", "maks", "leila"), start=True)
    dina = people["dina"]
    assert dina.post(f"/games/{gid}/target/reveal", data={"csrf": csrf(dina)}).status_code == 403


def test_feed_hides_who_killed_whom(game_factory, people):
    """По ленте нельзя восстановить рёбра круга."""
    gid = game_factory(start=True)
    chain = chain_of(gid)
    killer = next(iter(chain))
    victim = chain[killer]
    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, victim, "kill_confirmed", killer_id=killer)

    page = people[login_of(list(chain)[2])].get(f"/games/{gid}").text
    assert name_of(victim) in page, "факт выбывания должен быть виден"
    assert f"{name_of(killer)} → {name_of(victim)}" not in page
    assert "устранил" not in page.split("Ход игры")[1]


def test_full_log_opens_only_after_finish(game_factory, people):
    gid = game_factory(start=True)
    c = people["anna"]
    assert "Разбор" not in c.get(f"/games/{gid}").text

    while db.query_one("SELECT COUNT(*) AS n FROM participant WHERE game_id = ?"
                       " AND status = 'alive'", (gid,))["n"] > 1:
        chain = chain_of(gid)
        killer = next(iter(chain))
        with db.transaction() as conn:
            game_logic.eliminate(conn, gid, chain[killer], "kill_confirmed", killer_id=killer)

    assert "Разбор" in c.get(f"/games/{gid}").text


def test_private_game_hides_content_from_outsiders(game_factory, people):
    gid = game_factory(visibility="private", players=("anna", "maks", "leila"), start=True)
    page = people["dina"].get(f"/games/{gid}").text
    assert "Закрытая игра" in page
    assert "Ход игры" not in page and "Состав" not in page


def test_private_game_is_visible_as_a_fact(game_factory, people):
    """Приватность закрывает вход и содержание, но не существование игры."""
    game_factory(title="Тихий отдел", visibility="private",
                 players=("anna", "maks", "leila"), start=True)
    assert "Тихий отдел" in people["dina"].get("/").text


def test_private_game_absent_from_catalog(game_factory, people):
    game_factory(title="Тихий отдел", visibility="private", players=("anna", "maks"))
    assert "Тихий отдел" not in people["dina"].get("/catalog").text


def test_outsider_cannot_join_private_game(game_factory, people):
    gid = game_factory(visibility="private", players=("anna", "maks", "leila"))
    dina = people["dina"]
    r = dina.post(f"/games/{gid}/join", data={"csrf": csrf(dina)})
    assert "только по приглашению" in r.text
    assert participant_of(gid, "dina") is None


def test_playing_admin_sees_only_numbers(game_factory, people):
    """Иначе панель подсказывала бы ведущему-игроку, кто скоро выбывает."""
    boss = people["boss"]
    gid = game_factory(players=("boss", "anna", "maks", "leila"), start=True)
    chain = chain_of(gid)
    killer = next(iter(chain))
    people[login_of(killer)].post(f"/games/{gid}/claims",
                                  data={"csrf": csrf(people[login_of(killer)])})

    panel = boss.get(f"/manage/games/{gid}").text
    assert "вы играете — имена скрыты" in panel
    assert "Не подтвердили устранение" not in panel


def test_non_playing_admin_sees_pending_victims_only(game_factory, people):
    gid = game_factory(start=True)
    chain = chain_of(gid)
    killer = next(iter(chain))
    victim = chain[killer]
    kc = people[login_of(killer)]
    kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})

    panel = people["boss"].get(f"/manage/games/{gid}").text
    assert name_of(victim) in panel, "ведущий должен видеть, от кого ждут ответа"

    # Смотрим ровно на список зависших заявок: ниже идёт форма вывода, где
    # перечислены все живые — это состав, а не звено круга.
    block = panel.split("Не подтвердили устранение")[1].split("</ul>")[0]
    assert name_of(victim) in block
    assert name_of(killer) not in block, "в списке заявок раскрыт охотник"


def test_no_endpoint_returns_the_chain(game_factory, people):
    """Прямая проверка обещания: пар «кто на кого» нет ни в одном ответе."""
    gid = game_factory(start=True)
    chain = chain_of(gid)
    pairs = [(name_of(h), name_of(t)) for h, t in chain.items()]

    for who in ("boss", "anna"):
        for url in [f"/games/{gid}", f"/manage/games/{gid}", f"/admin/games/{gid}",
                    "/", "/catalog", "/people"]:
            page = people[who].get(url).text
            for hunter, target in pairs:
                assert f"{hunter} → {target}" not in page, f"{url} раскрыл ребро круга"


def test_active_games_hidden_in_public_profile(game_factory, people):
    """Через чужой профиль не должен утекать состав идущей закрытой игры."""
    game_factory(title="Тихий отдел", visibility="private",
                 players=("anna", "maks", "leila"), start=True)
    anna_id = db.query_one("SELECT id FROM user WHERE login = 'anna'")["id"]
    page = people["dina"].get(f"/people/{anna_id}").text
    assert "Тихий отдел" not in page
