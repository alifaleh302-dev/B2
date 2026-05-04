"""
Cloudflare Turnstile solver via 2Captcha (createTask / getTaskResult).

Used by the booking pipeline whenever Webook's hold-token endpoint
responds with `{"errors": {"turnstile": ...}}` — instead of giving up,
we obtain a fresh Turnstile token through 2Captcha and retry.

Public functions
────────────────
solve_turnstile(*, page_url: str, sitekey: str = "", action: str = "",
                cdata: str = "", page_data: str = "",
                timeout: int = 180) -> dict
    → {"ok": bool, "token": str, "user_agent": str, "error": str,
       "task_id": int|None}

discover_turnstile_sitekey(page_url: str) -> str
    → best-effort extraction of the Turnstile sitekey from a Webook page
      (used as a soft fallback when no sitekey is provided in config).

The default Webook Turnstile sitekey is exposed via
``app.core.config.webook_turnstile_sitekey()`` and can be overridden
through the /admin UI (key: ``WEBOOK_TURNSTILE_SITEKEY``).

Tokens are single-use and expire after ~5 minutes; never cache them
across booking attempts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

import aiohttp

from app.core.config import (
    two_captcha_api_key,
    webook_turnstile_sitekey,
    WEBOOK_ORIGIN,
)

log = logging.getLogger("turnstile")

API_BASE = "https://api.2captcha.com"

# Heuristic patterns for sitekey discovery on Webook pages
_SITEKEY_PATTERNS = [
    re.compile(r'data-sitekey=["\']([0-9A-Za-z_\-]{8,})["\']'),
    re.compile(r'turnstile[^"\']{0,80}["\']sitekey["\']\s*:\s*["\']([0-9A-Za-z_\-]{8,})["\']', re.I),
    re.compile(r'VITE_PUBLIC_TURNSTILE[_A-Z]*KEY[^"\']*["\']([0-9A-Za-z_\-]{8,})["\']'),
    re.compile(r'CAPTCHA[_A-Z]*KEY[^"\']*["\'](0x[0-9A-Fa-f]{20,})["\']'),
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


# ════════════════════════════════════════════════════════════════════════
# Sitekey discovery
# ════════════════════════════════════════════════════════════════════════
async def discover_turnstile_sitekey(page_url: str,
                                       session: Optional[aiohttp.ClientSession] = None,
                                       ) -> str:
    """Best-effort sitekey discovery from a webook page.

    Returns "" when nothing matches. The caller should fall back to the
    configured default sitekey in that case.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(
            page_url,
            headers={
                "user-agent": DEFAULT_UA,
                "accept": "text/html,application/xhtml+xml",
                "accept-language": "ar-SA,ar;q=0.9,en;q=0.8",
            },
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as r:
            html = await r.text()
        for pat in _SITEKEY_PATTERNS:
            m = pat.search(html)
            if m:
                return m.group(1)
    except Exception as e:
        log.debug(f"sitekey discovery failed for {page_url}: {e}")
    finally:
        if own_session:
            await session.close()
    return ""


# ════════════════════════════════════════════════════════════════════════
# 2Captcha API
# ════════════════════════════════════════════════════════════════════════
async def _create_task(session: aiohttp.ClientSession, *,
                        api_key: str,
                        website_url: str,
                        website_key: str,
                        action: str = "",
                        cdata: str = "",
                        page_data: str = "",
                        ) -> tuple[Optional[int], str]:
    """POST /createTask. Returns (task_id, error)."""
    task: dict[str, Any] = {
        "type": "TurnstileTaskProxyless",
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if action:
        task["action"] = action
    if cdata:
        task["data"] = cdata
    if page_data:
        task["pagedata"] = page_data

    body = {"clientKey": api_key, "task": task}
    try:
        async with session.post(
            f"{API_BASE}/createTask", json=body,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json(content_type=None)
    except Exception as e:
        return None, f"createTask network: {e}"

    if not isinstance(data, dict):
        return None, "createTask: bad response"
    if data.get("errorId"):
        return None, f"2captcha error: {data.get('errorCode') or data.get('errorDescription') or data}"
    tid = data.get("taskId")
    if not tid:
        return None, "createTask: no taskId returned"
    return int(tid), ""


async def _poll_result(session: aiohttp.ClientSession, *,
                        api_key: str, task_id: int,
                        timeout: int = 180,
                        poll_interval: float = 5.0,
                        ) -> tuple[dict, str]:
    """POST /getTaskResult repeatedly until ready or timeout."""
    deadline = time.time() + max(30, timeout)
    body = {"clientKey": api_key, "taskId": task_id}
    last_err = "timeout"
    # Initial 8 second delay — Turnstile usually needs ~15-25s
    await asyncio.sleep(8)
    while time.time() < deadline:
        try:
            async with session.post(
                f"{API_BASE}/getTaskResult", json=body,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                data = await r.json(content_type=None)
        except Exception as e:
            last_err = f"getTaskResult network: {e}"
            await asyncio.sleep(poll_interval)
            continue

        if not isinstance(data, dict):
            last_err = "getTaskResult: bad response"
            await asyncio.sleep(poll_interval)
            continue
        if data.get("errorId"):
            return {}, f"2captcha error: {data.get('errorCode') or data.get('errorDescription') or data}"
        status = data.get("status")
        if status == "ready":
            sol = data.get("solution") or {}
            if not isinstance(sol, dict):
                return {}, "getTaskResult: solution missing"
            return sol, ""
        # status == "processing" → keep waiting
        await asyncio.sleep(poll_interval)
    return {}, last_err


# ════════════════════════════════════════════════════════════════════════
# Public entry point
# ════════════════════════════════════════════════════════════════════════
async def solve_turnstile(
    *,
    page_url: str,
    sitekey: str = "",
    action: str = "",
    cdata: str = "",
    page_data: str = "",
    timeout: int = 180,
    session: Optional[aiohttp.ClientSession] = None,
) -> dict:
    """Solve a Cloudflare Turnstile challenge via 2Captcha.

    Args:
      page_url   — the URL where the Turnstile widget is hosted
                   (any Webook event/book page works).
      sitekey    — Turnstile sitekey. If empty we fall back to the
                   value configured in admin (``WEBOOK_TURNSTILE_SITEKEY``)
                   and finally to runtime discovery from page_url.
      action / cdata / page_data — only needed for Cloudflare
                   "challenge mode" pages. For the standalone widget
                   used by Webook hold-token they're ignored.
      timeout    — total seconds to wait for a solution.
      session    — optional shared aiohttp session.

    Returns dict with keys:
      ok, token, user_agent, task_id, error
    """
    out = {"ok": False, "token": "", "user_agent": "", "task_id": None, "error": ""}

    api_key = (two_captcha_api_key() or "").strip()
    if not api_key:
        out["error"] = "CAPTCHA_API_KEY غير مضبوط"
        return out

    sk = (sitekey or webook_turnstile_sitekey() or "").strip()
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        if not sk:
            sk = await discover_turnstile_sitekey(page_url, session=session)
        if not sk:
            out["error"] = "تعذّر تحديد Turnstile sitekey"
            return out

        log.info(f"🧩 turnstile solve start: url={page_url[:80]} sitekey=…{sk[-6:]}")
        tid, err = await _create_task(
            session, api_key=api_key,
            website_url=page_url, website_key=sk,
            action=action, cdata=cdata, page_data=page_data,
        )
        if not tid:
            out["error"] = err
            return out
        out["task_id"] = tid

        sol, err = await _poll_result(
            session, api_key=api_key, task_id=tid, timeout=timeout,
        )
        if err:
            out["error"] = err
            return out
        token = str(sol.get("token") or "").strip()
        if not token:
            out["error"] = "2captcha returned empty token"
            return out
        out["ok"] = True
        out["token"] = token
        out["user_agent"] = str(sol.get("userAgent") or "")
        log.info(f"✅ turnstile solved: …{token[-10:]}")
        return out
    finally:
        if own_session:
            try:
                await session.close()
            except Exception:
                pass


def webook_book_page(slug: str) -> str:
    """Canonical Webook page URL used as the Turnstile widget host."""
    return f"{WEBOOK_ORIGIN}/en/events/{slug}/book"
