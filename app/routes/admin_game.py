"""Панель админа игры: создание, набор, старт, пауза, завершение.

Операций, меняющих цепочку, здесь нет: вывод участника и откат события
принадлежат сисадмину. Админ игры отправляет ему заявку.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import auth, config, game_logic, repo, security, settings_store
from ..db import audit, execute, now, query, transaction
from ..web import redirect, render

router = APIRouter(prefix="/manage")


def owned_game(request: Request, game_id: int):
    user = auth.require_user(request)
    game = repo.game_by_id(game_id)
    if game is None:
        return user, None
    if game["admin_user_id"] != user["id"] and user["role"] != "sysadmin":
        return user, False
    return user, game


@router.get("/games/new", response_class=HTMLResponse)
def new_game_form(request: Request):
    user = auth.require_user(request)
    if not settings_store.get("allow_anyone_create_game") and user["role"] != "sysadmin":
        return render(request, "error.html", status_code=403, code=403,
                      message="Создавать игры может только сисадмин.")
    return render(request, "game_new.html", colors=config.GAME_COLORS)


@router.post("/games/new")
def new_game(request: Request, csrf: str = Form(""), title: str = Form(""),
             description: str = Form(""), visibility: str = Form("open"),
             color: str = Form(config.GAME_COLORS[0]), capacity: str = Form(""),
             deadline_at: str = Form(""), weapon: str = Form(""),
             safe_zones: str = Form(""), quiet_from: str = Form(""),
             quiet_to: str = Form(""), no_weekends: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    if not settings_store.get("allow_anyone_create_game") and user["role"] != "sysadmin":
        return render(request, "error.html", status_code=403, code=403,
                      message="Создавать игры может только сисадмин.")
    title = title.strip()
    if not title:
        return render(request, "game_new.html", colors=config.GAME_COLORS,
                      error="У игры должно быть название.")

    # Часы неприкосновенности задаются структурно — иначе сервис не смог бы их
    # проверять, а только показывать.
    rules = {"weapon": weapon.strip(), "safe_zones": safe_zones.strip(),
             "quiet_from": quiet_from.strip(), "quiet_to": quiet_to.strip(),
             "no_weekends": bool(no_weekends)}
    cur = execute(
        "INSERT INTO game (title, description, status, visibility, color, capacity,"
        " admin_user_id, rules_json, deadline_at, created_at)"
        " VALUES (?, ?, 'recruiting', ?, ?, ?, ?, ?, ?, ?)",
        (title[:80], description.strip()[:500],
         "private" if visibility == "private" else "open",
         color if color in config.GAME_COLORS else config.GAME_COLORS[0],
         int(capacity) if capacity.strip().isdigit() else None,
         user["id"], json.dumps(rules, ensure_ascii=False),
         deadline_at.strip() or None, now()))
    audit(user["id"], "game_created", "game", cur.lastrowid, {"title": title})
    return redirect(f"/manage/games/{cur.lastrowid}", "Игра создана. Соберите состав.")


@router.get("/games/{game_id}", response_class=HTMLResponse)
def manage_game(request: Request, game_id: int):
    user, game = owned_game(request, game_id)
    if game is None:
        return render(request, "error.html", status_code=404, code=404,
                      message="Игра не найдена.")
    if game is False:
        return render(request, "error.html", status_code=403, code=403,
                      message="Это не ваша игра.")

    me = repo.my_participation(game_id, user["id"])
    # Играющий админ видит только числа: даже обезличенная строка
    # «Пётр не подтвердил» подсказала бы ему, кто скоро выбывает. Выбывший админ
    # остаётся в урезанном режиме до финала — он по-прежнему рядом с живыми.
    admin_plays = (me is not None and me["status"] != "declined"
                   and game["status"] not in ("finished", "void"))
    candidates = query(
        "SELECT u.*, (SELECT COUNT(*) FROM participant p WHERE p.user_id = u.id"
        "  AND p.game_id = ?) AS in_game,"
        " (SELECT COUNT(*) FROM participant p JOIN game g2 ON g2.id = p.game_id"
        "  WHERE p.user_id = u.id AND g2.status IN ('running','paused','recruiting')"
        "  AND p.status IN ('invited','joined','alive')) AS busy"
        " FROM user u WHERE u.status = 'active' ORDER BY u.last_name, u.first_name",
        (game_id,)) if game["status"] in ("draft", "recruiting") else []

    stale = query(
        "SELECT v.display_name_snapshot AS who, c.created_at FROM kill_claim c"
        " JOIN participant v ON v.id = c.victim_id WHERE c.game_id = ? AND c.status = 'pending'"
        " ORDER BY c.created_at", (game_id,)) if not admin_plays else []

    return render(request, "manage_game.html", game=game, counters=repo.game_counters(game_id),
                  participants=repo.participants(game_id), candidates=candidates,
                  admin_plays=admin_plays, stale=stale,
                  min_players=config.MIN_PLAYERS,
                  display_name=repo.display_name)


@router.post("/games/{game_id}/invite")
def invite(request: Request, game_id: int, csrf: str = Form(""),
           user_ids: list[str] = Form(default=[]), mode: str = Form("selected")):
    auth.check_csrf(request, csrf)
    _user, game = owned_game(request, game_id)
    if not game:
        return redirect("/", "Игра недоступна.", "error")
    if game["status"] not in ("draft", "recruiting"):
        return redirect(f"/manage/games/{game_id}", "Набор закрыт.", "error")

    if mode == "all":
        rows = query("SELECT id FROM user WHERE status = 'active'")
        ids = [r["id"] for r in rows]
    elif mode == "free":
        rows = query(
            "SELECT u.id FROM user u WHERE u.status = 'active' AND NOT EXISTS ("
            " SELECT 1 FROM participant p JOIN game g ON g.id = p.game_id"
            " WHERE p.user_id = u.id AND g.status IN ('running','paused','recruiting')"
            " AND p.status IN ('invited','joined','alive'))")
        ids = [r["id"] for r in rows]
    else:
        ids = [int(x) for x in user_ids if str(x).isdigit()]

    added = 0
    skipped = 0
    with transaction() as conn:
        seats = None
        if game["capacity"]:
            taken = conn.execute(
                "SELECT COUNT(*) AS n FROM participant WHERE game_id = ? AND status IN"
                " ('invited','joined')", (game_id,)).fetchone()["n"]
            seats = max(game["capacity"] - taken, 0)
        for uid in ids:
            if seats is not None and added >= seats:
                break
            u = conn.execute("SELECT * FROM user WHERE id = ?", (uid,)).fetchone()
            if u is None or u["status"] != "active":
                continue
            exists = conn.execute("SELECT 1 AS x FROM participant WHERE game_id = ?"
                                  " AND user_id = ?", (game_id, uid)).fetchone()
            if exists:
                continue
            if repo.active_game_of(uid, exclude_game_id=game_id):
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO participant (game_id, user_id, display_name_snapshot,"
                " status, invited_at) VALUES (?, ?, ?, 'invited', ?)",
                (game_id, uid, repo.display_name(u), now()))
            game_logic.notify(conn, uid, "invited",
                              f"Вас добавили в игру «{game['title']}».", game_id)
            added += 1
    note = f"Приглашено: {added}." if added else "Новых приглашений нет."
    if skipped:
        note += f" Пропущено занятых в других играх: {skipped}."
    return redirect(f"/manage/games/{game_id}", note)


@router.post("/games/{game_id}/remove/{participant_id}")
def remove_participant(request: Request, game_id: int, participant_id: int,
                       csrf: str = Form("")):
    """Убрать из состава можно только до старта — после круг трогает сисадмин."""
    auth.check_csrf(request, csrf)
    _user, game = owned_game(request, game_id)
    if not game or game["status"] not in ("draft", "recruiting"):
        return redirect(f"/manage/games/{game_id}", "После старта состав меняет сисадмин.", "error")
    execute("DELETE FROM participant WHERE id = ? AND game_id = ?", (participant_id, game_id))
    return redirect(f"/manage/games/{game_id}", "Участник убран из состава.")


@router.post("/games/{game_id}/visibility")
def change_visibility(request: Request, game_id: int, csrf: str = Form(""),
                      visibility: str = Form("open")):
    auth.check_csrf(request, csrf)
    _user, game = owned_game(request, game_id)
    if not game:
        return redirect("/", "Игра недоступна.", "error")
    if game["status"] not in ("draft", "recruiting"):
        return redirect(f"/manage/games/{game_id}",
                        "Видимость переключается только до старта.", "error")
    execute("UPDATE game SET visibility = ? WHERE id = ?",
            ("private" if visibility == "private" else "open", game_id))
    return redirect(f"/manage/games/{game_id}", "Видимость изменена.")


@router.post("/games/{game_id}/start")
def start(request: Request, game_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    user, game = owned_game(request, game_id)
    if not game:
        return redirect("/", "Игра недоступна.", "error")
    try:
        with transaction() as conn:
            result = game_logic.start_game(conn, game_id, user["id"])
            audit(user["id"], "game_started", "game", game_id, result, conn=conn)
    except (ValueError, game_logic.ChainBroken) as exc:
        return redirect(f"/manage/games/{game_id}", str(exc), "error")
    return redirect(f"/manage/games/{game_id}",
                    f"Игра началась. Участников в круге: {result['players']}.")


@router.post("/games/{game_id}/pause")
def pause(request: Request, game_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    _user, game = owned_game(request, game_id)
    if not game:
        return redirect("/", "Игра недоступна.", "error")
    if game["status"] == "running":
        with transaction() as conn:
            conn.execute("UPDATE game SET status = 'paused' WHERE id = ?", (game_id,))
            game_logic.add_event(conn, game_id, "game_paused")
        return redirect(f"/manage/games/{game_id}", "Игра на паузе. Заявки заморожены.")
    if game["status"] == "paused":
        with transaction() as conn:
            conn.execute("UPDATE game SET status = 'running' WHERE id = ?", (game_id,))
            game_logic.add_event(conn, game_id, "game_resumed")
        return redirect(f"/manage/games/{game_id}", "Игра продолжается.")
    return redirect(f"/manage/games/{game_id}", "Паузу можно ставить только идущей игре.", "error")


@router.post("/games/{game_id}/finish")
def finish(request: Request, game_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    user, game = owned_game(request, game_id)
    if not game:
        return redirect("/", "Игра недоступна.", "error")
    if game["status"] not in ("running", "paused"):
        return redirect(f"/manage/games/{game_id}", "Игра не идёт.", "error")
    with transaction() as conn:
        game_logic.force_finish(conn, game_id)
        audit(user["id"], "game_force_finished", "game", game_id, conn=conn)
    return redirect(f"/games/{game_id}", "Игра завершена досрочно.")


@router.post("/games/{game_id}/deadline")
def extend_deadline(request: Request, game_id: int, csrf: str = Form(""),
                    deadline_at: str = Form("")):
    """Продлить срок — иначе планировщик завершит игру по дедлайну сам."""
    auth.check_csrf(request, csrf)
    user, game = owned_game(request, game_id)
    if not game:
        return redirect("/", "Игра недоступна.", "error")
    execute("UPDATE game SET deadline_at = ? WHERE id = ?",
            (deadline_at.strip() or None, game_id))
    audit(user["id"], "deadline_changed", "game", game_id, {"deadline": deadline_at})
    return redirect(f"/manage/games/{game_id}",
                    f"Срок игры: {deadline_at}" if deadline_at.strip() else "Срок снят.")


@router.post("/games/{game_id}/reveal")
def toggle_reveal(request: Request, game_id: int, csrf: str = Form("")):
    """Открыть разбор закрытой игры всем — когда прятать уже нечего."""
    auth.check_csrf(request, csrf)
    user, game = owned_game(request, game_id)
    if not game:
        return redirect("/", "Игра недоступна.", "error")
    new_value = 0 if game["reveal_after_finish"] else 1
    execute("UPDATE game SET reveal_after_finish = ? WHERE id = ?", (new_value, game_id))
    audit(user["id"], "game_reveal_toggled", "game", game_id, {"reveal": new_value})
    return redirect(f"/manage/games/{game_id}", "Разбор открыт для всех."
                    if new_value else "Разбор снова только для участников.")


@router.post("/games/{game_id}/request-withdraw")
def request_withdraw(request: Request, game_id: int, csrf: str = Form(""),
                     participant_id: str = Form(""), reason: str = Form("")):
    """Застрявшую игру расшивает сисадмин — здесь админ только просит."""
    auth.check_csrf(request, csrf)
    user, game = owned_game(request, game_id)
    if not game:
        return redirect("/", "Игра недоступна.", "error")
    pid = int(participant_id) if participant_id.isdigit() else None
    if pid is None:
        return redirect(f"/manage/games/{game_id}", "Выберите участника.", "error")
    code = security.support_code()
    execute("INSERT INTO support_request (code, user_id, game_id, subject_participant_id,"
            " type, message, created_at) VALUES (?, ?, ?, ?, 'withdraw', ?, ?)",
            (code, user["id"], game_id, pid, reason.strip()[:500], now()))
    return redirect(f"/manage/games/{game_id}",
                    f"Заявка {code} отправлена сисадмину.")
