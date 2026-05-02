"""
SeatCloud / Seats.io client for Webook seated events.

It provides:
  • public rendering info discovery
  • object status polling
  • hold-token creation
  • adjacent-seat picking with preferred blocks
  • optional WebSocket subscription for fast seat-release tracking
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from app.core.config import WEBOOK_ORIGIN
from app.services.seatsio_token_fetcher import ensure_tokens

log = logging.getLogger("seatsio_client")

SEATCLOUD_API = "https://api.seatcloud.com"


def _headers(hold_token: str = "") -> dict[str, str]:
    h = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": WEBOOK_ORIGIN,
        "referer": f"{WEBOOK_ORIGIN}/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "accept-language": "ar-SA,ar;q=0.9,en;q=0.8",
        "x-client-tool": "chart-renderer",
    }
    if hold_token:
        h["x-hold-token"] = hold_token
    return h


def _deep_find_value(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = _deep_find_value(v, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_value(v, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def _normalize_statuses(payload: Any) -> dict[str, str]:
    raw = payload.get("statuses") if isinstance(payload, dict) else None
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("label") or item.get("objectId") or item.get("id")
            val = item.get("status") or item.get("value") or item.get("state")
            if key and val:
                out[str(key)] = str(val)
        return out
    return {}


def _extract_objects(rendering_info: Any) -> list[dict[str, Any]]:
    if isinstance(rendering_info, dict):
        for key in ("objects", "items", "selectableObjects", "renderableObjects"):
            val = rendering_info.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
        for v in rendering_info.values():
            got = _extract_objects(v)
            if got:
                return got
    elif isinstance(rendering_info, list):
        if rendering_info and isinstance(rendering_info[0], dict):
            if any(("id" in x or "objectId" in x) for x in rendering_info if isinstance(x, dict)):
                return rendering_info
        for v in rendering_info:
            got = _extract_objects(v)
            if got:
                return got
    return []


def _seat_no(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = str(v)
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def pick_adjacent_from_snapshot(
    rendering_info: Any,
    statuses: dict[str, str],
    quantity: int,
    target_blocks: Optional[list[str]] = None,
) -> list[str]:
    target_blocks = [b.strip().lower() for b in (target_blocks or []) if b.strip()]
    objects = _extract_objects(rendering_info)
    free: list[dict[str, Any]] = []

    for obj in objects:
        oid = obj.get("id") or obj.get("objectId")
        labels = obj.get("labels") or {}
        label = (
            _deep_find_value(obj, {"displayedLabel", "label", "labelText"})
            or labels.get("displayedLabel")
            or oid
        )
        status = statuses.get(str(label)) or statuses.get(str(oid)) or "free"
        if str(status).lower() not in {"free", "available", "not_booked"}:
            continue
        category = obj.get("category") or obj.get("categoryKey") or obj.get("ticketType") or ""
        section = labels.get("section") or obj.get("section") or category or ""
        row = labels.get("parent") or obj.get("row") or ""
        seat = labels.get("own") or obj.get("seat") or obj.get("seatNumber") or ""
        section_norm = str(section).strip().lower()
        cat_norm = str(category).strip().lower()
        if target_blocks and section_norm not in target_blocks and cat_norm not in target_blocks:
            continue
        free.append({
            "id": str(oid or label),
            "label": str(label),
            "section": str(section),
            "row": str(row),
            "seat": str(seat),
            "seat_no": _seat_no(seat) or _seat_no(label),
        })

    if len(free) < quantity:
        return []

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in free:
        key = f"{item['section']}|{item['row']}"
        grouped.setdefault(key, []).append(item)

    for arr in grouped.values():
        arr.sort(key=lambda x: (x["seat_no"] is None, x["seat_no"] or 10**9, x["label"]))
        if len(arr) < quantity:
            continue
        for i in range(0, len(arr) - quantity + 1):
            window = arr[i:i + quantity]
            nums = [w["seat_no"] for w in window]
            if all(n is not None for n in nums) and all(nums[j] == nums[j - 1] + 1 for j in range(1, len(nums))):
                return [w["id"] for w in window]
        return [w["id"] for w in arr[:quantity]]

    return [w["id"] for w in free[:quantity]]


class SeatsioClient:
    def __init__(self, event_key: str):
        self.event_key = event_key
        self.workspace_key = ""
        self.session: Optional[aiohttp.ClientSession] = None
        self.hold_token = ""
        self.hold_token_expires = 0.0
        self._ws_task: Optional[asyncio.Task] = None

    async def __aenter__(self):
        tokens = await ensure_tokens()
        self.workspace_key = tokens.get("workspace_key") or ""
        if not self.workspace_key:
            raise RuntimeError("SeatCloud workspace key is not available")
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._ws_task:
            self._ws_task.cancel()
        if self.session:
            await self.session.close()

    async def _get(self, path: str, *, qs: Optional[dict[str, str]] = None, timeout: int = 15) -> Any:
        url = f"{SEATCLOUD_API}{path}"
        if qs:
            url += f"?{urlencode(qs)}"
        async with self.session.get(url, headers=_headers(self.hold_token), timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                return await r.json(content_type=None)
            except Exception:
                return {"raw": await r.text()}

    async def _post(self, path: str, body: dict[str, Any], timeout: int = 15) -> Any:
        url = f"{SEATCLOUD_API}{path}"
        async with self.session.post(url, headers=_headers(self.hold_token), json=body, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                return await r.json(content_type=None)
            except Exception:
                return {"raw": await r.text(), "status": r.status}

    async def rendering_info(self) -> Any:
        return await self._get(
            f"/system/public/{self.workspace_key}/rendering-info",
            qs={"event_key": self.event_key},
            timeout=20,
        )

    async def object_statuses(self) -> dict[str, str]:
        payload = await self._get(
            f"/system/public/{self.workspace_key}/events/object-statuses",
            qs={"event_key": self.event_key},
            timeout=10,
        )
        return _normalize_statuses(payload if isinstance(payload, dict) else {})

    async def init_hold_token(self) -> str:
        payload = await self._post(
            f"/system/public/{self.workspace_key}/hold-tokens",
            {},
            timeout=10,
        )
        token = ""
        if isinstance(payload, dict):
            token = payload.get("holdToken") or payload.get("hold_token") or payload.get("token") or ""
        if not token:
            raise RuntimeError(f"SeatCloud hold-token init failed: {payload}")
        self.hold_token = token
        self.hold_token_expires = time.time() + float((payload or {}).get("expiresInSeconds") or 900)
        return token

    async def hold_objects(self, object_ids: list[str], ticket_type: str = "") -> Any:
        if not self.hold_token:
            await self.init_hold_token()
        body = {
            "holdToken": self.hold_token,
            "objects": [
                ({"objectId": oid, "ticketType": ticket_type} if ticket_type else {"objectId": oid})
                for oid in object_ids
            ],
        }
        return await self._post(
            f"/adapter/sio/system/public/{self.workspace_key}/events/{self.event_key}/actions/hold",
            body,
            timeout=15,
        )

    async def release_objects(self, object_ids: list[str]) -> Any:
        if not self.hold_token:
            return {}
        body = {"holdToken": self.hold_token, "objects": [{"objectId": oid} for oid in object_ids]}
        return await self._post(
            f"/adapter/sio/system/public/{self.workspace_key}/events/{self.event_key}/actions/release",
            body,
            timeout=10,
        )

    async def pick_and_hold_adjacent(
        self,
        quantity: int,
        *,
        target_blocks: Optional[list[str]] = None,
        ticket_type: str = "",
        rendering_info: Any = None,
        statuses: Optional[dict[str, str]] = None,
    ) -> tuple[list[str], dict[str, Any]]:
        rendering = rendering_info if rendering_info is not None else await self.rendering_info()
        sts = statuses if statuses is not None else await self.object_statuses()
        object_ids = pick_adjacent_from_snapshot(rendering, sts, quantity, target_blocks=target_blocks)
        if not object_ids:
            return [], {"rendering_info": rendering, "statuses": sts}
        await self.init_hold_token()
        hold_result = await self.hold_objects(object_ids, ticket_type=ticket_type)
        errors = hold_result.get("errors") if isinstance(hold_result, dict) else None
        if errors:
            raise RuntimeError(f"SeatCloud hold failed: {errors}")
        return object_ids, {"rendering_info": rendering, "statuses": sts, "hold": hold_result}

    async def watch_changes(self, on_change):
        if not self.hold_token:
            await self.init_hold_token()
        ws_url = (
            f"wss://api.seatcloud.com/system/public/{self.workspace_key}/events/"
            f"{self.event_key}/changes/socket?holdToken={self.hold_token}"
        )

        async def _runner():
            while True:
                try:
                    async with self.session.ws_connect(ws_url, heartbeat=25) as ws:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    await on_change(json.loads(msg.data))
                                except Exception:
                                    pass
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                                break
                except Exception as e:
                    log.debug(f"SeatCloud WS reconnect: {e}")
                    await asyncio.sleep(1.5)

        self._ws_task = asyncio.create_task(_runner())
