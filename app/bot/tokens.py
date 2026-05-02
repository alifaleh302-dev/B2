"""
Short opaque token store for callback_data.

Telegram limits callback_data to 64 bytes. A Webook slug + Mongo ObjectId
easily exceeds that. We map them to 8-char tokens held in-memory.
"""
from __future__ import annotations

import secrets
from typing import Any

_store: dict[str, dict[str, Any]] = {}


def put(data: dict[str, Any]) -> str:
    """Store a dict, return an 8-char token."""
    while True:
        tok = secrets.token_urlsafe(6)[:8]
        if tok not in _store:
            _store[tok] = data
            return tok


def get(tok: str) -> dict[str, Any] | None:
    return _store.get(tok)


def gc(max_size: int = 5000) -> None:
    """Trim the store if it grows too large (keep newest ~5000)."""
    if len(_store) > max_size:
        # Simple FIFO trim — remove oldest 20%
        to_drop = len(_store) - int(max_size * 0.8)
        for k in list(_store.keys())[:to_drop]:
            _store.pop(k, None)
