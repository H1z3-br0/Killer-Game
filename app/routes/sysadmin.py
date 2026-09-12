"""Панель сисадмина: пользователи, заявки, игры, настройки, бэкапы, аудит.

Здесь и только здесь живут две операции, меняющие цепочку: вывод участника и
откат последнего события. Экрана с самой цепочкой нет и тут — её можно
прочитать лишь из файла базы.
"""
from __future__ import annotations

import shutil
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import auth, config, game_logic, repo, security, settings_store
from ..db import audit, execute, now, query, query_one, transaction
from ..web import redirect, render

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
def overview(request: Request):
    auth.require_sysadmin(request)
    stats = {
        "users": query_one("SELECT COUNT(*) AS n FROM user")["n"],
        "active_week": query_one(
            "SELECT COUNT(*) AS n FROM user WHERE last_seen_at >= ?",
            (security.expires_in(days=-7),))["n"],
        "running": query_one("SELECT COUNT(*) AS n FROM game WHERE status IN"
                             " ('running','paused')")["n"],
        "open_requests": query_one("SELECT COUNT(*) AS n FROM support_request"
                                   " WHERE status = 'open'")["n"],
        "db_size": config.DB_PATH.stat().st_size // 1024 if config.DB_PATH.exists() else 0,
        "backups": len(list(config.BACKUP_DIR.glob("*.db"))) if config.BACKUP_DIR.exists() else 0,
        "uptime": uptime_text(),
        "errors": query_one("SELECT COUNT(*) AS n FROM error_log")["n"],
    }
    orphan = query(
        "SELECT g.* FROM game g JOIN user u ON u.id = g.admin_user_id"
        " WHERE u.status = 'blocked' AND g.status IN ('recruiting','running','paused')")
    return render(request, "sysadmin/overview.html", page="overview", stats=stats, orphan=orphan,
                  requests=query("SELECT * FROM support_request WHERE status = 'open'"
                                 " ORDER BY id DESC LIMIT 10"))


def uptime_text() -> str:
    from ..main import STARTED_AT
    seconds = int(time.time() - STARTED_AT)
    days, rest = divmod(seconds, 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return f"{days} д {hours} ч"
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, q: str = "", filter: str = ""):
    auth.require_sysadmin(request)
    sql = ("SELECT u.*,"
           " (SELECT COUNT(*) FROM participant p JOIN game g ON g.id = p.game_id"
           " WHERE p.user_id = u.id AND g.status IN ('running','paused','recruiting')"
           " AND p.status IN ('invited','joined','alive')) AS active_games FROM user u WHERE 1=1")
    params: list = []
    if q.strip():
        sql += " AND (u.name_normalized LIKE ? OR u.login LIKE ?)"
        params += [f"%{security.normalize_name(q)}%", f"%{security.normalize_login(q)}%"]
    if filter == "blocked":
        sql += " AND u.status = 'blocked'"
    elif filter == "never":
        sql += " AND u.last_seen_at IS NULL"
    elif filter == "today":
        sql += " AND u.created_at >= ?"
        params.append(security.expires_in(days=-1))
    sql += " ORDER BY u.last_name, u.first_name LIMIT 300"
    return render(request, "sysadmin/users.html", page="users", rows=query(sql, tuple(params)),
                  q=q, filter=filter, display_name=repo.display_name)


@router.post("/users/{user_id}/block")
def block_user(request: Request, user_id: int, csrf: str = Form("")):
    """Блокировка выводит человека из всех активных игр.

    Оставшись в круге, заблокированный не смог бы подтвердить своё устранение,
    и без арбитража игра встала бы до конца срока.
    """
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    target = query_one("SELECT * FROM user WHERE id = ?", (user_id,))
    if target is None:
        return redirect("/admin/users", "Пользователь не найден.", "error")

    new_status = "active" if target["status"] == "blocked" else "blocked"
    withdrawn = 0
    with transaction() as conn:
        conn.execute("UPDATE user SET status = ? WHERE id = ?", (new_status, user_id))
        if new_status == "blocked":
            conn.execute("UPDATE session SET revoked_at = ? WHERE user_id = ?", (now(), user_id))
            rows = conn.execute(
                "SELECT p.id, p.game_id FROM participant p JOIN game g ON g.id = p.game_id"
                " WHERE p.user_id = ? AND p.status = 'alive' AND g.status IN ('running','paused')",
                (user_id,)).fetchall()
            for r in rows:
                game_logic.eliminate(conn, r["game_id"], r["id"], "participant_withdrawn",
                                     actor_user_id=admin["id"],
                                            reason="учётная запись заблокирована")
                withdrawn += 1
        audit(admin["id"], f"user_{new_status}", "user", user_id,
              {"withdrawn_from": withdrawn}, conn=conn)
    suffix = f" Выведен из игр: {withdrawn}." if withdrawn else ""
    return redirect("/admin/users",
                    ("Заблокирован." if new_status == "blocked" else "Разблокирован.") + suffix)


