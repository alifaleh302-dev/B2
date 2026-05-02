"""
Persistence layer backed by either Turso (cloud SQLite) or local sqlite3.

Tables:
  • accounts       — email/password + JWT tokens per account
  • events         — cached events seen on Webook
  • watch_keywords — keywords that trigger auto-alerts
  • bookings       — successful bookings (one row per account per booking)
  • sniper_tasks   — active sniper tasks (user-triggered fast monitoring)
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.core.db import connect as _conn


def init_db() -> None:
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id              TEXT PRIMARY KEY,
            label           TEXT,
            email           TEXT NOT NULL,
            password        TEXT NOT NULL,
            access_token    TEXT,
            refresh_token   TEXT,
            token_expires_at REAL DEFAULT 0,
            user_id         TEXT,
            status          TEXT DEFAULT 'new',   -- new/ready/refreshing/blocked/needs_relogin
            last_used_at    REAL DEFAULT 0,
            tickets_booked  INTEGER DEFAULT 0,
            last_error      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS events (
            slug            TEXT PRIMARY KEY,
            title           TEXT,
            category        TEXT,
            city            TEXT,
            url             TEXT,
            start_date      INTEGER,
            is_seated       INTEGER DEFAULT 0,
            poster          TEXT,
            tickets_json    TEXT,
            first_seen_at   REAL,
            last_seen_at    REAL,
            last_checked_at REAL
        );

        CREATE TABLE IF NOT EXISTS watch_keywords (
            keyword       TEXT PRIMARY KEY,
            added_by      TEXT,
            added_at      REAL
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id         TEXT,
            event_slug      TEXT,
            event_title     TEXT,
            ticket_type     TEXT,
            account_id      TEXT,
            quantity        INTEGER,
            seat_info       TEXT,     -- JSON
            payment_url     TEXT,
            total_amount    REAL,
            currency        TEXT,
            status          TEXT,     -- pending/paid/cancelled/expired
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS sniper_tasks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id         TEXT,
            event_slug      TEXT,
            ticket_type_id  TEXT,
            quantity        INTEGER,
            status          TEXT DEFAULT 'active',
            attempts        INTEGER DEFAULT 0,
            created_at      REAL,
            updated_at      REAL
        );

        CREATE INDEX IF NOT EXISTS idx_events_last_seen  ON events(last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_events_start_date ON events(start_date);
        CREATE INDEX IF NOT EXISTS idx_accounts_status   ON accounts(status);
        CREATE INDEX IF NOT EXISTS idx_snipers_status    ON sniper_tasks(status);
        """)


# ════════════════════════════════════════════════════════════════════════
# Accounts
# ════════════════════════════════════════════════════════════════════════
def upsert_account(account_id: str, email: str, password: str,
                   label: str = "") -> None:
    with _conn() as con:
        con.execute("""
            INSERT INTO accounts (id, label, email, password, status, created_at)
            VALUES (?, ?, ?, ?, 'new', ?)
            ON CONFLICT(id) DO UPDATE SET
              label = excluded.label,
              email = excluded.email,
              password = excluded.password
        """, (account_id, label or email.split("@")[0], email, password, time.time()))


