"""Общие помощники веб-слоя: шаблоны, рендер, флеш-сообщения."""
from __future__ import annotations

from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, config, settings_store
from .db import query_one

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
templates.env.filters["from_json"] = lambda raw: __import__("json").loads(raw or "{}")


def flash(response: RedirectResponse, message: str, kind: str = "ok") -> RedirectResponse:
    """Однократное сообщение после редиректа — живёт до следующего показа.

    Значение кодируем процентами: в cookie допустим только latin-1, а тексты у
    нас русские.
    """
    response.set_cookie("flash", quote(f"{kind}|{message}"), max_age=10, path="/")
    return response


def render(request: Request, template: str, status_code: int = 200,
           **ctx) -> HTMLResponse:
    csrf = auth.ensure_csrf(request)
    user = ctx.pop("user", None)
    if user is None:
        user = auth.current_user(request)
    unread = 0
    if user is not None:
        row = query_one("SELECT COUNT(*) AS n FROM notification WHERE user_id = ?"
                        " AND read_at IS NULL", (user["id"],))
        unread = row["n"] if row else 0
    raw_flash = request.cookies.get("flash", "")
    flash_kind, _, flash_text = unquote(raw_flash).partition("|")
    response = templates.TemplateResponse(request, template, {
        "csrf": csrf,
        "user": user,
        "unread": unread,
        "settings": settings_store.all_settings(),
        "flash": {"kind": flash_kind, "text": flash_text} if flash_text else None,
        **ctx,
    }, status_code=status_code)
    if getattr(request.state, "new_csrf", None):
        response.set_cookie(config.CSRF_COOKIE, request.state.new_csrf,
                            httponly=True, samesite="lax", max_age=30 * 86400, path="/")
    if raw_flash:
        response.delete_cookie("flash", path="/")
    return response


def redirect(url: str, message: str = "", kind: str = "ok") -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if message:
        flash(response, message, kind)
    return response
