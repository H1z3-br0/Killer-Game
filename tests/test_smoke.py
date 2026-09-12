"""Каждая страница обязана открываться — битый шаблон ловится здесь."""
from __future__ import annotations

import pytest
from conftest import csrf

from app import db

GUEST = ["/", "/rules", "/login", "/register", "/login/device", "/login/reset", "/support"]
PLAYER = ["/", "/rules", "/catalog", "/people", "/hall", "/profile", "/notifications"]
SYSADMIN = ["/admin", "/admin/users", "/admin/support", "/admin/games", "/admin/settings",
            "/admin/backups", "/admin/audit", "/admin/integrity", "/admin/duplicates",
            "/admin/maintenance"]


@pytest.mark.parametrize("url", GUEST)
def test_guest_pages(client, url):
    assert client.get(url).status_code == 200


@pytest.mark.parametrize("url", PLAYER)
def test_player_pages(people, url):
    assert people["anna"].get(url).status_code == 200


@pytest.mark.parametrize("url", SYSADMIN)
def test_sysadmin_pages(people, url):
    assert people["boss"].get(url).status_code == 200


def test_game_pages_for_every_role(game_factory, people):
    gid = game_factory(start=True)
    assert people["anna"].get(f"/games/{gid}").status_code == 200
    assert people["boss"].get(f"/manage/games/{gid}").status_code == 200
    assert people["boss"].get(f"/admin/games/{gid}").status_code == 200
    uid = db.query_one("SELECT id FROM user WHERE login = 'anna'")["id"]
    assert people["boss"].get(f"/admin/users/{uid}").status_code == 200
    assert people["anna"].get(f"/people/{uid}").status_code == 200


def test_missing_pages_give_404(people):
    assert people["anna"].get("/games/9999").status_code == 404
    assert people["anna"].get("/такой-страницы-нет").status_code == 404
    assert people["boss"].get("/admin/users/9999").status_code == 404


def test_healthz_reports_uptime(client):
    data = client.get("/healthz").json()
    assert data["status"] == "ok" and data["uptime_seconds"] >= 0


def test_csv_export_has_bom_and_rows(game_factory, people):
    gid = game_factory(start=True)
    people["boss"].post(f"/manage/games/{gid}/finish", data={"csrf": csrf(people["boss"])})
    r = people["anna"].get(f"/games/{gid}/export.csv")
    assert r.status_code == 200
    assert r.text.startswith("﻿"), "без BOM Excel сломает кириллицу"
    assert "участник,статус,устранений,место" in r.text


def test_csv_export_closed_before_finish(game_factory, people):
    gid = game_factory(start=True)
    assert people["anna"].get(f"/games/{gid}/export.csv").status_code == 403


def test_game_state_endpoint(game_factory, people):
    gid = game_factory(start=True)
    data = people["anna"].get(f"/games/{gid}/state").json()
    assert data["alive"] == 4 and data["status"] == "running"
    assert "target" not in data, "состояние не должно раскрывать цель"


def test_subnet_guard(client, monkeypatch):
    from fastapi.testclient import TestClient

    from app import settings_store
    from app.main import app

    settings_store.set_value("subnet_allowlist", "10.99.0.0/24")
    outside = TestClient(app, client=("192.168.5.7", 5000), follow_redirects=True)
    inside = TestClient(app, client=("10.99.0.15", 5000), follow_redirects=True)
    assert outside.get("/login").status_code == 403
    assert inside.get("/login").status_code == 200
