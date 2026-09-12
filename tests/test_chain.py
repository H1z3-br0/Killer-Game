"""Ядро: круг, инвариант, пересшивание, финал.

Главное правило сервиса — живые всегда образуют ровно один замкнутый цикл.
Здесь оно проверяется и на обычных сценариях, и на случайных.
"""
from __future__ import annotations

import random

import pytest
from conftest import alive_count, chain_of, csrf, hunter_of, participant_of

from app import db, game_logic


def start(game_factory, **kw) -> int:
    return game_factory(start=True, **kw)


def kill(game_id: int, victim_id: int) -> None:
    hunter = hunter_of(game_id, victim_id)
    with db.transaction() as conn:
        game_logic.eliminate(conn, game_id, victim_id, "kill_confirmed",
                             killer_id=hunter["id"])


def invariant(game_id: int) -> dict:
    return game_logic.verify_chain(db.connect(), game_id)


# ─────────────────────────── построение ───────────────────────────


def test_chain_is_single_cycle_after_start(game_factory):
    gid = start(game_factory)
    check = invariant(gid)
    assert check["ok"] and check["loops"] == 1
    assert check["walked"] == check["total"] == 4


def test_nobody_hunts_themselves(game_factory):
    gid = start(game_factory)
    assert all(h != t for h, t in chain_of(gid).items())


def test_every_player_is_hunted_exactly_once(game_factory):
    gid = start(game_factory)
    targets = list(chain_of(gid).values())
    assert len(targets) == len(set(targets))


def test_shuffle_gives_different_orders(game_factory, people):
    """Два запуска подряд не должны давать одинаковый круг."""
    orders = set()
    for i in range(6):
        gid = start(game_factory, title=f"Игра {i}")
        orders.add(tuple(sorted(chain_of(gid).items())))
        db.execute("DELETE FROM game WHERE id = ?", (gid,))
    assert len(orders) > 1, "перемешивание выдаёт один и тот же порядок"


def test_start_requires_minimum_players(game_factory, people):
    gid = game_factory(players=("anna", "maks"))
    boss = people["boss"]
    r = boss.post(f"/manage/games/{gid}/start", data={"csrf": csrf(boss)})
    assert "минимум" in r.text
    assert db.query_one("SELECT status FROM game WHERE id = ?", (gid,))["status"] != "running"


def test_silent_invitee_counts_as_refusal(game_factory, people):
    """Молчание к старту — отказ: иначе игра встанет на том, кто не заходил."""
    boss = people["boss"]
    gid = game_factory(players=("anna", "maks", "leila"))
    dina_id = db.query_one("SELECT id FROM user WHERE login = 'dina'")["id"]
    boss.post(f"/manage/games/{gid}/invite",
              data={"csrf": csrf(boss), "mode": "selected", "user_ids": [str(dina_id)]})
    boss.post(f"/manage/games/{gid}/start", data={"csrf": csrf(boss)})

    assert participant_of(gid, "dina")["status"] == "declined"
    assert invariant(gid)["total"] == 3


# ─────────────────────────── пересшивание ───────────────────────────


def test_killer_inherits_victims_target(game_factory):
    gid = start(game_factory)
    before = chain_of(gid)
    victim = next(iter(before.values()))
    killer = hunter_of(gid, victim)["id"]
    victims_target = before[victim]

    kill(gid, victim)
    assert chain_of(gid)[killer] == victims_target
    assert invariant(gid)["ok"]


def test_invariant_holds_through_whole_game(game_factory):
    gid = start(game_factory)
    while alive_count(gid) > 1:
        victim = next(iter(chain_of(gid).values()))
        kill(gid, victim)
        assert invariant(gid)["ok"], "круг развалился по ходу игры"
    assert db.query_one("SELECT status FROM game WHERE id = ?", (gid,))["status"] == "finished"


def test_two_players_hunt_each_other(game_factory):
    """Финальная пара — штатное состояние, а не ошибка."""
    gid = start(game_factory)
    while alive_count(gid) > 2:
        kill(gid, next(iter(chain_of(gid).values())))
    pair = chain_of(gid)
    a, b = list(pair)
    assert pair[a] == b and pair[b] == a
    assert invariant(gid)["ok"]


