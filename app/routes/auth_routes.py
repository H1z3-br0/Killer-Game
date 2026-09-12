"""Регистрация, вход, выход, вход на новом устройстве, обращения в поддержку."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, repo, security, settings_store
from ..db import audit, execute, now, query_one
from ..web import redirect, render

router = APIRouter()


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    if auth.current_user(request):
        return redirect("/")
    return render(request, "register.html")


@router.post("/register")
def register(request: Request, csrf: str = Form(""), login: str = Form(""),
             last_name: str = Form(""), first_name: str = Form(""),
             middle_name: str = Form(""), password: str = Form(""),
             password2: str = Form("")):
    auth.check_csrf(request, csrf)
    last_name, first_name = last_name.strip(), first_name.strip()
    middle_name = middle_name.strip()
    login = security.normalize_login(login)
    form = {"login": login, "last_name": last_name, "first_name": first_name,
            "middle_name": middle_name}

    if security.rate_limit_hit(client_key(request), "register"):
        return render(request, "register.html", form=form,
                      error="Слишком много регистраций с этого устройства. Попробуйте позже.")
    if not last_name or not first_name:
        return render(request, "register.html", form=form, error="Укажите фамилию и имя.")
    if not login:
        return render(request, "register.html", form=form,
                      error="Придумайте логин — с ним вы будете входить.")
    if not security.login_is_valid(login):
        return render(request, "register.html", form=form,
                      error="Логин: латиница и цифры, от 3 до 20 символов.")
    if repo.user_by_login(login):
        return render(request, "register.html", form=form,
                      error="Такой логин уже занят — придумайте другой.")
    if len(password) < 6:
        return render(request, "register.html", form=form, error="Пароль короче шести символов.")
    if password != password2:
        return render(request, "register.html", form=form, error="Пароли не совпадают.")

    # Полные тёзки допустимы: уникален логин, а ФИО — отображаемое имя.
    normalized = security.normalize_name(last_name, first_name, middle_name)
    cur = execute(
        "INSERT INTO user (login, last_name, first_name, middle_name, name_normalized,"
        " password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (login, last_name, first_name, middle_name, normalized,
         security.hash_password(password),
         "sysadmin" if not query_one("SELECT 1 AS x FROM user LIMIT 1") else "user",
         now()))
    user_id = cur.lastrowid
    audit(user_id, "register", "user", user_id)
    response = redirect("/", "Добро пожаловать. Аккаунт создан.")
    auth.set_session_cookie(response, auth.create_session(user_id, request.headers.get("user-agent", "")))
    return response


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if auth.current_user(request):
        return redirect("/")
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, csrf: str = Form(""), login: str = Form(""),
          password: str = Form("")):
    auth.check_csrf(request, csrf)
    if security.rate_limit_hit(client_key(request), "login"):
        return render(request, "login.html",
                      error="Слишком много попыток. Подождите несколько минут.")

    user = repo.user_by_login(security.normalize_login(login))
    if user is None or not security.verify_password(user["password_hash"], password):
        return render(request, "login.html", form={"login": login},
                      error="Не сходится логин или пароль.")
    if user["status"] == "blocked":
        return render(request, "login.html",
                      error="Учётная запись заблокирована. Напишите сисадмину.")

    response = redirect("/", f"С возвращением, {user['first_name']}.")
    auth.set_session_cookie(response, auth.create_session(user["id"], request.headers.get("user-agent", "")))
    return response


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    raw = request.cookies.get(config.SESSION_COOKIE)
    if raw:
        auth.revoke_session(raw)
    response = redirect("/login", "Вы вышли.")
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return response


# ─────────── вход на новом устройстве: не сброс пароля, а перенос сессии ───────────


@router.post("/devices/code")
def issue_device_code(request: Request, csrf: str = Form("")):
    auth.check_csrf(request, csrf)
    user = auth.require_user(request)
    code = security.numeric_code(6)
    execute("INSERT INTO device_code (user_id, source_session_id, code_hash, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (user["id"], user["session_id"], security.digest(code),
             security.expires_in(seconds=config.DEVICE_CODE_TTL_SECONDS)))
    return render(request, "device_code.html", code=code,
                  ttl=config.DEVICE_CODE_TTL_SECONDS // 60)


@router.get("/login/device", response_class=HTMLResponse)
def device_login_form(request: Request):
    return render(request, "device_login.html")


@router.post("/login/device")
def device_login(request: Request, csrf: str = Form(""), login: str = Form(""),
                 code: str = Form("")):
    auth.check_csrf(request, csrf)
    user = repo.user_by_login(security.normalize_login(login))
    if user is None:
        return render(request, "device_login.html", error="Не сходится логин или код.")

    row = query_one(
        "SELECT * FROM device_code WHERE user_id = ? AND used_at IS NULL"
        " ORDER BY id DESC LIMIT 1", (user["id"],))
    if row is None or security.is_expired(row["expires_at"]):
        return render(request, "device_login.html", error="Код просрочен — выпустите новый.")
    if row["attempts"] >= config.DEVICE_CODE_ATTEMPTS:
        return render(request, "device_login.html", error="Слишком много попыток, код сгорел.")
    if not security.constant_eq(row["code_hash"], security.digest(code.strip())):
        execute("UPDATE device_code SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        return render(request, "device_login.html", error="Не сходится логин или код.")

    execute("UPDATE device_code SET used_at = ? WHERE id = ?", (now(), row["id"]))
    audit(user["id"], "login_device_code", "user", user["id"])
    response = redirect("/profile", "Вы вошли. Смените пароль, если забыли его.")
    auth.set_session_cookie(response, auth.create_session(user["id"], request.headers.get("user-agent", "")))
    return response


# ─────────────────────────── поддержка ───────────────────────────


@router.get("/support", response_class=HTMLResponse)
def support_form(request: Request):
    return render(request, "support.html", telegram=settings_store.get("support_telegram"))


@router.post("/support")
def support_create(request: Request, csrf: str = Form(""), type_: str = Form("other"),
                   claimed_name: str = Form(""), message: str = Form("")):
    auth.check_csrf(request, csrf)
    if security.rate_limit_hit(client_key(request), "support"):
        return render(request, "support.html", telegram=settings_store.get("support_telegram"),
                      error="Слишком часто. Попробуйте позже.")
    user = auth.current_user(request)
    code = security.support_code()
    execute("INSERT INTO support_request (code, user_id, claimed_name, type, message,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, user["id"] if user else None, claimed_name.strip(),
             type_ if type_ in ("password", "name_conflict", "other") else "other",
             message.strip()[:1000], now()))
    return render(request, "support_created.html", code=code,
                  telegram=settings_store.get("support_telegram"))


@router.get("/login/reset", response_class=HTMLResponse)
def reset_form(request: Request):
    return render(request, "reset_login.html")


@router.post("/login/reset")
def reset_login(request: Request, csrf: str = Form(""), login: str = Form(""),
                code: str = Form(""), password: str = Form(""), password2: str = Form("")):
    """Вход по одноразовому коду, который сисадмин выдал лично."""
    auth.check_csrf(request, csrf)
    if len(password) < 6 or password != password2:
        return render(request, "reset_login.html", error="Пароль короткий или не совпадает.")
    user = repo.user_by_login(security.normalize_login(login))
    if user is None:
        return render(request, "reset_login.html", error="Не сходится логин или код.")
    row = query_one("SELECT * FROM reset_code WHERE user_id = ? AND used_at IS NULL"
                    " ORDER BY id DESC LIMIT 1", (user["id"],))
    if row is None or security.is_expired(row["expires_at"]) or \
            not security.constant_eq(row["code_hash"], security.digest(code.strip().upper())):
        return render(request, "reset_login.html", error="Не сходится логин или код.")

    execute("UPDATE reset_code SET used_at = ? WHERE id = ?", (now(), row["id"]))
    execute("UPDATE user SET password_hash = ? WHERE id = ?",
            (security.hash_password(password), user["id"]))
    execute("UPDATE session SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now(), user["id"]))
    execute("UPDATE support_request SET status = 'resolved', resolved_at = ?"
            " WHERE user_id = ? AND type = 'password' AND status = 'open'", (now(), user["id"]))
    audit(user["id"], "password_reset_used", "user", user["id"])
    response = redirect("/", "Пароль изменён.")
    auth.set_session_cookie(response, auth.create_session(user["id"], request.headers.get("user-agent", "")))
    return response
