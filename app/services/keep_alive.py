"""Self-ping loop so Render free tier never sleeps."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from app.core.config import (
    KEEP_ALIVE_ENABLED, KEEP_ALIVE_INTERVAL, PUBLIC_URL,
)

log = logging.getLogger("keepalive")


async def keep_alive_loop() -> None:
    if not KEEP_ALIVE_ENABLED:
        log.info("keep-alive disabled")
        return
    await asyncio.sleep(30)  # wait for the web server to come up
    while True:
        try:
            if PUBLIC_URL:
                url = PUBLIC_URL.rstrip("/") + "/ping"
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        url, timeout=aiohttp.ClientTimeout(total=20)
                    ) as r:
                        log.info(f"🫀 ping {url} → {r.status}")
        except Exception as e:
            log.warning(f"ping failed: {e}")
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)