@router.post("/users/{user_id}/reset")
def reset_password(request: Request, user_id: int, csrf: str = Form("")):
    """Пароль сисадмин не видит и не придумывает — выдаётся одноразовый код."""
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    code = security.safe_code(8)
    execute("INSERT INTO reset_code (user_id, code_hash, issued_by, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, security.digest(code), admin["id"],
             security.expires_in(minutes=config.RESET_CODE_TTL_MINUTES)))
    audit(admin["id"], "password_reset_issued", "user", user_id)
    return render(request, "sysadmin/reset_code.html", code=code,
                  minutes=config.RESET_CODE_TTL_MINUTES,
                  target=query_one("SELECT * FROM user WHERE id = ?", (user_id,)))


@router.post("/users/{user_id}/role")
def toggle_role(request: Request, user_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    target = query_one("SELECT * FROM user WHERE id = ?", (user_id,))
    if target is None:
        return redirect("/admin/users", "Пользователь не найден.", "error")
    if target["id"] == admin["id"]:
        return redirect("/admin/users", "Себя разжаловать нельзя.", "error")
    new_role = "user" if target["role"] == "sysadmin" else "sysadmin"
    execute("UPDATE user SET role = ? WHERE id = ?", (new_role, user_id))
    audit(admin["id"], "role_changed", "user", user_id, {"role": new_role})
    return redirect("/admin/users", f"Роль изменена: {new_role}.")


@router.post("/users/{user_id}/anonymize")
def anonymize(request: Request, user_id: int, csrf: str = Form("")):
    """Человек ушёл и просит стереть данные: имя уходит, история игр остаётся."""
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    with transaction() as conn:
        conn.execute(
            "UPDATE user SET last_name = 'Бывший', first_name = 'участник', middle_name = '',"
            " name_normalized = ?, login = ?, status = 'blocked', telegram = ''"
            " WHERE id = ?", (f"удалён-{user_id}", f"deleted{user_id}", user_id))
        conn.execute("UPDATE participant SET display_name_snapshot = 'Бывший участник'"
                     " WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE session SET revoked_at = ? WHERE user_id = ?", (now(), user_id))
        audit(admin["id"], "user_anonymized", "user", user_id, conn=conn)
    return redirect("/admin/users", "Аккаунт обезличен, история игр сохранена.")


@router.post("/users/{user_id}/delete")
def delete_user(request: Request, user_id: int, csrf: str = Form(""),
                confirm_name: str = Form("")):
    """Полное удаление: учётка и личная статистика стираются, а в логах игр
    остаётся снимок имени, чтобы прошлые игры читались целиком."""
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    target = query_one("SELECT * FROM user WHERE id = ?", (user_id,))
    if target is None:
        return redirect("/admin/users", "Пользователь не найден.", "error")
    if security.normalize_name(confirm_name) != target["name_normalized"]:
        return redirect("/admin/users", "Для удаления введите ФИО точно.", "error")
    active = query_one(
        "SELECT COUNT(*) AS n FROM participant p JOIN game g ON g.id = p.game_id"
        " WHERE p.user_id = ? AND p.status = 'alive' AND g.status IN ('running','paused')",
        (user_id,))
    if active and active["n"]:
        return redirect("/admin/users",
                        "Сначала выведите человека из идущих игр.", "error")
    with transaction() as conn:
        conn.execute("UPDATE participant SET user_id = NULL,"
                     " display_name_snapshot = 'Удалённый участник' WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM achievement WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM notification WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM support_request WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user WHERE id = ?", (user_id,))
        audit(admin["id"], "user_deleted", "user", user_id,
              {"name": repo.display_name(target)}, conn=conn)
    return redirect("/admin/users", "Учётная запись удалена полностью.")


# ─────────────────────────── заявки ───────────────────────────


@router.get("/support", response_class=HTMLResponse)
def support(request: Request, status: str = "open"):
    auth.require_sysadmin(request)
    rows = query(
        "SELECT s.*, g.title AS game_title, p.display_name_snapshot AS subject_name,"
        " u.last_name, u.first_name, u.middle_name FROM support_request s"
        " LEFT JOIN game g ON g.id = s.game_id"
        " LEFT JOIN participant p ON p.id = s.subject_participant_id"
        " LEFT JOIN user u ON u.id = s.user_id WHERE s.status = ? ORDER BY s.id DESC LIMIT 200",
        (status,))
    return render(request, "sysadmin/support.html", page="support", rows=rows, status=status)


@router.post("/support/{req_id}/resolve")
def resolve_request(request: Request, req_id: int, csrf: str = Form(""),
                    action: str = Form("close")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    req = query_one("SELECT * FROM support_request WHERE id = ?", (req_id,))
    if req is None:
        return redirect("/admin/support", "Заявка не найдена.", "error")

    if action == "withdraw" and req["game_id"] and req["subject_participant_id"]:
        try:
            with transaction() as conn:
                game_logic.eliminate(conn, req["game_id"], req["subject_participant_id"],
                                     "participant_withdrawn", actor_user_id=admin["id"],
                                     reason=req["message"] or "по заявке админа игры")
                conn.execute("UPDATE support_request SET status = 'resolved', resolved_at = ?,"
                             " resolved_by = ? WHERE id = ?", (now(), admin["id"], req_id))
                audit(admin["id"], "participant_withdrawn", "game", req["game_id"],
                      {"participant": req["subject_participant_id"]}, conn=conn)
        except (ValueError, game_logic.ChainBroken) as exc:
            return redirect("/admin/support", str(exc), "error")
        return redirect("/admin/support", "Участник выведен, круг пересшит.")

    execute("UPDATE support_request SET status = ?, resolved_at = ?, resolved_by = ?"
            " WHERE id = ?",
            ("rejected" if action == "reject" else "resolved", now(), admin["id"], req_id))
    return redirect("/admin/support", "Заявка закрыта.")


# ─────────────────────────── игры ───────────────────────────


@router.get("/games", response_class=HTMLResponse)
def games(request: Request):
    auth.require_sysadmin(request)
    rows = query(
        "SELECT g.*, u.last_name, u.first_name, u.middle_name,"
        " (SELECT COUNT(*) FROM participant p WHERE p.game_id = g.id AND p.status = 'alive')"
        " AS alive FROM game g LEFT JOIN user u ON u.id = g.admin_user_id ORDER BY g.id DESC")
    return render(request, "sysadmin/games.html", page="games", rows=rows,
                  display_name=repo.display_name)


@router.get("/games/{game_id}", response_class=HTMLResponse)
def game_detail(request: Request, game_id: int):
    auth.require_sysadmin(request)
    game = repo.game_by_id(game_id)
    if game is None:
        return render(request, "error.html", code=404, message="Игра не найдена.")
    last = query_one("SELECT * FROM event WHERE game_id = ? AND reverted_at IS NULL"
                     " ORDER BY id DESC LIMIT 1", (game_id,))
    with_conn_check = None
    from ..db import connect
    with_conn_check = game_logic.verify_chain(connect(), game_id)
    return render(request, "sysadmin/game_detail.html", game=game,
                  participants=repo.participants(game_id),
                  counters=repo.game_counters(game_id), last_event=last,
                  check=with_conn_check,
                  candidates=query("SELECT id, display_name_snapshot FROM participant"
                                   " WHERE game_id = ? AND status = 'alive'"
                                   " ORDER BY display_name_snapshot", (game_id,)))


@router.post("/games/{game_id}/withdraw")
def withdraw(request: Request, game_id: int, csrf: str = Form(""),
             participant_id: str = Form(""), reason: str = Form("")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    if not participant_id.isdigit():
        return redirect(f"/admin/games/{game_id}", "Выберите участника.", "error")
    try:
        with transaction() as conn:
            game_logic.eliminate(conn, game_id, int(participant_id), "participant_withdrawn",
                                 actor_user_id=admin["id"], reason=reason.strip())
            audit(admin["id"], "participant_withdrawn", "game", game_id,
                  {"participant": int(participant_id), "reason": reason}, conn=conn)
    except (ValueError, game_logic.ChainBroken) as exc:
        return redirect(f"/admin/games/{game_id}", str(exc), "error")
    return redirect(f"/admin/games/{game_id}", "Участник выведен, круг пересшит.")


@router.post("/games/{game_id}/revert")
def revert(request: Request, game_id: int, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    try:
        with transaction() as conn:
            message = game_logic.revert_last_event(conn, game_id)
            audit(admin["id"], "event_reverted", "game", game_id, {"message": message}, conn=conn)
    except (ValueError, game_logic.ChainBroken) as exc:
        return redirect(f"/admin/games/{game_id}", str(exc), "error")
    return redirect(f"/admin/games/{game_id}", message)


@router.post("/games/{game_id}/transfer")
def transfer(request: Request, game_id: int, csrf: str = Form(""), user_id: str = Form("")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    if not user_id.isdigit():
        return redirect(f"/admin/games/{game_id}", "Выберите нового админа.", "error")
    execute("UPDATE game SET admin_user_id = ? WHERE id = ?", (int(user_id), game_id))
    audit(admin["id"], "game_transferred", "game", game_id, {"to": int(user_id)})
    return redirect(f"/admin/games/{game_id}", "Права на игру переданы.")


# ─────────────────────────── настройки, бэкапы, аудит ───────────────────────────


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    auth.require_sysadmin(request)
    return render(request, "sysadmin/settings.html", page="settings",
                  values=settings_store.all_settings())


@router.post("/settings")
def settings_save(request: Request, csrf: str = Form(""), platform_name: str = Form("Киллер"),
                  support_telegram: str = Form(""), maintenance_message: str = Form(""),
                  allow_anyone_create_game: str = Form(""),
                  allow_multiple_active_games: str = Form(""), subnet_allowlist: str = Form("")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    settings_store.set_value("platform_name", platform_name.strip()[:40] or "Киллер",
                             admin["id"])
    settings_store.set_value("support_telegram",
                             support_telegram.strip().lstrip("@")[:60], admin["id"])
    settings_store.set_value("maintenance_message", maintenance_message.strip()[:200],
                             admin["id"])
    settings_store.set_value("allow_anyone_create_game",
                             bool(allow_anyone_create_game), admin["id"])
    settings_store.set_value("allow_multiple_active_games",
                             bool(allow_multiple_active_games), admin["id"])
    settings_store.set_value("subnet_allowlist", subnet_allowlist.strip()[:200],
                             admin["id"])
    audit(admin["id"], "settings_changed", "setting")
    return redirect("/admin/settings", "Настройки сохранены.")


@router.get("/backups", response_class=HTMLResponse)
def backups(request: Request):
    auth.require_sysadmin(request)
    files = sorted(config.BACKUP_DIR.glob("*.db"),
                   reverse=True) if config.BACKUP_DIR.exists() else []
    rows = [{"name": f.name, "size": f.stat().st_size // 1024,
             "at": f.stat().st_mtime} for f in files]
    running = query("SELECT id, title FROM game WHERE status IN ('running','paused')")
    return render(request, "sysadmin/backups.html", page="backups", rows=rows, running=running)


@router.post("/backups")
def make_backup(request: Request, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"killer-{now().replace(':', '-')}.db"
    from ..db import connect
    target = config.BACKUP_DIR / name
    with connect() as src:  # sqlite3 умеет консистентную копию на лету
        dst = __import__("sqlite3").connect(target)
        src.backup(dst)
        dst.close()
    audit(admin["id"], "backup_created", "backup", None, {"file": name})
    return redirect("/admin/backups", f"Бэкап создан: {name}")


@router.post("/backups/restore")
def restore_backup(request: Request, csrf: str = Form(""), name: str = Form("")):
    """Восстановление воскрешает мёртвых: всё после копии исчезает.

    Поэтому идущие игры сначала уходят на паузу.
    """
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    src = config.BACKUP_DIR / name
    if not src.exists() or src.parent != config.BACKUP_DIR:
        return redirect("/admin/backups", "Копия не найдена.", "error")
    execute("UPDATE game SET status = 'paused' WHERE status = 'running'")
    audit(admin["id"], "backup_restore_requested", "backup", None, {"file": name})
    shutil.copy2(config.DB_PATH, config.DB_PATH.with_suffix(".before-restore.db"))
    shutil.copy2(src, config.DB_PATH)
    return redirect("/admin/backups",
                    "База восстановлена. Перезапустите сервис, чтобы применить.")


@router.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, action: str = ""):
    auth.require_sysadmin(request)
    sql = ("SELECT a.*, u.last_name, u.first_name, u.middle_name FROM audit_log a"
           " LEFT JOIN user u ON u.id = a.actor_user_id WHERE 1=1")
    params: list = []
    if action.strip():
        sql += " AND a.action LIKE ?"
        params.append(f"%{action.strip()}%")
    sql += " ORDER BY a.id DESC LIMIT 300"
    return render(request, "sysadmin/audit.html", page="audit", rows=query(sql, tuple(params)),
                  action=action, display_name=repo.display_name)


@router.get("/integrity", response_class=HTMLResponse)
def integrity(request: Request):
    """Проверка инварианта во всех активных играх — по кнопке, а не по вере."""
    auth.require_sysadmin(request)
    from ..db import connect
    conn = connect()
    rows = []
    for g in query("SELECT * FROM game WHERE status IN ('running','paused')"):
        rows.append({"game": g, "check": game_logic.verify_chain(conn, g["id"])})
    return render(request, "sysadmin/integrity.html", page="integrity", rows=rows)


@router.get("/users/{user_id}", response_class=HTMLResponse)
def user_card(request: Request, user_id: int):
    auth.require_sysadmin(request)
    card = repo.user_card(user_id)
    if not card:
        return render(request, "error.html", status_code=404, code=404,
                      message="Пользователь не найден.")
    return render(request, "sysadmin/user_card.html", display_name=repo.display_name, **card)


@router.get("/duplicates", response_class=HTMLResponse)
def duplicates(request: Request):
    """Похожие ФИО: опечатка при регистрации рождает тихий дубль."""
    auth.require_sysadmin(request)
    return render(request, "sysadmin/duplicates.html", page="dupes",
                  pairs=repo.possible_duplicates())


@router.post("/users/{keep_id}/merge")
def merge_users(request: Request, keep_id: int, csrf: str = Form(""),
                drop_id: str = Form("")):
    """Сливаем дубль в основную запись: история игр и достижения переезжают."""
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    if not drop_id.isdigit() or int(drop_id) == keep_id:
        return redirect("/admin/duplicates", "Выберите разные записи.", "error")
    drop = int(drop_id)
    conflict = query_one(
        "SELECT 1 AS x FROM participant a JOIN participant b ON a.game_id = b.game_id"
        " WHERE a.user_id = ? AND b.user_id = ?", (keep_id, drop))
    if conflict:
        return redirect("/admin/duplicates",
                        "Обе записи участвуют в одной игре — это разные люди.", "error")
    with transaction() as conn:
        conn.execute("UPDATE participant SET user_id = ? WHERE user_id = ?", (keep_id, drop))
        conn.execute("UPDATE achievement SET user_id = ? WHERE user_id = ?", (keep_id, drop))
        conn.execute("UPDATE notification SET user_id = ? WHERE user_id = ?", (keep_id, drop))
        conn.execute("DELETE FROM session WHERE user_id = ?", (drop,))
        conn.execute("DELETE FROM user WHERE id = ?", (drop,))
        audit(admin["id"], "users_merged", "user", keep_id, {"dropped": drop}, conn=conn)
    return redirect("/admin/duplicates", "Записи объединены.")


@router.get("/maintenance", response_class=HTMLResponse)
def maintenance(request: Request):
    auth.require_sysadmin(request)
    import shutil as _shutil
    free_mb = _shutil.disk_usage(config.DB_PATH.parent).free // (1024 * 1024)
    return render(
        request, "sysadmin/maintenance.html", page="maint",
        uptime=uptime_text(), free_mb=free_mb,
        db_size=config.DB_PATH.stat().st_size // 1024 if config.DB_PATH.exists() else 0,
        errors=query("SELECT * FROM error_log ORDER BY id DESC LIMIT 50"),
        stats=query("SELECT * FROM request_stat ORDER BY day DESC LIMIT 14"),
        sessions=query_one("SELECT COUNT(*) AS n FROM session WHERE revoked_at IS NULL")["n"],
    )


@router.post("/maintenance/cleanup")
def run_cleanup(request: Request, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    from .. import scheduler
    removed = scheduler.cleanup()
    audit(admin["id"], "cleanup", "maintenance", None, {"removed": removed})
    return redirect("/admin/maintenance", f"Очищено записей: {removed}.")


@router.post("/maintenance/errors/clear")
def clear_errors(request: Request, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    admin = auth.require_sysadmin(request)
    execute("DELETE FROM error_log")
    audit(admin["id"], "errors_cleared", "maintenance")
    return redirect("/admin/maintenance", "Журнал ошибок очищен.")