def save_tokens(account_id: str, access: str, refresh: str,
                expires_at: float, user_id: Optional[str] = None) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE accounts
            SET access_token = ?, refresh_token = ?, token_expires_at = ?,
                user_id = COALESCE(?, user_id), status = 'ready',
                last_error = NULL
            WHERE id = ?
        """, (access, refresh, expires_at, user_id, account_id))


def set_account_status(account_id: str, status: str,
                       error: Optional[str] = None) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE accounts SET status = ?, last_error = ? WHERE id = ?",
            (status, error, account_id),
        )


def mark_account_used(account_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE accounts SET last_used_at = ?, tickets_booked = tickets_booked + 1 "
            "WHERE id = ?",
            (time.time(), account_id),
        )


def get_account(account_id: str) -> Optional[dict[str, Any]]:
    with _conn() as con:
        r = con.execute("SELECT * FROM accounts WHERE id = ?",
                        (account_id,)).fetchone()
        return dict(r) if r else None


def list_accounts(status: Optional[str] = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM accounts"
    params: list[Any] = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY created_at ASC"
    with _conn() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]


def delete_account(account_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


# ════════════════════════════════════════════════════════════════════════
# Events
# ════════════════════════════════════════════════════════════════════════
def upsert_event(slug: str, data: dict[str, Any]) -> bool:
    """Returns True if this is a brand-new slug we hadn't seen before."""
    now = time.time()
    with _conn() as con:
        cur = con.execute("SELECT 1 FROM events WHERE slug = ?", (slug,)).fetchone()
        is_new = cur is None
        con.execute("""
            INSERT INTO events (slug, title, category, city, url, start_date,
                                is_seated, poster, tickets_json,
                                first_seen_at, last_seen_at, last_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
              title = excluded.title,
              category = excluded.category,
              city = excluded.city,
              url = excluded.url,
              start_date = excluded.start_date,
              is_seated = excluded.is_seated,
              poster = excluded.poster,
              tickets_json = excluded.tickets_json,
              last_seen_at = excluded.last_seen_at,
              last_checked_at = excluded.last_checked_at
        """, (
            slug,
            data.get("title"),
            data.get("category"),
            data.get("city"),
            data.get("url"),
            data.get("start_date"),
            1 if data.get("is_seated") else 0,
            data.get("poster"),
            json.dumps(data.get("tickets") or [], ensure_ascii=False),
            now, now, now,
        ))
        return is_new


def get_event(slug: str) -> Optional[dict[str, Any]]:
    with _conn() as con:
        r = con.execute("SELECT * FROM events WHERE slug = ?",
                        (slug,)).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["tickets"] = json.loads(d.get("tickets_json") or "[]")
        except Exception:
            d["tickets"] = []
        return d


def list_recent_events(limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM events ORDER BY last_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════
# Watch keywords
# ════════════════════════════════════════════════════════════════════════
def add_keyword(keyword: str, added_by: str = "system") -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO watch_keywords (keyword, added_by, added_at) "
            "VALUES (?, ?, ?)",
            (keyword.lower().strip(), added_by, time.time()),
        )


def remove_keyword(keyword: str) -> None:
    with _conn() as con:
        con.execute(
            "DELETE FROM watch_keywords WHERE keyword = ?",
            (keyword.lower().strip(),),
        )


def list_keywords() -> list[str]:
    with _conn() as con:
        return [r["keyword"] for r in con.execute(
            "SELECT keyword FROM watch_keywords ORDER BY added_at"
        ).fetchall()]


# ════════════════════════════════════════════════════════════════════════
# Bookings
# ════════════════════════════════════════════════════════════════════════
def add_booking(chat_id: str, event_slug: str, event_title: str,
                ticket_type: str, account_id: str, quantity: int,
                seat_info: dict, payment_url: str,
                total_amount: float = 0.0, currency: str = "SAR",
                status: str = "pending") -> int:
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO bookings (chat_id, event_slug, event_title, ticket_type,
                                  account_id, quantity, seat_info, payment_url,
                                  total_amount, currency, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, event_slug, event_title, ticket_type, account_id,
              quantity, json.dumps(seat_info, ensure_ascii=False),
              payment_url, total_amount, currency, status, time.time()))
        return cur.lastrowid


def list_bookings(chat_id: Optional[str] = None,
                  limit: int = 20) -> list[dict[str, Any]]:
    q = "SELECT * FROM bookings"
    params: list[Any] = []
    if chat_id:
        q += " WHERE chat_id = ?"
        params.append(chat_id)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as con:
        rows = con.execute(q, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["seat_info"] = json.loads(d.get("seat_info") or "{}")
            except Exception:
                d["seat_info"] = {}
            out.append(d)
        return out


# ════════════════════════════════════════════════════════════════════════
# Sniper tasks
# ════════════════════════════════════════════════════════════════════════
def add_sniper(chat_id: str, event_slug: str, ticket_type_id: str,
               quantity: int) -> int:
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO sniper_tasks (chat_id, event_slug, ticket_type_id,
                                      quantity, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (chat_id, event_slug, ticket_type_id, quantity,
              time.time(), time.time()))
        return cur.lastrowid


def list_snipers(status: Optional[str] = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM sniper_tasks"
    params: list[Any] = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY created_at DESC"
    with _conn() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]


def set_sniper_status(task_id: int, status: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE sniper_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), task_id),
        )


# Initialize on import so any module that imports us gets a ready DB
init_db()
