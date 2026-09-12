"""Сессии, текущий пользователь, CSRF и права доступа."""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from . import config, security
from .db import execute, now, query_one


def create_session(user_id: int, user_agent: str = "") -> str:
    raw = security.token()
    execute(
        "INSERT INTO session (id, user_id, user_agent, created_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (security.digest(raw), user_id, user_agent[:200], now(), now()))
    return raw


def revoke_session(raw_token: str) -> None:
    execute("UPDATE session SET revoked_at = ? WHERE id = ?",
            (now(), security.digest(raw_token)))


def current_user(request: Request) -> sqlite3.Row | None:
    raw = request.cookies.get(config.SESSION_COOKIE)
    if not raw:
        return None
    row = query_one(
        "SELECT u.*, s.id AS session_id FROM session s JOIN user u ON u.id = s.user_id"
        " WHERE s.id = ? AND s.revoked_at IS NULL", (security.digest(raw),))
    if row is None or row["status"] == "blocked":
        return None
    execute("UPDATE session SET last_seen_at = ? WHERE id = ?", (now(), row["session_id"]))
    execute("UPDATE user SET last_seen_at = ? WHERE id = ?", (now(), row["id"]))
    return row


def require_user(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER,
                            headers={"Location": "/login"})
    return user


def require_sysadmin(request: Request) -> sqlite3.Row:
    user = require_user(request)
    if user["role"] != "sysadmin":
        raise HTTPException(status_code=403, detail="Нужны права сисадмина")
    return user


def set_session_cookie(response: RedirectResponse, raw_token: str) -> None:
    response.set_cookie(
        config.SESSION_COOKIE, raw_token, httponly=True, samesite="lax",
        max_age=config.SESSION_DAYS * 86400, path="/")


def ensure_csrf(request: Request) -> str:
    token = request.cookies.get(config.CSRF_COOKIE)
    if not token:
        token = security.token(16)
        request.state.new_csrf = token
    request.state.csrf = token
    return token


def check_csrf(request: Request, submitted: str) -> None:
    """Cookie браузер приложит к любому запросу, в том числе присланному чужой
    страницей; токен в форме чужая страница не знает."""
    cookie = request.cookies.get(config.CSRF_COOKIE, "")
    if not cookie or not security.constant_eq(cookie, submitted):
        raise HTTPException(status_code=403, detail="Форма устарела, обновите страницу")