def test_last_survivor_wins_and_has_no_target(game_factory):
    gid = start(game_factory)
    while alive_count(gid) > 1:
        kill(gid, next(iter(chain_of(gid).values())))
    game = db.query_one("SELECT * FROM game WHERE id = ?", (gid,))
    winner = db.query_one("SELECT * FROM participant WHERE id = ?",
                          (game["winner_participant_id"],))
    assert game["status"] == "finished"
    assert winner["target_id"] is None and winner["place"] == 1


def test_places_are_assigned_in_order_of_elimination(game_factory):
    gid = start(game_factory)
    order = []
    while alive_count(gid) > 1:
        victim = next(iter(chain_of(gid).values()))
        order.append(victim)
        kill(gid, victim)
    places = [db.query_one("SELECT place FROM participant WHERE id = ?", (p,))["place"]
              for p in order]
    assert places == [4, 3, 2], "места должны идти от последнего к первому"


def test_withdraw_reshapes_chain_like_a_kill(game_factory):
    gid = start(game_factory)
    before = chain_of(gid)
    victim = next(iter(before))
    killer = hunter_of(gid, victim)["id"]

    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, victim, "participant_withdrawn", reason="уволился")

    assert chain_of(gid)[killer] == before[victim]
    assert participant_of(gid, "anna") is not None
    assert invariant(gid)["ok"]


# ─────────────────────────── откат ───────────────────────────


def test_revert_restores_chain_exactly(game_factory):
    gid = start(game_factory)
    before = chain_of(gid)
    victim = next(iter(before.values()))
    killer = hunter_of(gid, victim)["id"]
    kill(gid, victim)

    with db.transaction() as conn:
        game_logic.revert_last_event(conn, gid)

    assert chain_of(gid) == before
    assert invariant(gid)["ok"]
    assert db.query_one("SELECT kills_count FROM participant WHERE id = ?",
                        (killer,))["kills_count"] == 0


def test_revert_only_touches_last_event(game_factory):
    gid = start(game_factory)
    kill(gid, next(iter(chain_of(gid).values())))
    kill(gid, next(iter(chain_of(gid).values())))
    with db.transaction() as conn:
        game_logic.revert_last_event(conn, gid)
    assert alive_count(gid) == 3, "откатилось больше одного события"
    assert invariant(gid)["ok"]


def test_revert_refuses_when_nothing_to_undo(game_factory):
    gid = game_factory()
    with pytest.raises(ValueError, match="нечего откатывать"):
        with db.transaction() as conn:
            game_logic.revert_last_event(conn, gid)


# ─────────────────────────── случайные сценарии ───────────────────────────


@pytest.mark.parametrize("seed", range(8))
def test_invariant_survives_random_events(game_factory, people, seed):
    """Самая ценная проверка: случайная смесь устранений и выводов.

    Круг обязан остаться одним циклом после каждого события, каким бы ни был
    порядок — именно здесь ловятся ошибки пересшивания.
    """
    rnd = random.Random(seed)
    gid = game_factory(players=("anna", "maks", "leila", "petr", "dina"), start=True)

    while alive_count(gid) > 1:
        alive = list(chain_of(gid))
        victim = rnd.choice(alive)
        if rnd.random() < 0.35:
            with db.transaction() as conn:
                game_logic.eliminate(conn, gid, victim, "participant_withdrawn",
                                     reason="случайный вывод")
        else:
            kill(gid, victim)
        check = invariant(gid)
        assert check["ok"], f"seed={seed}: {check['reason']}"

    assert db.query_one("SELECT status FROM game WHERE id = ?", (gid,))["status"] \
        in ("finished", "void")


def test_chain_version_moves_with_every_event(game_factory):
    gid = start(game_factory)
    conn = db.connect()
    first = game_logic.chain_version(conn, gid)
    kill(gid, next(iter(chain_of(gid).values())))
    assert game_logic.chain_version(conn, gid) > first
