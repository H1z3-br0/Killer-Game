"""Пароли, сессии, CSRF и ограничение частоты.

Сервис живёт в локальной сети по http://, поэтому защищённый контекст
браузера недоступен: вся криптография — на сервере.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from . import config
from .db import execute, now, query_one

_hasher = PasswordHasher()

# Алфавит без пар-двойников: 0/O и 1/I/l не путаются при диктовке.
SAFE_ALPHABET = "ACDEFGHJKMNPQRTUVWXYZ2346789"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, Exception):
        return False


def token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def digest(value: str) -> str:
    """Одноразовые коды и токены сессий хранятся только в виде хеша."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def numeric_code(digits: int = 6) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(digits))


def safe_code(length: int = 8) -> str:
    return "".join(secrets.choice(SAFE_ALPHABET) for _ in range(length))


def support_code() -> str:
    return "SR-" + "".join(secrets.choice(string.digits) for _ in range(4))


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


def normalize_name(*parts: str) -> str:
    """ФИО как логин: регистр, лишние пробелы, «ё» и дефисы не должны
    порождать двух разных Ивановых."""
    joined = " ".join(p.strip() for p in parts if p and p.strip())
    joined = " ".join(joined.split())
    return joined.lower().replace("ё", "е").replace("-", " ").replace("  ", " ")


LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,19}$")
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def normalize_login(value: str) -> str:
    """Логин регистронезависим: Petrov и petrov — один и тот же человек."""
    return (value or "").strip().lower()


def login_is_valid(value: str) -> bool:
    return bool(LOGIN_RE.match(normalize_login(value)))


def rate_limit_hit(bucket: str, kind: str) -> bool:
    """True — лимит исчерпан, запрос выполнять нельзя."""
    limit, window = config.RATE_LIMITS.get(kind, (60, 60))
    since = (datetime.now(timezone.utc) - timedelta(seconds=window)).replace(
        microsecond=0).isoformat()
    key = f"{kind}:{bucket}"
    row = query_one(
        "SELECT COUNT(*) AS n FROM rate_hit WHERE bucket = ? AND created_at >= ?",
        (key, since))
    if row and row["n"] >= limit:
        return True
    execute("INSERT INTO rate_hit (bucket, created_at) VALUES (?, ?)", (key, now()))
    return False


def expires_in(**kwargs) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).replace(
        microsecond=0).isoformat()


def is_expired(iso_ts: str) -> bool:
    return iso_ts < now()
