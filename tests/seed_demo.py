"""Демо-данные для визуальной проверки: игра в разгаре."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DEMO = Path(os.environ.get("DEMO_DB", "/tmp/killer_demo.db"))
DEMO.unlink(missing_ok=True)
os.environ["KILLER_DB"] = str(DEMO)

from app import config, db, game_logic, repo, security  # noqa: E402
db.migrate()

people = [("Ведущий", "Игорь", "igor"), ("Соколова", "Анна", "anna"),
          ("Дорн", "Максим", "mdorn"), ("Хайруллина", "Лейла", "leila"),
          ("Ивашов", "Пётр", "petr"), ("Абрамова", "Дина", "dina"),
          ("Валиев", "Тимур", "timur")]
for i, (last, first, login) in enumerate(people):
    db.execute("INSERT INTO user (login, last_name, first_name, name_normalized,"
               " password_hash, role, avatar_emoji, department, created_at, last_seen_at)"
               " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (login, last, first, security.normalize_name(last, first),
                security.hash_password("secret123"),
                "sysadmin" if i == 0 else "user", "🕵", db.now(), db.now()))

db.execute("INSERT INTO game (title, description, status, visibility, color, admin_user_id,"
           " rules_json, created_at) VALUES (?, ?, 'recruiting', 'open', ?, 1, ?, ?)",
           ("Осенний отстрел", "Игра на неделю для всего этажа.", "#D9A441",
            '{"weapon": "наклейка на спине", "safe_zones": "столовая, переговорные",'
            ' "quiet_from": "19:00", "quiet_to": "09:00", "no_weekends": true}', db.now()))
for uid in range(2, 8):
    u = db.query_one("SELECT * FROM user WHERE id = ?", (uid,))
    db.execute("INSERT INTO participant (game_id, user_id, display_name_snapshot, status,"
               " invited_at, joined_at) VALUES (1, ?, ?, 'joined', ?, ?)",
               (uid, repo.display_name(u), db.now(), db.now()))

with db.transaction() as conn:
    game_logic.start_game(conn, 1, 1)
# одно устранение уже произошло
with db.transaction() as conn:
    victim = conn.execute("SELECT * FROM participant WHERE game_id = 1 AND status = 'alive'"
                          " LIMIT 1").fetchone()
    hunter = game_logic.hunter_of(conn, victim["id"])
    game_logic.eliminate(conn, 1, victim["id"], "kill_confirmed", killer_id=hunter["id"])

db.execute("INSERT INTO game (title, description, status, visibility, color,"
           " admin_user_id, rules_json, created_at)"
           " VALUES (?, ?, 'recruiting', 'private', ?, ?, ?, ?)",
           ("Тихий отдел", "Закрытая игра для своих.", "#5FA88B", 2, "{}", db.now()))

print(DEMO)
