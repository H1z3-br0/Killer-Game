"""Экраны игрока: главная, игра, цель, заявки, профиль, зал славы."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .. import auth, game_logic, repo, security, settings_store
from ..db import execute, now, query, query_one, transaction
from ..web import redirect, render

router = APIRouter()


def visible_to(game, participation, user) -> bool:
    """Приватная игра закрывает вход и содержание, но не факт существования."""
    if game["visibility"] == "open":
        return True
    if participation is not None:
        return True
    if game["status"] == "finished" and game["reveal_after_finish"]:
        return True
    return user is not None and user["role"] == "sysadmin"


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = auth.current_user(request)
    if user is None:
        # Гостю показываем ленту: что за игры идут в компании — не секрет.
        return render(request, "landing.html", feed=repo.platform_feed(12))
    return render(
        request, "home.html",
        pending=repo.pending_claims_for(user["id"]),
        games=repo.my_games(user["id"]),
        catalog=[g for g in repo.open_catalog(user["id"]) if not g["mine"]][:6],
        feed=repo.platform_feed(12),
        stats=repo.user_stats(user["id"]),
    )


@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    """Правила доступны всем, в том числе до регистрации."""
    return render(request, "rules.html")


@router.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request):
    user = auth.require_user(request)
    return render(request, "catalog.html", games=repo.open_catalog(user["id"]))


@router.get("/hall", response_class=HTMLResponse)
def hall(request: Request, year: str = ""):
    auth.require_user(request)
    return render(request, "hall.html", rows=repo.hall_of_fame_year(year or None),
                  years=repo.game_years(), year=year)


@router.get("/games/{game_id}", response_class=HTMLResponse)
def game_page(request: Request, game_id: int):
    user = auth.require_user(request)
    game = repo.game_by_id(game_id)
    if game is None:
        return render(request, "error.html", code=404, message="Игра не найдена.")
    me = repo.my_participation(game_id, user["id"])
    if not visible_to(game, me, user):
        # Факт игры публичен, содержание — нет.
        return render(request, "game_closed.html", game=game)

    counters = repo.game_counters(game_id)
    is_admin = game["admin_user_id"] == user["id"]
    claim = None
    if me is not None:
        claim = query_one(
            "SELECT c.*, k.display_name_snapshot AS killer_name,"
            " v.display_name_snapshot AS victim_name FROM kill_claim c"
            " JOIN participant k ON k.id = c.killer_id JOIN participant v ON v.id = c.victim_id"
            " WHERE c.game_id = ? AND c.status = 'pending' AND ? IN (c.killer_id, c.victim_id)",
            (game_id, me["id"]))
    finished = game["status"] in ("finished", "void")
    return render(
        request, "game.html", game=game, me=me, counters=counters, is_admin=is_admin,
        claim=claim, feed=repo.feed(game_id),
        by_day=repo.eliminations_by_day(game_id) if finished else [],
        records=repo.game_records(game_id) if finished else {},
        participants=repo.participants(game_id) if (is_admin or game["status"] in
                                                    ("draft", "recruiting")) else [],
        log=repo.full_log(game_id) if game["status"] in ("finished", "void") else [],
        rules=game["rules_json"],
    )


@router.get("/games/{game_id}/state")
def game_state(request: Request, game_id: int):
    """Лёгкая сводка для автообновления: пуш по http недоступен, опрашиваем."""
    user = auth.current_user(request)
    if user is None:
        return JSONResponse({"error": "no-session"}, status_code=403)
    game = repo.game_by_id(game_id)
    me = repo.my_participation(game_id, user["id"])
    if game is None or not visible_to(game, me, user):
        return JSONResponse({"error": "closed"}, status_code=403)
    counters = repo.game_counters(game_id)
    claim = None
    if me is not None:
        row = query_one("SELECT id FROM kill_claim WHERE game_id = ? AND status = 'pending'"
                        " AND ? IN (killer_id, victim_id)", (game_id, me["id"]))
        claim = row["id"] if row else None
    return JSONResponse({"alive": counters["alive"], "status": game["status"],
                         "claim": claim, "my_status": me["status"] if me else None})


@router.get("/games/{game_id}/export.csv")
def export_csv(request: Request, game_id: int):
    """Разбор игры таблицей — после финала участникам, ведущему всегда."""
    user = auth.require_user(request)
    game = repo.game_by_id(game_id)
    if game is None:
        return PlainTextResponse("нет такой игры", status_code=404)
    me = repo.my_participation(game_id, user["id"])
    allowed = (game["admin_user_id"] == user["id"] or user["role"] == "sysadmin"
               or (me is not None and game["status"] in ("finished", "void")))
    if not allowed:
        return PlainTextResponse("экспорт доступен после финала", status_code=403)
    name = f"killer-game-{game_id}.csv"
    return PlainTextResponse(
        repo.game_csv(game_id), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/games/{game_id}/target/reveal")
def reveal_target(request: Request, game_id: int, csrf: str = Form("")):
    """Имя цели уходит только этим запросом и только своему владельцу —
    в HTML страницы его нет, иначе скрывающая плашка бессмысленна."""
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    me = repo.my_participation(game_id, user["id"])
    if me is None or me["status"] != "alive":
        return JSONResponse({"error": "Вы не в игре."}, status_code=403)
    name = repo.target_for_participant(me["id"])
    if not name:
        return JSONResponse({"error": "Цели нет."}, status_code=404)
    return JSONResponse({"target": name})


@router.post("/games/{game_id}/join")
def join_game(request: Request, game_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    game = repo.game_by_id(game_id)
    if game is None or game["status"] not in ("draft", "recruiting"):
        return redirect(f"/games/{game_id}", "Набор в эту игру закрыт.", "error")

    me = repo.my_participation(game_id, user["id"])
    if me is None:
        # В приватную игру по своей инициативе войти нельзя — только приглашение.
        if game["visibility"] != "open":
            return redirect("/", "Это закрытая игра, вход только по приглашению.", "error")
        if game["capacity"]:
            joined = repo.game_counters(game_id)["joined"]
            if joined >= game["capacity"]:
                return redirect("/catalog", "Мест больше нет.", "error")
        if game["admin_user_id"] == user["id"]:
            pass  # админ вправе играть в своей игре: цепочку он всё равно не видит
        execute("INSERT INTO participant (game_id, user_id, display_name_snapshot,"
                " status, invited_at, joined_at) VALUES (?, ?, ?, 'joined', ?, ?)",
                (game_id, user["id"], repo.display_name(user), now(), now()))
    else:
        execute("UPDATE participant SET status = 'joined', joined_at = ? WHERE id = ?",
                (now(), me["id"]))
    return redirect(f"/games/{game_id}", "Вы в составе. Ждём старта.")


@router.post("/games/{game_id}/decline")
def decline_game(request: Request, game_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    me = repo.my_participation(game_id, user["id"])
    if me and me["status"] in ("invited", "joined"):
        execute("UPDATE participant SET status = 'declined' WHERE id = ?", (me["id"],))
    return redirect("/", "Вы отказались от участия.")


@router.post("/games/{game_id}/claims")
def create_claim(request: Request, game_id: int, csrf: str = Form(""),
                 as_victim: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    if security.rate_limit_hit(str(user["id"]), "claim"):
        return redirect(f"/games/{game_id}", "Слишком часто. Подождите.", "error")
    me = repo.my_participation(game_id, user["id"])
    if me is None:
        return redirect("/", "Вы не в этой игре.", "error")
    try:
        with transaction() as conn:
            game_logic.create_claim(conn, game_id, me["id"], as_victim=bool(as_victim))
    except ValueError as exc:
        return redirect(f"/games/{game_id}", str(exc), "error")
    return redirect(f"/games/{game_id}", "Заявка подана. Ждём подтверждения второй стороны.")


@router.post("/claims/{claim_id}/confirm")
def confirm_claim(request: Request, claim_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    claim = query_one("SELECT * FROM kill_claim WHERE id = ?", (claim_id,))
    if claim is None:
        return redirect("/", "Заявка не найдена.", "error")
    me = repo.my_participation(claim["game_id"], user["id"])
    if me is None:
        return redirect("/", "Вы не в этой игре.", "error")
    try:
        with transaction() as conn:
            game_logic.confirm_claim(conn, claim_id, me["id"])
    except (ValueError, game_logic.ChainBroken) as exc:
        return redirect(f"/games/{claim['game_id']}", str(exc), "error")
    return redirect(f"/games/{claim['game_id']}", "Подтверждено.")


@router.post("/claims/{claim_id}/cancel")
def cancel_claim(request: Request, claim_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    claim = query_one("SELECT * FROM kill_claim WHERE id = ?", (claim_id,))
    if claim is None:
        return redirect("/", "Заявка не найдена.", "error")
    me = repo.my_participation(claim["game_id"], user["id"])
    if me is None or claim["created_by"] != me["id"]:
        return redirect(f"/games/{claim['game_id']}", "Отменить может только автор.", "error")
    execute("UPDATE kill_claim SET status = 'cancelled', cancel_reason = 'cancelled_by_author',"
            " resolved_at = ? WHERE id = ? AND status = 'pending'", (now(), claim_id))
    return redirect(f"/games/{claim['game_id']}", "Заявка отменена.")


# ─────────────────────────── профиль ───────────────────────────


@router.get("/profile", response_class=HTMLResponse)
def profile(request: Request):
    user = auth.require_user(request)
    return render(
        request, "profile.html",
        stats=repo.user_stats(user["id"]),
        achievements=repo.achievements(user["id"]),
        games=repo.my_games(user["id"]),
        sessions=query("SELECT * FROM session WHERE user_id = ? AND revoked_at IS NULL"
                       " ORDER BY last_seen_at DESC", (user["id"],)),
        telegram_support=settings_store.get("support_telegram"),
    )


@router.post("/profile")
def profile_save(request: Request, csrf: str = Form(""), avatar_emoji: str = Form("🕵"),
                 department: str = Form(""), telegram: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    execute("UPDATE user SET avatar_emoji = ?, department = ?, telegram = ? WHERE id = ?",
            (avatar_emoji[:4] or "🕵", department.strip()[:60],
             telegram.strip().lstrip("@")[:60], user["id"]))
    return redirect("/profile", "Профиль сохранён.")


@router.post("/profile/password")
def change_password(request: Request, csrf: str = Form(""), current: str = Form(""),
                    password: str = Form(""), password2: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    if not security.verify_password(user["password_hash"], current):
        return redirect("/profile", "Текущий пароль не подходит.", "error")
    if len(password) < 6 or password != password2:
        return redirect("/profile", "Новый пароль короткий или не совпадает.", "error")
    execute("UPDATE user SET password_hash = ? WHERE id = ?",
            (security.hash_password(password), user["id"]))
    return redirect("/profile", "Пароль изменён.")


@router.post("/profile/sessions/{session_id}/revoke")
def revoke_session(request: Request, session_id: str, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    execute("UPDATE session SET revoked_at = ? WHERE id = ? AND user_id = ?",
            (now(), session_id, user["id"]))
    return redirect("/profile", "Устройство отключено.")


@router.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request):
    user = auth.require_user(request)
    items = repo.notifications(user["id"], 50)
    execute("UPDATE notification SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
            (now(), user["id"]))
    return render(request, "notifications.html", items=items)
