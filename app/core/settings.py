"""
Runtime settings persisted in the database (PostgreSQL / SQLite).

Allows the admin web UI to set / update values (e.g. TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID, WEBOOK_PUBLIC_TOKEN) WITHOUT restarting the service
or touching Render's env vars (which lose values on the "replace all"
API).

Resolution order used everywhere in the code:
    1. os.environ  — if set at process boot, takes priority
    2. DB value    — set via the /admin web UI
    3. default     — hard-coded fallback

Values are cached in memory for 10 s to avoid hammering the DB.
"""
from __future__ import annotations

import logging
import os
import time
from threading import RLock
from typing import Any

from app.core.db import connect

log = logging.getLogger("settings")

_lock = RLock()
_cache: dict[str, Any] = {}
_cache_stamp: float = 0.0
_CACHE_TTL = 10.0


def _init_table() -> None:
    try:
        with connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key         TEXT PRIMARY KEY,
                value       TEXT,
                updated_at  DOUBLE PRECISION
            );
            """)
    except Exception as e:
        log.error(f"settings table init failed: {e}")


_init_table()


def _refresh_cache() -> None:
    global _cache_stamp
    try:
        rows = {}
        with connect() as con:
            for r in con.execute("SELECT key, value FROM settings").fetchall():
                rows[r["key"]] = r["value"]
        with _lock:
            _cache.clear()
            _cache.update(rows)
            _cache_stamp = time.time()
    except Exception as e:
        log.debug(f"settings cache refresh err: {e}")


def get(key: str, default: str = "") -> str:
    """Read a setting: env → DB → default."""
    env_val = os.environ.get(key)
    if env_val:
        return env_val

    # cache
    if time.time() - _cache_stamp > _CACHE_TTL:
        _refresh_cache()

    with _lock:
        v = _cache.get(key)
    return v if v not in (None, "") else default


def set_value(key: str, value: str) -> None:
    """Upsert a setting and invalidate cache."""
    with connect() as con:
        con.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                             updated_at = excluded.updated_at
        """, (key, value, time.time()))
    global _cache_stamp
    _cache_stamp = 0.0


def delete(key: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM settings WHERE key = ?", (key,))
    global _cache_stamp
    _cache_stamp = 0.0


def list_all() -> dict[str, str]:
    """Return all keys (DB only, for admin UI; env wins on read)."""
    _refresh_cache()
    with _lock:
        return dict(_cache)


# ════════════════════════════════════════════════════════════════════════
# Known well-typed getters
# ════════════════════════════════════════════════════════════════════════
def telegram_bot_token() -> str:
    return get("TELEGRAM_BOT_TOKEN", "")


def telegram_chat_id() -> str:
    return get("TELEGRAM_CHAT_ID", "")


def authorized_chat_ids() -> list[str]:
    raw = get("AUTHORIZED_CHAT_IDS", "")
    ids = [c.strip() for c in raw.split(",") if c.strip()]
    main = telegram_chat_id()
    if main and main not in ids:
        ids.append(main)
    return ids


def webook_public_token() -> str:
    return get(
        "WEBOOK_PUBLIC_TOKEN",
        "e9aac1f2f0b6c07d6be070ed14829de684264278359148d6a582ca65a50934d2",
    )


def admin_password() -> str:
    """Password used to open the /admin UI. Change from UI or env."""
    return get("ADMIN_PASSWORD", "webook-admin")


# Fallbacks for PostgreSQL url so we can still bootstrap
def database_url() -> str:
    return get("DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
