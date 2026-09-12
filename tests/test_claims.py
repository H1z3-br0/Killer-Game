"""Заявки на устранение: подтверждение, отмена, устаревшие экраны, правила."""
from __future__ import annotations

import pytest
from conftest import alive_count, chain_of, csrf

from app import db, game_logic


def pending(game_id: int):
    return db.query_one("SELECT * FROM kill_claim WHERE game_id = ? AND status = 'pending'",
                        (game_id,))


def login_of(participant_id: int) -> str:
    return db.query_one("SELECT u.login FROM participant p JOIN user u ON u.id = p.user_id"
                        " WHERE p.id = ?", (participant_id,))["login"]


def killer_and_victim(game_id: int):
    chain = chain_of(game_id)
    killer = next(iter(chain))
    return killer, chain[killer]


def test_claim_needs_the_other_side(game_factory, people):
    gid = game_factory(start=True)
    killer, victim = killer_and_victim(gid)
    kc, vc = people[login_of(killer)], people[login_of(victim)]

    kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    claim = pending(gid)
    assert claim is not None

    r = kc.post(f"/claims/{claim['id']}/confirm", data={"csrf": csrf(kc)})
    assert "вторая сторона" in r.text
    assert alive_count(gid) == 4, "заявку подтвердил сам автор"

    vc.post(f"/claims/{claim['id']}/confirm", data={"csrf": csrf(vc)})
    assert alive_count(gid) == 3
    assert db.query_one("SELECT status FROM kill_claim WHERE id = ?",
                        (claim["id"],))["status"] == "confirmed"


def test_victim_can_declare_own_elimination(game_factory, people):
    gid = game_factory(start=True)
    killer, victim = killer_and_victim(gid)
    kc, vc = people[login_of(killer)], people[login_of(victim)]

    vc.post(f"/games/{gid}/claims", data={"csrf": csrf(vc), "as_victim": "1"})
    claim = pending(gid)
    assert claim["killer_id"] == killer and claim["victim_id"] == victim

    kc.post(f"/claims/{claim['id']}/confirm", data={"csrf": csrf(kc)})
    assert alive_count(gid) == 3


def test_claim_on_someone_else_is_impossible(game_factory, people):
    """Заявка всегда про свою цель — выбрать жертву нельзя в принципе."""
    gid = game_factory(start=True)
    killer, victim = killer_and_victim(gid)
    kc = people[login_of(killer)]
    kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    assert pending(gid)["victim_id"] == victim


def test_self_defence_is_refused(game_factory, people):
    """Охотиться на своего охотника нельзя: заявка «меня устранили» создаёт
    пару в правильную сторону, а не наоборот."""
    gid = game_factory(start=True)
    killer, victim = killer_and_victim(gid)
    vc = people[login_of(victim)]
    vc.post(f"/games/{gid}/claims", data={"csrf": csrf(vc), "as_victim": "1"})
    claim = pending(gid)
    assert claim["victim_id"] == victim and claim["killer_id"] == killer


def test_author_can_cancel_claim(game_factory, people):
    gid = game_factory(start=True)
    killer, victim = killer_and_victim(gid)
    kc, vc = people[login_of(killer)], people[login_of(victim)]
    kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    claim = pending(gid)

    r = vc.post(f"/claims/{claim['id']}/cancel", data={"csrf": csrf(vc)})
    assert "только автор" in r.text
    kc.post(f"/claims/{claim['id']}/cancel", data={"csrf": csrf(kc)})
    assert pending(gid) is None


def test_claim_dies_with_its_author(game_factory, people):
    """A заявил на B, но A устранил его собственный охотник — заявка гаснет."""
    gid = game_factory(start=True)
    chain = chain_of(gid)
    a = next(iter(chain))
    b = chain[a]
    hunter_of_a = next(h for h, t in chain.items() if t == a)

    ac = people[login_of(a)]
    ac.post(f"/games/{gid}/claims", data={"csrf": csrf(ac)})
    claim = pending(gid)
    assert claim["killer_id"] == a and claim["victim_id"] == b

    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, a, "kill_confirmed", killer_id=hunter_of_a)

    closed = db.query_one("SELECT * FROM kill_claim WHERE id = ?", (claim["id"],))
    assert closed["status"] == "cancelled"
    assert closed["cancel_reason"] == "killer_eliminated"


def test_claim_dies_when_victim_withdrawn(game_factory, people):
    gid = game_factory(start=True)
    killer, victim = killer_and_victim(gid)
    kc = people[login_of(killer)]
    kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    claim = pending(gid)

    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, victim, "participant_withdrawn", reason="уволился")

    closed = db.query_one("SELECT * FROM kill_claim WHERE id = ?", (claim["id"],))
    assert closed["status"] == "cancelled" and closed["cancel_reason"] == "victim_eliminated"


def test_stale_screen_cannot_confirm(game_factory, people):
    """Между подачей и подтверждением цель сменилась — заявка отклоняется."""
    gid = game_factory(start=True)
    killer, victim = killer_and_victim(gid)
    kc, vc = people[login_of(killer)], people[login_of(victim)]
    kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    claim = pending(gid)

    # жертву выводит сисадмин, заявка становится неактуальной
    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, victim, "participant_withdrawn", reason="вывод")

    r = vc.post(f"/claims/{claim['id']}/confirm", data={"csrf": csrf(vc)})
    assert "закрыта" in r.text or "изменилась" in r.text
    assert alive_count(gid) == 3, "устранение засчиталось дважды"


def test_no_claims_while_paused(game_factory, people):
    gid = game_factory(start=True)
    boss = people["boss"]
    boss.post(f"/manage/games/{gid}/pause", data={"csrf": csrf(boss)})

    killer, _ = killer_and_victim(gid)
    kc = people[login_of(killer)]
    r = kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    assert "не идёт" in r.text and pending(gid) is None


def test_quiet_hours_block_claims(game_factory, people):
    """Единственное правило, которое сервис проверяет сам."""
    gid = game_factory(start=True, quiet_from="00:00", quiet_to="23:59")
    killer, _ = killer_and_victim(gid)
    kc = people[login_of(killer)]
    r = kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    assert "запрещена правилами" in r.text and pending(gid) is None


@pytest.mark.parametrize("rules,expected", [
    ({}, False),
    ({"quiet_from": "00:00", "quiet_to": "23:59"}, True),
    ({"quiet_from": "23:58", "quiet_to": "23:59"}, False),
])
def test_quiet_now_logic(rules, expected):
    assert bool(game_logic.quiet_now(rules)) is expected


def test_duplicate_claim_returns_same_one(game_factory, people):
    gid = game_factory(start=True)
    killer, _ = killer_and_victim(gid)
    kc = people[login_of(killer)]
    kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    kc.post(f"/games/{gid}/claims", data={"csrf": csrf(kc)})
    assert db.query_one("SELECT COUNT(*) AS n FROM kill_claim WHERE game_id = ?"
                        " AND status = 'pending'", (gid,))["n"] == 1


def test_dead_player_cannot_claim(game_factory, people):
    gid = game_factory(start=True)
    killer, victim = killer_and_victim(gid)
    with db.transaction() as conn:
        game_logic.eliminate(conn, gid, victim, "kill_confirmed", killer_id=killer)

    vc = people[login_of(victim)]
    r = vc.post(f"/games/{gid}/claims", data={"csrf": csrf(vc)})
    assert "не в игре" in r.text or "вы не в игре" in r.text.lower()
