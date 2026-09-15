"""Ядро игры: построение круга, применение событий, проверка инварианта.

Главное правило всего сервиса: живые участники всегда образуют РОВНО ОДИН
замкнутый цикл. Любая операция, меняющая цепочку, обязана оставить её в этом
состоянии, иначе транзакция откатывается.
"""
from __future__ import annotations

import json
import secrets
import sqlite3

from . import config
from .db import now

# ─────────────────────────── вспомогательное ───────────────────────────


def chain_version(conn: sqlite3.Connection, game_id: int) -> int:
    """Версия цепочки — id последнего события игры.

    Заявка несёт её с собой, чтобы сервер отличил актуальное нажатие от
    сделанного на устаревшем экране.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS v FROM event WHERE game_id = ? AND reverted_at IS NULL",
        (game_id,)).fetchone()
    return row["v"]


def alive(conn: sqlite3.Connection, game_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM participant WHERE game_id = ? AND status = 'alive' ORDER BY id",
        (game_id,)).fetchall()


def hunter_of(conn: sqlite3.Connection, participant_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM participant WHERE target_id = ? AND status = 'alive'",
        (participant_id,)).fetchone()


def add_event(conn: sqlite3.Connection, game_id: int, type_: str,
              actor: int | None = None, subject: int | None = None,
              payload: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO event (game_id, type, actor_participant_id, subject_participant_id,"
        " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (game_id, type_, actor, subject,
         json.dumps(payload or {}, ensure_ascii=False), now()))
    return cur.lastrowid


def notify(conn: sqlite3.Connection, user_id: int | None, type_: str,
           text: str, game_id: int | None = None) -> None:
    if not user_id:
        return
    conn.execute(
        "INSERT INTO notification (user_id, type, game_id, text, created_at)"
        " VALUES (?, ?, ?, ?, ?)", (user_id, type_, game_id, text, now()))


def award(conn: sqlite3.Connection, user_id: int | None, code: str,
          game_id: int | None) -> None:
    """Достижения идемпотентны: повторная выдача молча игнорируется."""
    if not user_id:
        return
    conn.execute(
        "INSERT OR IGNORE INTO achievement (user_id, code, game_id, awarded_at)"
        " VALUES (?, ?, ?, ?)", (user_id, code, game_id, now()))


# ─────────────────────────── инвариант ───────────────────────────


class ChainBroken(Exception):
    """Цепочка перестала быть одним замкнутым кругом — транзакцию откатываем."""


def verify_chain(conn: sqlite3.Connection, game_id: int) -> dict:
    """Проходим круг от первого живого и проверяем, что он возвращается в
    начало, задев каждого ровно один раз."""
    living = alive(conn, game_id)
    total = len(living)
    result = {"ok": False, "walked": 0, "loops": 0, "self_targets": 0, "total": total,
              "reason": ""}
    if total == 0:
        result.update(ok=True, loops=0, reason="")
        return result
    if total == 1:
        only = living[0]
        result.update(ok=only["target_id"] is None, walked=1, loops=1,
                      reason="" if only["target_id"] is None else "у победителя осталась цель")
        return result

    by_id = {p["id"]: p for p in living}
    result["self_targets"] = sum(1 for p in living if p["target_id"] == p["id"])

    node = living[0]["id"]
    walked: set[int] = set()
    while node not in walked:
        walked.add(node)
        nxt = by_id.get(node)
        if nxt is None or nxt["target_id"] is None:
            result["reason"] = "цепочка обрывается"
            return result
        node = nxt["target_id"]
        if node not in by_id:
            result["reason"] = "цель вне круга живых"
            return result

    result["walked"] = len(walked)
    closes = node == living[0]["id"]
    complete = len(walked) == total
    targets = [p["target_id"] for p in living]
    unique = len(set(targets)) == total
    result["loops"] = 1 if complete else 2
    result["ok"] = closes and complete and unique and result["self_targets"] == 0
    if not result["ok"]:
        result["reason"] = ("круг распался на несколько" if not complete
                            else "цепочка не замкнулась")
    return result


def assert_chain(conn: sqlite3.Connection, game_id: int) -> None:
    res = verify_chain(conn, game_id)
    if not res["ok"]:
        raise ChainBroken(res["reason"] or "цепочка нарушена")


# ─────────────────────────── старт игры ───────────────────────────


def start_game(conn: sqlite3.Connection, game_id: int, actor_user_id: int) -> dict:
    """Фиксируем состав, строим кольцо, раздаём цели.

    В круг входят только принявшие приглашение: молчание к моменту старта
    считается отказом, иначе игра встанет на человеке, который не открывал
    сервис.
    """
    game = conn.execute("SELECT * FROM game WHERE id = ?", (game_id,)).fetchone()
    if game is None:
        raise ValueError("игра не найдена")
    if game["status"] not in ("draft", "recruiting"):
        raise ValueError("игра уже стартовала")

    joined = conn.execute(
        "SELECT * FROM participant WHERE game_id = ? AND status = 'joined' ORDER BY id",
        (game_id,)).fetchall()
    if len(joined) < config.MIN_PLAYERS:
        raise ValueError(f"нужно минимум {config.MIN_PLAYERS} участника, принявших приглашение")

    # Молчащих переводим в отказ — они не попадут в круг.
    conn.execute(
        "UPDATE participant SET status = 'declined' WHERE game_id = ? AND status = 'invited'",
        (game_id,))

    order = list(joined)
    # Перемешивание Фишера–Йейтса на криптостойком источнике.
    for i in range(len(order) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        order[i], order[j] = order[j], order[i]

    for i, p in enumerate(order):
        target = order[(i + 1) % len(order)]
        conn.execute(
            "UPDATE participant SET status = 'alive', target_id = ? WHERE id = ?",
            (target["id"], p["id"]))

    event_id = add_event(conn, game_id, "game_started", payload={"players": len(order)})
    for p in order:
        cur = conn.execute("SELECT target_id FROM participant WHERE id = ?", (p["id"],)).fetchone()
        conn.execute(
            "INSERT INTO edge_snapshot (game_id, hunter_id, target_id, valid_from_event_id)"
            " VALUES (?, ?, ?, ?)", (game_id, p["id"], cur["target_id"], event_id))
        notify(conn, p["user_id"], "game_started",
               f"Игра «{game['title']}» началась — вам назначена цель.", game_id)

    conn.execute("UPDATE game SET status = 'running', started_at = ? WHERE id = ?",
                 (now(), game_id))
    assert_chain(conn, game_id)
    return {"players": len(order), "event_id": event_id}


# ─────────────────────────── выбытие ───────────────────────────


def cancel_claims_of(conn: sqlite3.Connection, participant_id: int) -> None:
    """Гасим все незакрытые заявки, где участник — киллер или жертва.

    Ради случая: A заявил на B и ждёт подтверждения, а собственный охотник
    устраняет самого A. Не погасив заявку, мы дали бы B подтвердить устранение
    от выбывшего и пересшили бы круг дважды.

    Причину пишем по роли выбывшего: вторая сторона должна прочитать, что
    произошло на самом деле, а не гадать.
    """
    stamp = now()
    conn.execute(
        "UPDATE kill_claim SET status = 'cancelled', cancel_reason = 'killer_eliminated',"
        " resolved_at = ? WHERE status = 'pending' AND killer_id = ?",
        (stamp, participant_id))
    conn.execute(
        "UPDATE kill_claim SET status = 'cancelled', cancel_reason = 'victim_eliminated',"
        " resolved_at = ? WHERE status = 'pending' AND victim_id = ?",
        (stamp, participant_id))


def eliminate(conn: sqlite3.Connection, game_id: int, victim_id: int,
              event_type: str, killer_id: int | None = None,
              actor_user_id: int | None = None, reason: str = "") -> dict:
    """Единая операция выбытия: и устранение, и вывод админом.

    Круг пересшивается одинаково: охотник выбывшего наследует его цель.
    """
    game = conn.execute("SELECT * FROM game WHERE id = ?", (game_id,)).fetchone()
    if game["status"] not in ("running", "paused"):
        raise ValueError("игра не идёт")

    victim = conn.execute("SELECT * FROM participant WHERE id = ?", (victim_id,)).fetchone()
    if victim is None or victim["status"] != "alive":
        raise ValueError("участник уже вне игры")

    hunter = hunter_of(conn, victim_id)
    living_before = len(alive(conn, game_id))

    event_id = add_event(conn, game_id, event_type, actor=killer_id, subject=victim_id,
                         payload={"reason": reason})

    # Пересшивание: охотник жертвы получает её цель.
    if hunter is not None and hunter["id"] != victim_id:
        conn.execute("UPDATE edge_snapshot SET valid_to_event_id = ? WHERE game_id = ?"
                     " AND hunter_id = ? AND valid_to_event_id IS NULL",
                     (event_id, game_id, hunter["id"]))
        new_target = victim["target_id"]
        if new_target == hunter["id"]:
            new_target = None  # остался один — цели больше нет
        conn.execute("UPDATE participant SET target_id = ? WHERE id = ?",
                     (new_target, hunter["id"]))
        if new_target is not None:
            conn.execute(
                "INSERT INTO edge_snapshot (game_id, hunter_id, target_id, valid_from_event_id)"
                " VALUES (?, ?, ?, ?)", (game_id, hunter["id"], new_target, event_id))
            # У новой цели сменился охотник: считаем, скольких она уже пережила.
            hunters = conn.execute(
                "SELECT COUNT(*) AS n FROM edge_snapshot WHERE game_id = ? AND target_id = ?",
                (game_id, new_target)).fetchone()
            if hunters["n"] >= 4:
                survivor = conn.execute("SELECT user_id FROM participant WHERE id = ?",
                                        (new_target,)).fetchone()
                award(conn, survivor["user_id"], "survivor3", game_id)

    conn.execute("UPDATE edge_snapshot SET valid_to_event_id = ? WHERE game_id = ?"
                 " AND hunter_id = ? AND valid_to_event_id IS NULL",
                 (event_id, game_id, victim_id))
    conn.execute(
        "UPDATE participant SET status = ?, target_id = NULL, died_at = ?, place = ?"
        " WHERE id = ?",
        ("dead" if event_type == "kill_confirmed" else "withdrawn",
         now(), living_before, victim_id))

    cancel_claims_of(conn, victim_id)

    if killer_id:
        conn.execute("UPDATE participant SET kills_count = kills_count + 1 WHERE id = ?",
                     (killer_id,))
        killer = conn.execute("SELECT * FROM participant WHERE id = ?", (killer_id,)).fetchone()
        award(conn, killer["user_id"], "first_blood", game_id)
        if killer["kills_count"] + 1 >= 3:
            award(conn, killer["user_id"], "triple", game_id)
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM event WHERE game_id = ? AND type = 'kill_confirmed'"
            " AND actor_participant_id = ? AND created_at >= ? AND reverted_at IS NULL",
            (game_id, killer_id, now()[:10])).fetchone()
        if today["n"] >= 2:
            award(conn, killer["user_id"], "double_day", game_id)
        new_t = conn.execute("SELECT target_id FROM participant WHERE id = ?",
                             (killer_id,)).fetchone()["target_id"]
        if new_t:
            notify(conn, killer["user_id"], "new_target",
                   f"Цель устранена. В игре «{game['title']}» вам назначена новая.", game_id)
    elif hunter is not None:
        notify(conn, hunter["user_id"], "new_target",
               f"Ваша цель выбыла из игры «{game['title']}». Назначена новая.", game_id)

    notify(conn, victim["user_id"], "eliminated",
           f"Вы выбыли из игры «{game['title']}».", game_id)

    living = alive(conn, game_id)
    if len(living) == 1:
        winner = living[0]
        conn.execute("UPDATE participant SET target_id = NULL, place = 1 WHERE id = ?",
                     (winner["id"],))
        finish_game(conn, game_id, winner["id"])
    elif len(living) == 0:
        conn.execute("UPDATE game SET status = 'void', finished_at = ? WHERE id = ?",
                     (now(), game_id))

    assert_chain(conn, game_id)
    return {"event_id": event_id, "alive": len(alive(conn, game_id))}


def finish_game(conn: sqlite3.Connection, game_id: int, winner_participant_id: int | None) -> None:
    game = conn.execute("SELECT * FROM game WHERE id = ?", (game_id,)).fetchone()
    conn.execute(
        "UPDATE game SET status = 'finished', finished_at = ?, winner_participant_id = ?"
        " WHERE id = ?", (now(), winner_participant_id, game_id))
    add_event(conn, game_id, "game_finished", subject=winner_participant_id)
    if winner_participant_id:
        w = conn.execute("SELECT * FROM participant WHERE id = ?",
                         (winner_participant_id,)).fetchone()
        award(conn, w["user_id"], "winner", game_id)
        # Финалисты: победитель и тот, кто выбыл последним.
        for p in conn.execute(
                "SELECT * FROM participant WHERE game_id = ? AND place IS NOT NULL"
                " AND place <= 2", (game_id,)).fetchall():
            award(conn, p["user_id"], "finalist", game_id)
        for p in conn.execute(
                "SELECT p.user_id, (SELECT COUNT(*) FROM participant q JOIN game g2"
                "  ON g2.id = q.game_id WHERE q.user_id = p.user_id AND g2.status = 'finished'"
                "  AND q.status IN ('alive','dead','withdrawn')) AS played"
                " FROM participant p WHERE p.game_id = ? AND p.user_id IS NOT NULL",
                (game_id,)).fetchall():
            if p["played"] >= 5:
                award(conn, p["user_id"], "veteran", game_id)
        conn.execute("UPDATE participant SET status = 'alive', place = 1 WHERE id = ?",
                     (winner_participant_id,))
    for p in conn.execute("SELECT * FROM participant WHERE game_id = ? AND status IN"
                          " ('alive','dead','withdrawn')", (game_id,)).fetchall():
        notify(conn, p["user_id"], "game_finished",
               f"Игра «{game['title']}» завершена. Итоги открыты.", game_id)


def force_finish(conn: sqlite3.Connection, game_id: int) -> None:
    """Досрочное завершение: побеждает лучший по устранениям, при равенстве —
    устранивший раньше."""
    living = conn.execute(
        "SELECT p.*, (SELECT MAX(e.id) FROM event e WHERE e.actor_participant_id = p.id"
        "  AND e.type = 'kill_confirmed') AS last_kill"
        " FROM participant p WHERE p.game_id = ? AND p.status = 'alive'",
        (game_id,)).fetchall()
    if not living:
        conn.execute("UPDATE game SET status = 'void', finished_at = ? WHERE id = ?",
                     (now(), game_id))
        return
    best = sorted(living, key=lambda p: (-p["kills_count"], p["last_kill"] or 1 << 30))[0]
    for i, p in enumerate(sorted(living, key=lambda p: (-p["kills_count"],
                                                        p["last_kill"] or 1 << 30)), start=1):
        conn.execute("UPDATE participant SET place = ? WHERE id = ?", (i, p["id"]))
    finish_game(conn, game_id, best["id"])


# ─────────────────────────── заявки ───────────────────────────


def create_claim(conn: sqlite3.Connection, game_id: int, author_participant_id: int,
                 as_victim: bool = False) -> int:
    """Заявку создаёт киллер («устранил цель») или жертва («меня устранили»).

    Проверяем, что цель заявителя прямо сейчас та самая: экран мог устареть.
    """
    game = conn.execute("SELECT * FROM game WHERE id = ?", (game_id,)).fetchone()
    if game["status"] != "running":
        raise ValueError("игра сейчас не идёт")

    author = conn.execute("SELECT * FROM participant WHERE id = ?",
                          (author_participant_id,)).fetchone()
    if author is None or author["status"] != "alive":
        raise ValueError("вы не в игре")

    if as_victim:
        killer = hunter_of(conn, author["id"])
        if killer is None:
            raise ValueError("на вас никто не охотится")
        killer_id, victim_id = killer["id"], author["id"]
    else:
        if not author["target_id"]:
            raise ValueError("у вас нет цели")
        killer_id, victim_id = author["id"], author["target_id"]

    victim = conn.execute("SELECT * FROM participant WHERE id = ?", (victim_id,)).fetchone()
    if victim["status"] != "alive":
        raise ValueError("ваша цель уже не та, обновите страницу")

    existing = conn.execute(
        "SELECT id FROM kill_claim WHERE game_id = ? AND status = 'pending'"
        " AND killer_id = ? AND victim_id = ?", (game_id, killer_id, victim_id)).fetchone()
    if existing:
        return existing["id"]

    cur = conn.execute(
        "INSERT INTO kill_claim (game_id, killer_id, victim_id, chain_version, created_by,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (game_id, killer_id, victim_id, chain_version(conn, game_id),
         author_participant_id, now()))

    other_id = victim_id if not as_victim else killer_id
    other = conn.execute("SELECT * FROM participant WHERE id = ?", (other_id,)).fetchone()
    notify(conn, other["user_id"], "claim",
           f"Заявка на устранение в игре «{game['title']}» ждёт вашего подтверждения.",
           game_id)
    return cur.lastrowid


def confirm_claim(conn: sqlite3.Connection, claim_id: int, confirming_participant_id: int) -> dict:
    """Подтверждение второй стороной. Проверку актуальности цели повторяем:
    между подачей и подтверждением цепочка могла измениться."""
    claim = conn.execute("SELECT * FROM kill_claim WHERE id = ?", (claim_id,)).fetchone()
    if claim is None or claim["status"] != "pending":
        raise ValueError("заявка уже закрыта")
    if claim["created_by"] == confirming_participant_id:
        raise ValueError("подтвердить должна вторая сторона")
    if confirming_participant_id not in (claim["killer_id"], claim["victim_id"]):
        raise ValueError("это не ваша заявка")

    game = conn.execute("SELECT * FROM game WHERE id = ?", (claim["game_id"],)).fetchone()
    if game["status"] != "running":
        raise ValueError("игра сейчас не идёт")

    killer = conn.execute("SELECT * FROM participant WHERE id = ?", (claim["killer_id"],
                          )).fetchone()
    if killer["status"] != "alive" or killer["target_id"] != claim["victim_id"]:
        conn.execute("UPDATE kill_claim SET status = 'cancelled', cancel_reason = ?,"
                     " resolved_at = ? WHERE id = ?", ("target_changed", now(), claim_id))
        raise ValueError("цепочка изменилась, заявка отменена")

    conn.execute("UPDATE kill_claim SET status = 'confirmed', resolved_at = ? WHERE id = ?",
                 (now(), claim_id))
    return eliminate(conn, claim["game_id"], claim["victim_id"], "kill_confirmed",
                     killer_id=claim["killer_id"])


def revert_last_event(conn: sqlite3.Connection, game_id: int) -> str:
    """Откатить можно только последнее событие: воскрешение из середины
    истории сделало бы бессмысленными все последующие пересшивания."""
    ev = conn.execute(
        "SELECT * FROM event WHERE game_id = ? AND reverted_at IS NULL"
        " ORDER BY id DESC LIMIT 1", (game_id,)).fetchone()
    if ev is None:
        raise ValueError("нечего откатывать")
    if ev["type"] not in ("kill_confirmed", "participant_withdrawn"):
        raise ValueError("это событие не откатывается")

    victim_id = ev["subject_participant_id"]

    # Возвращаем жертву в круг на её прежнее место.
    edge = conn.execute(
        "SELECT * FROM edge_snapshot WHERE game_id = ? AND hunter_id = ?"
        " AND valid_to_event_id = ? ORDER BY id DESC LIMIT 1",
        (game_id, victim_id, ev["id"])).fetchone()
    hunter = conn.execute(
        "SELECT * FROM edge_snapshot WHERE game_id = ? AND target_id = ?"
        " AND valid_to_event_id = ? ORDER BY id DESC LIMIT 1",
        (game_id, victim_id, ev["id"])).fetchone()
    if edge is None or hunter is None:
        raise ValueError("нет данных для отката")

    conn.execute("UPDATE participant SET status = 'alive', target_id = ?, died_at = NULL,"
                 " place = NULL WHERE id = ?", (edge["target_id"], victim_id))
    conn.execute("UPDATE participant SET target_id = ? WHERE id = ?",
                 (victim_id, hunter["hunter_id"]))
    conn.execute("DELETE FROM edge_snapshot WHERE game_id = ? AND valid_from_event_id = ?",
                 (game_id, ev["id"]))
    conn.execute("UPDATE edge_snapshot SET valid_to_event_id = NULL WHERE game_id = ?"
                 " AND valid_to_event_id = ?", (game_id, ev["id"]))
    if ev["actor_participant_id"]:
        conn.execute("UPDATE participant SET kills_count = MAX(kills_count - 1, 0)"
                     " WHERE id = ?", (ev["actor_participant_id"],))
    conn.execute("UPDATE event SET reverted_at = ? WHERE id = ?", (now(), ev["id"]))
    conn.execute("UPDATE game SET status = 'running', finished_at = NULL,"
                 " winner_participant_id = NULL WHERE id = ? AND status IN ('finished','void')",
                 (game_id,))
    assert_chain(conn, game_id)
    return f"Откачено: {ev['type']} от {ev['created_at']}"
