"""
Extract SeatCloud / Seats.io public runtime tokens from Webook frontend bundles.

This is inspired by the legacy bot.exe approach, but made safer:
  • discovers current bundle dynamically from webook HTML
  • refreshes on TTL instead of hard-coding a single asset URL
  • returns a normalized dict usable by the booking engine
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import aiohttp

from app.core.config import WEBOOK_ORIGIN, seatsio_token_ttl

log = logging.getLogger("seatsio_tokens")

ASSET_RE = re.compile(
    r"(?P<url>https://wbk-assets\.webook\.com[^\"']*?/assets/index-[A-Za-z0-9_-]+\.js|/assets/index-[A-Za-z0-9_-]+\.js)",
    re.I,
)
PATTERNS = {
    "seatio_workspace_key": re.compile(
        r"VITE_PUBLIC_SEATIO_WORKSPACE_KEY[^\"']*[\"']([a-fA-F0-9\-]{36})[\"']"
    ),
    "seatcloud_workspace_key": re.compile(
        r"VITE_PUBLIC_SEATCLOUD_WORKSPACE_KEY[^\"']*[\"']([a-fA-F0-9]{24})[\"']"
    ),
    "tickets_api_token": re.compile(
        r"VITE_PUBLIC_TICKETS_API_TOKEN[^\"']*[\"']([a-fA-F0-9]{64})[\"']"
    ),
    "captcha_site_key": re.compile(
        r"VITE_PUBLIC_CAPTCHA_KEY[^\"']*[\"']([A-Za-z0-9_-]{20,})[\"']"
    ),
}


class TokenCache:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.asset_url: str = ""
        self.fetched_at: float = 0.0

    def is_fresh(self) -> bool:
        ttl = max(60, seatsio_token_ttl())
        return bool(self.values) and (time.time() - self.fetched_at) < ttl

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.values,
            "asset_url": self.asset_url,
            "fetched_at": self.fetched_at,
            "workspace_key": self.values.get("seatcloud_workspace_key")
            or self.values.get("seatio_workspace_key", ""),
        }


CACHE = TokenCache()


async def _fetch_text(session: aiohttp.ClientSession, url: str, timeout: int = 20) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.text()


async def _discover_asset_urls(session: aiohttp.ClientSession) -> list[str]:
    candidates: list[str] = []
    for page in (f"{WEBOOK_ORIGIN}/en", f"{WEBOOK_ORIGIN}/ar", WEBOOK_ORIGIN):
        try:
            html = await _fetch_text(session, page, timeout=15)
        except Exception as e:
            log.debug(f"discover asset from {page} failed: {e}")
            continue
        for m in ASSET_RE.finditer(html):
            url = m.group("url")
            if url.startswith("/"):
                url = "https://wbk-assets.webook.com" + url
            if url not in candidates:
                candidates.append(url)
    return candidates[:5]


def _extract_tokens(js: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, pat in PATTERNS.items():
        m = pat.search(js)
        if m:
            found[key] = m.group(1)
    return found


async def refresh_tokens(force: bool = False) -> dict[str, Any]:
    if CACHE.is_fresh() and not force:
        return CACHE.to_dict()

    async with aiohttp.ClientSession() as session:
        asset_urls = await _discover_asset_urls(session)
        if not asset_urls:
            log.warning("No current Webook asset bundle could be discovered")
            return CACHE.to_dict()

        for asset_url in asset_urls:
            try:
                js = await _fetch_text(session, asset_url, timeout=30)
            except Exception as e:
                log.debug(f"fetch asset {asset_url} failed: {e}")
                continue
            tokens = _extract_tokens(js)
            if tokens.get("seatcloud_workspace_key") or tokens.get("seatio_workspace_key"):
                CACHE.values = tokens
                CACHE.asset_url = asset_url
                CACHE.fetched_at = time.time()
                log.info("SeatCloud tokens refreshed from current Webook bundle")
                return CACHE.to_dict()

    return CACHE.to_dict()


async def ensure_tokens() -> dict[str, Any]:
    if CACHE.is_fresh():
        return CACHE.to_dict()
    return await refresh_tokens(force=True)
