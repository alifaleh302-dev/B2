"""Thin async Telegram Bot API wrapper."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import aiohttp

from app.core.config import telegram_bot_token as _bot_token

log = logging.getLogger("tg")


class Notifier:
    def __init__(self, token: str | None = None) -> None:
        # Lazy-resolve from env/DB so updates via /admin take effect immediately.
        self._override = token

    @property
    def token(self) -> str:
        return self._override or _bot_token()

    @property
    def base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    # ── Low-level ───────────────────────────────────────────────────
    async def _call(self, method: str, payload: dict,
                    retries: int = 2) -> Optional[dict]:
        if not self.token:
            return None
        url = f"{self.base}/{method}"
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as r:
                        data = await r.json(content_type=None)
                        if data.get("ok"):
                            return data
                        log.warning(f"TG {method} error: {data}")
            except Exception as e:
                log.debug(f"TG {method} attempt {attempt+1}: {e}")
                await asyncio.sleep(1 + attempt)
        return None

    # ── High-level ──────────────────────────────────────────────────
    async def send(self, chat_id, text: str, *, reply_markup: Any = None,
                   parse_mode: str = "HTML",
                   disable_preview: bool = True) -> Optional[dict]:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)

    async def edit(self, chat_id, message_id: int, text: str, *,
                   reply_markup: Any = None,
                   parse_mode: str = "HTML") -> Optional[dict]:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("editMessageText", payload)

    async def answer_cb(self, callback_query_id: str, text: str = "",
                        show_alert: bool = False) -> None:
        await self._call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        })

    async def send_photo(self, chat_id, image_path: str, *,
                         caption: str = "",
                         parse_mode: str = "HTML") -> Optional[dict]:
        if not self.token or not os.path.exists(image_path):
            return None
        url = f"{self.base}/sendPhoto"
        with aiohttp.MultipartWriter("form-data") as mp:
            p1 = mp.append(str(chat_id)); p1.set_content_disposition(
                "form-data", name="chat_id")
            p2 = mp.append(caption); p2.set_content_disposition(
                "form-data", name="caption")
            p3 = mp.append(parse_mode); p3.set_content_disposition(
                "form-data", name="parse_mode")
            f = open(image_path, "rb")
            try:
                p4 = mp.append(f, {"Content-Type": "image/png"})
                p4.set_content_disposition(
                    "form-data", name="photo",
                    filename=os.path.basename(image_path))
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        url, data=mp,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as r:
                        data = await r.json(content_type=None)
                        if not data.get("ok"):
                            log.warning(f"sendPhoto error: {data}")
                        return data
            finally:
                f.close()

    # ── Webhook management ──────────────────────────────────────────
    async def set_webhook(self, url: str) -> bool:
        r = await self._call("setWebhook", {
            "url": url,
            "allowed_updates": ["message", "callback_query"],
        })
        return bool(r and r.get("ok"))

    async def delete_webhook(self) -> bool:
        r = await self._call("deleteWebhook",
                             {"drop_pending_updates": True})
        return bool(r and r.get("ok"))

    async def get_updates(self, offset: Optional[int] = None,
                          timeout: int = 25) -> Optional[dict]:
        params: dict[str, Any] = {"timeout": timeout,
                                   "allowed_updates":
                                       json.dumps(["message",
                                                   "callback_query"])}
        if offset is not None:
            params["offset"] = offset
        if not self.token:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self.base}/getUpdates", params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout + 5),
                ) as r:
                    return await r.json(content_type=None)
        except Exception as e:
            log.debug(f"getUpdates err: {e}")
            return None
