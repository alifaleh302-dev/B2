"""
Captcha relay: Playwright grabs a screenshot → sends it to the user via Telegram
→ user replies with the 6-char text / token (or clicks an inline button to
"refresh" the captcha) → Playwright consumes the answer.

The broker is an in-memory dict keyed by (chat_id, account_id). Each entry holds
an asyncio.Future that the login flow awaits.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("captcha")


@dataclass
class CaptchaRequest:
    chat_id: str
    account_id: str
    message_id: Optional[int] = None
    future: asyncio.Future = field(default_factory=asyncio.Future)

    def key(self) -> str:
        return f"{self.chat_id}:{self.account_id}"


_pending: dict[str, CaptchaRequest] = {}


def create(chat_id: str, account_id: str) -> CaptchaRequest:
    req = CaptchaRequest(chat_id=chat_id, account_id=account_id)
    _pending[req.key()] = req
    log.info(f"🧩 captcha requested: {req.key()}")
    return req


def resolve(chat_id: str, account_id: str, answer: str) -> bool:
    key = f"{chat_id}:{account_id}"
    req = _pending.get(key)
    if not req or req.future.done():
        return False
    req.future.set_result(answer.strip())
    _pending.pop(key, None)
    log.info(f"✅ captcha resolved: {key}")
    return True


def cancel(chat_id: str, account_id: str) -> None:
    key = f"{chat_id}:{account_id}"
    req = _pending.pop(key, None)
    if req and not req.future.done():
        req.future.cancel()


def latest_for_chat(chat_id: str) -> Optional[CaptchaRequest]:
    """Return the newest pending captcha for a given chat (for quick reply)."""
    for req in reversed(list(_pending.values())):
        if req.chat_id == chat_id and not req.future.done():
            return req
    return None
