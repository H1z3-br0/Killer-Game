"""Киллер — внутренний сервис для игры в локальной сети организации."""
from __future__ import annotations

import asyncio
import ipaddress
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, scheduler, settings_store
from .routes import admin_game, auth_routes, games, sysadmin
from .web import render

STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    applied = db.migrate()
    if applied:
        print(f"[киллер] применены миграции: {', '.join(applied)}")
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(scheduler.run_forever())
    yield
    task.cancel()


app = FastAPI(title="Киллер", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")),
          name="static")

app.include_router(auth_routes.router)
app.include_router(games.router)
app.include_router(admin_game.router)
app.include_router(sysadmin.router)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Ограничение по подсети, счётчик обращений и журнал ошибок."""
    path = request.url.path
    if not path.startswith(("/static", "/healthz")):
        allow = settings_store.get("subnet_allowlist")
        if allow:
            host = request.client.host if request.client else ""
            try:
                address = ipaddress.ip_address(host)
                inside = any(
                    address in ipaddress.ip_network(net.strip(), strict=False)
                    for net in allow.split(",") if net.strip())
            except ValueError:
                inside = True  # некорректная настройка не должна запирать сервис
            if not inside:
                return JSONResponse(
                    {"detail": "Сервис доступен только из рабочей сети."}, status_code=403)
    try:
        response = await call_next(request)
    except Exception:
        scheduler.log_error(path, traceback.format_exc())
        raise
    if not path.startswith("/static"):
        try:
            db.execute("INSERT INTO request_stat (day, hits) VALUES (?, 1)"
                       " ON CONFLICT(day) DO UPDATE SET hits = hits + 1", (db.now()[:10],))
        except Exception:  # noqa: S110 — статистика не важнее ответа
            pass
    return response


@app.exception_handler(404)
async def not_found(request: Request, exc) -> HTMLResponse:
    return render(request, "error.html", status_code=404, code=404,
                  message="Такой страницы нет.")


@app.exception_handler(403)
async def forbidden(request: Request, exc) -> HTMLResponse:
    detail = getattr(exc, "detail", "Доступ закрыт.")
    return render(request, "error.html", status_code=403, code=403, message=detail)


@app.exception_handler(303)
async def see_other(request: Request, exc) -> RedirectResponse:
    location = getattr(exc, "headers", {}).get("Location", "/")
    return RedirectResponse(location, status_code=303)


@app.exception_handler(500)
async def server_error(request: Request, exc) -> HTMLResponse:
    scheduler.log_error(request.url.path, traceback.format_exc())
    return render(request, "error.html", status_code=500, code=500,
                  message="Что-то сломалось. Сисадмин увидит это в журнале ошибок.")


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "uptime_seconds": int(time.time() - STARTED_AT)})
