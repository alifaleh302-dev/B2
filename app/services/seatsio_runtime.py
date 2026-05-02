"""
Warm caches for SeatCloud seated events.

Goal:
  • prefetch current rendering info before the sale moment
  • keep object statuses hot in memory
  • provide a fast snapshot for the booking engine
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from app.core.config import seatsio_prewarm_enabled, seatsio_status_interval
from app.services.seatsio_client import SeatsioClient

log = logging.getLogger("seatsio_runtime")

_PREWARM: dict[str, dict[str, Any]] = {}


async def ensure_event_warm(event_key: str) -> None:
    if not seatsio_prewarm_enabled() or not event_key:
        return
    state = _PREWARM.get(event_key)
    if state and state.get("task") and not state["task"].done():
        return

    state = {
        "rendering_info": None,
        "statuses": {},
        "last_update": 0.0,
        "task": None,
    }
    _PREWARM[event_key] = state

    async def _loop():
        while True:
            try:
                async with SeatsioClient(event_key) as client:
                    if state.get("rendering_info") is None:
                        state["rendering_info"] = await client.rendering_info()
                    while True:
                        state["statuses"] = await client.object_statuses()
                        state["last_update"] = time.time()
                        await asyncio.sleep(max(0.25, seatsio_status_interval()))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug(f"prewarm loop {event_key} failed: {e}")
                await asyncio.sleep(2)

    state["task"] = asyncio.create_task(_loop(), name=f"seatwarm:{event_key}")


def get_snapshot(event_key: str, max_age: float = 3.0) -> Optional[dict[str, Any]]:
    state = _PREWARM.get(event_key)
    if not state:
        return None
    if (time.time() - float(state.get("last_update") or 0)) > max_age:
        return None
    return {
        "rendering_info": state.get("rendering_info"),
        "statuses": state.get("statuses") or {},
        "last_update": state.get("last_update") or 0,
    }


async def stop_all() -> None:
    tasks = [s.get("task") for s in _PREWARM.values() if s.get("task")]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _PREWARM.clear()
