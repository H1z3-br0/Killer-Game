#!/usr/bin/env python3
"""Схема должна применяться на чистой базе и не применяться повторно."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["KILLER_DB"] = str(Path(tempfile.mkdtemp()) / "schema-check.db")

from app import config, db

config.DB_PATH = Path(os.environ["KILLER_DB"])
db.reset_connection()

applied = db.migrate()
assert applied, "миграции не применились на чистой базе"
assert db.migrate() == [], "миграция применилась повторно"

tables = db.query("SELECT name FROM sqlite_master WHERE type = 'table'"
                  " AND name NOT LIKE 'sqlite_%'")
assert len(tables) >= 15, f"таблиц меньше ожидаемого: {len(tables)}"

broken = db.query("PRAGMA foreign_key_check")
assert not broken, f"нарушены внешние ключи: {broken}"

print(f"схема в порядке: {', '.join(applied)}, таблиц {len(tables)}")
