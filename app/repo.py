"""Запросы чтения: всё, что показывается на экранах.

Ключевое правило приватности: ни одна функция здесь не отдаёт цепочку
целиком. Имя цели возвращает только target_for_participant, и только своему
владельцу.
"""
from __future__ import annotations

import sqlite3

from .db import query, query_one


def user_by_login(login: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM user WHERE login = ?", (login,))


def user_by_name(normalized: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM user WHERE name_normalized = ?", (normalized,))


def display_name(user: sqlite3.Row) -> str:
    parts = [user["last_name"], user["first_name"]]
    if user["middle_name"]:
        parts.append(user["middle_name"])
    name = " ".join(p for p in parts if p)
    return f"{name} ({user['qualifier']})" if user["qualifier"] else name


def my_participation(game_id: int, user_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM participant WHERE game_id = ? AND user_id = ?",
                     (game_id, user_id))


def target_for_participant(participant_id: int) -> str | None:
    """Единственное место во всём сервисе, где наружу уходит имя цели."""
    row = query_one(
        "SELECT t.display_name_snapshot AS name FROM participant p"
        " JOIN participant t ON t.id = p.target_id WHERE p.id = ? AND p.status = 'alive'",
        (participant_id,))
    return row["name"] if row else None


def game_by_id(game_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM game WHERE id = ?", (game_id,))


def game_counters(game_id: int) -> dict:
    rows = query("SELECT status, COUNT(*) AS n FROM participant WHERE game_id = ?"
                 " GROUP BY status", (game_id,))
    counts = {r["status"]: r["n"] for r in rows}
    pending = query_one("SELECT COUNT(*) AS n FROM kill_claim WHERE game_id = ?"
                        " AND status = 'pending'", (game_id,))
    return {
        "invited": counts.get("invited", 0),
        "joined": counts.get("joined", 0),
        "declined": counts.get("declined", 0),
        "alive": counts.get("alive", 0),
        "dead": counts.get("dead", 0),
        "withdrawn": counts.get("withdrawn", 0),
        "pending_claims": pending["n"] if pending else 0,
    }


def feed(game_id: int, limit: int = 50) -> list[dict]:
    """Публичная лента игры: только факт выбывания.

    Кто чей киллер — не раскрывается: каждое «Иван устранил Петра» это
    раскрытое ребро круга, за неделю игроки вычислили бы своего охотника.
    """
    rows = query(
        "SELECT e.type, e.created_at, p.display_name_snapshot AS who"
        " FROM event e LEFT JOIN participant p ON p.id = e.subject_participant_id"
        " WHERE e.game_id = ? AND e.reverted_at IS NULL AND e.type IN"
        " ('game_started','kill_confirmed','participant_withdrawn','game_finished',"
        "  'game_paused','game_resumed')"
        " ORDER BY e.id DESC LIMIT ?", (game_id, limit))
    out = []
    for r in rows:
        if r["type"] == "game_started":
            text = "Игра началась"
        elif r["type"] == "kill_confirmed":
            text = f"{r['who']} выбыл"
        elif r["type"] == "participant_withdrawn":
            text = f"{r['who']} выведен из игры"
        elif r["type"] == "game_paused":
            text = "Игра на паузе"
        elif r["type"] == "game_resumed":
            text = "Игра продолжается"
        else:
            text = f"Победитель: {r['who']}" if r["who"] else "Игра завершена"
        out.append({"text": text, "at": r["created_at"], "type": r["type"]})
    return out


def full_log(game_id: int) -> list[dict]:
    """Полный разбор с парами «кто кого» — только после финала."""
    rows = query(
        "SELECT e.created_at, e.type, a.display_name_snapshot AS killer,"
        " s.display_name_snapshot AS victim FROM event e"
        " LEFT JOIN participant a ON a.id = e.actor_participant_id"
        " LEFT JOIN participant s ON s.id = e.subject_participant_id"
        " WHERE e.game_id = ? AND e.reverted_at IS NULL ORDER BY e.id", (game_id,))
    return [dict(r) for r in rows]


def participants(game_id: int) -> list[sqlite3.Row]:
    return query(
        "SELECT p.*, u.avatar_emoji, u.department FROM participant p"
        " LEFT JOIN user u ON u.id = p.user_id WHERE p.game_id = ?"
        " ORDER BY CASE p.status WHEN 'alive' THEN 0 WHEN 'joined' THEN 1"
        " WHEN 'invited' THEN 2 ELSE 3 END, p.display_name_snapshot", (game_id,))


def my_games(user_id: int) -> list[sqlite3.Row]:
    return query(
        "SELECT g.*, p.status AS my_status, p.id AS my_participant_id, p.place"
        " FROM participant p JOIN game g ON g.id = p.game_id"
        " WHERE p.user_id = ? AND p.status != 'declined'"
        " ORDER BY CASE g.status WHEN 'running' THEN 0 WHEN 'paused' THEN 1"
        " WHEN 'recruiting' THEN 2 WHEN 'draft' THEN 3 ELSE 4 END, g.id DESC",
        (user_id,))


def open_catalog(user_id: int) -> list[sqlite3.Row]:
    """Каталог показывает только то, куда можно вступить в одно нажатие."""
    return query(
        "SELECT g.*, (SELECT COUNT(*) FROM participant p WHERE p.game_id = g.id"
        "  AND p.status = 'joined') AS joined_count,"
        " (SELECT COUNT(*) FROM participant p WHERE p.game_id = g.id AND p.user_id = ?)"
        "  AS mine FROM game g WHERE g.visibility = 'open'"
        " AND g.status IN ('draft','recruiting') ORDER BY g.id DESC", (user_id,))


def platform_feed(limit: int = 30) -> list[dict]:
    """Лента платформы: факт игры виден всем, включая приватные.

    Закрыт вход и содержание, а не существование игры.
    """
    rows = query(
        "SELECT g.id, g.title, g.color, g.visibility, g.status, g.created_at,"
        " g.started_at, g.finished_at, w.display_name_snapshot AS winner"
        " FROM game g LEFT JOIN participant w ON w.id = g.winner_participant_id"
        " WHERE g.status != 'draft' ORDER BY COALESCE(g.finished_at, g.started_at,"
        " g.created_at) DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def pending_claims_for(user_id: int) -> list[sqlite3.Row]:
    """Заявки, ждущие подтверждения именно этим человеком."""
    return query(
        "SELECT c.*, g.title, g.color, k.display_name_snapshot AS killer_name,"
        " v.display_name_snapshot AS victim_name, me.id AS my_participant_id"
        " FROM kill_claim c JOIN game g ON g.id = c.game_id"
        " JOIN participant k ON k.id = c.killer_id"
        " JOIN participant v ON v.id = c.victim_id"
        " JOIN participant me ON me.game_id = c.game_id AND me.user_id = ?"
        " WHERE c.status = 'pending' AND c.created_by != me.id"
        " AND me.id IN (c.killer_id, c.victim_id)", (user_id,))


def notifications(user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return query("SELECT * FROM notification WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                 (user_id, limit))


def user_stats(user_id: int) -> dict:
    row = query_one(
        "SELECT COUNT(*) AS games, COALESCE(SUM(p.kills_count), 0) AS kills,"
        " COALESCE(SUM(CASE WHEN g.winner_participant_id = p.id THEN 1 ELSE 0 END), 0) AS wins"
        " FROM participant p JOIN game g ON g.id = p.game_id"
        " WHERE p.user_id = ? AND p.status IN ('alive','dead','withdrawn')", (user_id,))
    return dict(row) if row else {"games": 0, "kills": 0, "wins": 0}


def hall_of_fame(limit: int = 20) -> list[dict]:
    rows = query(
        "SELECT u.id, u.avatar_emoji, u.last_name, u.first_name, u.middle_name, u.qualifier,"
        " COUNT(DISTINCT p.game_id) AS games, COALESCE(SUM(p.kills_count), 0) AS kills,"
        " SUM(CASE WHEN g.winner_participant_id = p.id THEN 1 ELSE 0 END) AS wins"
        " FROM user u JOIN participant p ON p.user_id = u.id"
        " JOIN game g ON g.id = p.game_id AND g.status = 'finished'"
        " GROUP BY u.id ORDER BY wins DESC, kills DESC, games DESC LIMIT ?", (limit,))
    return [{**dict(r), "name": display_name(r)} for r in rows]


def achievements(user_id: int) -> list[sqlite3.Row]:
    return query("SELECT * FROM achievement WHERE user_id = ? ORDER BY awarded_at DESC",
                 (user_id,))


def hall_of_fame_year(year: str | None = None, limit: int = 20) -> list[dict]:
    """Зал славы за год или за всё время."""
    sql = ("SELECT u.id, u.avatar_emoji, u.last_name, u.first_name, u.middle_name,"
           " u.qualifier, COUNT(DISTINCT p.game_id) AS games,"
           " COALESCE(SUM(p.kills_count), 0) AS kills,"
           " SUM(CASE WHEN g.winner_participant_id = p.id THEN 1 ELSE 0 END) AS wins"
           " FROM user u JOIN participant p ON p.user_id = u.id"
           " JOIN game g ON g.id = p.game_id AND g.status = 'finished'")
    params: list = []
    if year:
        sql += " AND substr(g.finished_at, 1, 4) = ?"
        params.append(year)
    sql += " GROUP BY u.id ORDER BY wins DESC, kills DESC, games DESC LIMIT ?"
    params.append(limit)
    rows = query(sql, tuple(params))
    return [{**dict(r), "name": display_name(r)} for r in rows]


def game_years() -> list[str]:
    rows = query("SELECT DISTINCT substr(finished_at, 1, 4) AS y FROM game"
                 " WHERE status = 'finished' AND finished_at IS NOT NULL ORDER BY y DESC")
    return [r["y"] for r in rows if r["y"]]


def eliminations_by_day(game_id: int) -> list[dict]:
    """Хронология выбываний по дням — для графика в разборе игры."""
    rows = query(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM event"
        " WHERE game_id = ? AND reverted_at IS NULL AND type IN"
        " ('kill_confirmed','participant_withdrawn') GROUP BY day ORDER BY day", (game_id,))
    peak = max([r["n"] for r in rows], default=1) or 1
    return [{"day": r["day"][5:], "n": r["n"], "pct": round(r["n"] * 100 / peak)} for r in rows]


def game_records(game_id: int) -> dict:
    """Рекорды игры: самое быстрое устранение и самая долгая охота."""
    rows = query(
        "SELECT e.created_at, a.display_name_snapshot AS killer,"
        " s.display_name_snapshot AS victim FROM event e"
        " LEFT JOIN participant a ON a.id = e.actor_participant_id"
        " LEFT JOIN participant s ON s.id = e.subject_participant_id"
        " WHERE e.game_id = ? AND e.type = 'kill_confirmed' AND e.reverted_at IS NULL"
        " ORDER BY e.id", (game_id,))
    game = game_by_id(game_id)
    if not rows or not game or not game["started_at"]:
        return {}
    top = query_one(
        "SELECT p.display_name_snapshot AS name, p.kills_count FROM participant p"
        " WHERE p.game_id = ? ORDER BY p.kills_count DESC LIMIT 1", (game_id,))
    return {
        "first_kill": {"at": rows[0]["created_at"][:16].replace("T", " "),
                       "killer": rows[0]["killer"], "victim": rows[0]["victim"]},
        "last_kill": {"at": rows[-1]["created_at"][:16].replace("T", " "),
                      "killer": rows[-1]["killer"], "victim": rows[-1]["victim"]},
        "top_hunter": dict(top) if top and top["kills_count"] else None,
        "total": len(rows),
    }


def game_csv(game_id: int) -> str:
    """Разбор игры таблицей. BOM обязателен, иначе Excel ломает кириллицу."""
    def cell(value) -> str:
        return '"' + str(value if value is not None else "").replace('"', '""') + '"'

    lines = ["\ufeffкогда,событие,кто,кого"]
    for e in full_log(game_id):
        label = {"game_started": "старт", "kill_confirmed": "устранение",
                 "participant_withdrawn": "вывод", "game_finished": "финал",
                 "game_paused": "пауза", "game_resumed": "продолжение"}.get(e["type"], e["type"])
        lines.append(",".join([cell(e["created_at"][:16].replace("T", " ")), cell(label),
                               cell(e["killer"]), cell(e["victim"])]))
    lines.append("")
    lines.append("\ufeffучастник,статус,устранений,место")
    for p in participants(game_id):
        lines.append(",".join([cell(p["display_name_snapshot"]), cell(p["status"]),
                               cell(p["kills_count"]), cell(p["place"])]))
    return "\n".join(lines)


def user_card(user_id: int) -> dict:
    """Карточка пользователя для админки: профиль, игры, устройства."""
    user = query_one("SELECT * FROM user WHERE id = ?", (user_id,))
    if user is None:
        return {}
    return {
        "user": user,
        "stats": user_stats(user_id),
        "games": query(
            "SELECT g.id, g.title, g.status, p.status AS my_status, p.place, p.kills_count"
            " FROM participant p JOIN game g ON g.id = p.game_id WHERE p.user_id = ?"
            " ORDER BY g.id DESC", (user_id,)),
        "sessions": query("SELECT * FROM session WHERE user_id = ? AND revoked_at IS NULL"
                          " ORDER BY last_seen_at DESC", (user_id,)),
        "achievements": achievements(user_id),
        "requests": query("SELECT * FROM support_request WHERE user_id = ?"
                          " ORDER BY id DESC LIMIT 10", (user_id,)),
    }


def possible_duplicates() -> list[dict]:
    """Похожие ФИО — кандидаты в дубли (опечатка при регистрации)."""
    rows = query("SELECT id, name_normalized, last_name, first_name, middle_name,"
                 " qualifier, created_at FROM user ORDER BY name_normalized")
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if abs(len(a["name_normalized"]) - len(b["name_normalized"])) > 2:
                continue
            if _close(a["name_normalized"], b["name_normalized"]):
                pairs.append({"a": a, "b": b, "a_name": display_name(a),
                              "b_name": display_name(b)})
    return pairs[:50]


def _close(a: str, b: str, limit: int = 2) -> bool:
    """Расстояние Левенштейна не больше limit."""
    if a == b:
        return False
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= limit
