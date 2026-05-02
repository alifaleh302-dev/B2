"""
Direct HTTP booking engine — fast path for Webook.

Enhancements in this version:
  • keeps the old non-seated HTTP flow
  • adds a real SeatCloud / seats.io reservation layer for seated events
  • supports prewarmed status snapshots and stalker-mode for released seats
  • enriches the checkout/cart payload with hold-token + selected seats
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

import aiohttp

from app.core.config import (
    WEBOOK_API,
    WEBOOK_ORIGIN,
    WEBOOK_PUBLIC_TOKEN,
    seatsio_enabled,
    seatsio_stalker_enabled,
    seatsio_stalker_max_wait,
    seatsio_stalker_poll_interval,
    target_blocks,
)
from app.services.seatsio_client import SeatsioClient
from app.services.seatsio_runtime import ensure_event_warm, get_snapshot

log = logging.getLogger("booking_http")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

SEATED_EVENT_KEY_CANDIDATES = {
    "seats_io_event_key", "seatsio_event_key", "seatcloud_event_key",
    "event_key", "chart_key", "chartKey", "eventKey",
}


def build_headers(bearer: str, lang: str = "en") -> dict[str, str]:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": DEFAULT_UA,
        "accept-language": "ar-SA",
        "authorization": f"Bearer {bearer}" if bearer else "Bearer",
        "token": WEBOOK_PUBLIC_TOKEN,
        "origin": WEBOOK_ORIGIN,
        "referer": f"{WEBOOK_ORIGIN}/",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Chromium";v="128"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    }


async def _get(session: aiohttp.ClientSession, url: str, bearer: str, timeout: int = 15) -> tuple[int, Any]:
    try:
        async with session.get(url, headers=build_headers(bearer), timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": (await r.text())[:1200]}
            return r.status, data
    except Exception as e:
        return 0, {"error": str(e)[:200]}


async def _post(session: aiohttp.ClientSession, url: str, bearer: str, body: dict, timeout: int = 25) -> tuple[int, Any]:
    try:
        async with session.post(url, headers=build_headers(bearer), json=body, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": (await r.text())[:1200]}
            return r.status, data
    except Exception as e:
        return 0, {"error": str(e)[:200]}


def _deep_find_first(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = _deep_find_first(v, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_first(item, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def _find_ticket_blob(raw_payload: dict[str, Any], ticket_id: str) -> dict[str, Any]:
    event_ticket = ((raw_payload or {}).get("data") or {}).get("event_ticket") or []
    for item in event_ticket:
        if str(item.get("_id") or item.get("id")) == str(ticket_id):
            return item
    return {}


async def fetch_event_meta(session: aiohttp.ClientSession, slug: str, bearer: str) -> dict[str, Any]:
    url = f"{WEBOOK_API}/event-detail/{slug}?lang=en&visible_in=rs"
    status, data = await _get(session, url, bearer)
    if status != 200 or not isinstance(data, dict):
        return {}
    d = data.get("data") or {}
    return {
        "event_id": d.get("_id"),
        "title": d.get("title") or slug,
        "is_seated": bool(d.get("is_seated")),
        "booking_seats_without_map": bool(d.get("booking_seats_without_map")),
        "time_slot_dates": list(d.get("time_slots") or []),
        "is_experience": bool(d.get("is_experience")),
        "require_visa": bool(d.get("require_visa")),
        "raw": d,
    }


async def fetch_raw_ticket_details(session: aiohttp.ClientSession, slug: str, bearer: str = "") -> dict[str, Any]:
    status, data = await _get(
        session,
        f"{WEBOOK_API}/event-ticket-details/{slug}?lang=en&visible_in=rs&page=1",
        bearer,
    )
    return data if status == 200 and isinstance(data, dict) else {}


async def resolve_seated_manifest(
    session: aiohttp.ClientSession,
    slug: str,
    ticket_id: str,
    bearer: str = "",
    *,
    ticket_meta: Optional[dict[str, Any]] = None,
    event_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    raw_tickets = await fetch_raw_ticket_details(session, slug, bearer)
    raw_ticket = _find_ticket_blob(raw_tickets, ticket_id)
    raw_event = ((raw_tickets or {}).get("data") or {}).get("event") or {}
    meta_raw = (event_meta or {}).get("raw") or {}

    event_key = (
        _deep_find_first(raw_ticket, SEATED_EVENT_KEY_CANDIDATES)
        or _deep_find_first(raw_event, SEATED_EVENT_KEY_CANDIDATES)
        or _deep_find_first(meta_raw, SEATED_EVENT_KEY_CANDIDATES)
        or ""
    )
    category = (
        (ticket_meta or {}).get("seats_io_category")
        or raw_ticket.get("seats_io_category")
        or raw_ticket.get("seatcloud_category")
        or raw_ticket.get("category")
        or ""
    )
    return {
        "event_key": str(event_key or "").strip(),
        "category": str(category or "").strip(),
        "raw_ticket": raw_ticket,
        "raw_event": raw_event,
    }


async def prewarm_event_from_slug(slug: str, ticket_id: str = "") -> None:
    if not seatsio_enabled() or not slug:
        return
    async with aiohttp.ClientSession() as session:
        meta = await fetch_event_meta(session, slug, "")
        if not meta.get("is_seated"):
            return
        manifest = await resolve_seated_manifest(session, slug, ticket_id, "", event_meta=meta)
        if manifest.get("event_key"):
            await ensure_event_warm(manifest["event_key"])


async def fetch_timeslot_id(session: aiohttp.ClientSession, slug: str, date_str: str, ticket_id: str, bearer: str) -> Optional[str]:
    url = f"{WEBOOK_API}/event-detail/{slug}/timeslot-capacity?time_slot={date_str}&visible_in=rs&lang=en"
    status, data = await _get(session, url, bearer)
    if status != 200 or not isinstance(data, dict):
        return None
    slots = data.get("data") or []
    for s in slots:
        if s.get("is_soldout"):
            continue
        cap = s.get(ticket_id)
        if cap is None or cap == -1 or (isinstance(cap, (int, float)) and cap > 0):
            return s.get("_id")
    return slots[0].get("_id") if slots else None


async def add_to_cart(
    session: aiohttp.ClientSession,
    *,
    ticket_id: str,
    quantity: int,
    parent_event_id: str,
    time_slot_id: Optional[str],
    bearer: str,
    seat_payload: Optional[dict[str, Any]] = None,
) -> tuple[bool, Any]:
    body = {
        "ticket_id": ticket_id,
        "quantity": quantity,
        "type": "ticket",
        "parent_event_id": parent_event_id,
    }
    if time_slot_id:
        body["time_slot_id"] = time_slot_id
    if seat_payload:
        body.update({k: v for k, v in seat_payload.items() if v not in (None, "", [], {})})

    status, data = await _post(session, f"{WEBOOK_API}/cart/add-to-cart?lang=en", bearer, body)
    if status == 200 and isinstance(data, dict) and data.get("status") == "success":
        return True, data.get("data") or {}
    return False, data


async def clear_cart(session: aiohttp.ClientSession, parent_event_id: str, bearer: str) -> None:
    for url in [
        f"{WEBOOK_API}/cart/clear?lang=en&parent_event_id={parent_event_id}",
        f"{WEBOOK_API}/cart/clear-cart?lang=en&parent_event_id={parent_event_id}",
    ]:
        try:
            async with session.post(url, headers=build_headers(bearer), timeout=aiohttp.ClientTimeout(total=8)):
                pass
        except Exception:
            pass


async def create_checkout(
    session: aiohttp.ClientSession,
    *,
    slug: str,
    event_id: str,
    ticket_id: str,
    quantity: int,
    time_slot_id: Optional[str],
    bearer: str,
    payment_method: str = "credit_card",
    seat_payload: Optional[dict[str, Any]] = None,
) -> tuple[bool, dict]:
    body = {
        "event_id": event_id,
        "redirect": f"{WEBOOK_ORIGIN}/en/payment-success",
        "redirect_failed": f"{WEBOOK_ORIGIN}/en/payment-failed",
        "booking_source": "rs-web",
        "lang": "en",
        "payment_method": payment_method,
        "is_wallet": False,
        "saudi_redeem": None,
        "refund_guarantee": False,
        "perks": [],
        "merchandise": [],
        "addons": [],
        "vouchers": [],
        "tickets": [{"qty": quantity, "id": ticket_id}],
        "app_source": "rs",
    }
    if time_slot_id:
        body["time_slot_id"] = time_slot_id
    if seat_payload:
        body.update({k: v for k, v in seat_payload.items() if v not in (None, "", [], {})})

    status, data = await _post(session, f"{WEBOOK_API}/event-detail/{slug}/checkout?lang=en", bearer, body, timeout=30)
    if status == 200 and isinstance(data, dict) and data.get("status") == "success":
        return True, data.get("data") or {}
    return False, data or {}


async def hold_adjacent_seats(
    session: aiohttp.ClientSession,
    *,
    slug: str,
    event_id: str,
    ticket_id: str,
    quantity: int,
    time_slot_id: Optional[str],
    bearer: str,
) -> tuple[bool, list[str]]:
    candidates = [
        ("POST", f"{WEBOOK_API}/seats/hold", {
            "event_id": event_id, "ticket_id": ticket_id,
            "quantity": quantity, "time_slot_id": time_slot_id,
            "selection_mode": "best_available_adjacent",
        }),
        ("POST", f"{WEBOOK_API}/event-detail/{slug}/seats/hold", {
            "ticket_id": ticket_id,
            "quantity": quantity, "time_slot_id": time_slot_id,
            "mode": "best_available_adjacent",
        }),
    ]
    for method, url, body in candidates:
        try:
            status, data = await _post(session, url, bearer, body, timeout=15)
            if status == 200 and isinstance(data, dict) and data.get("status") == "success":
                seats = (data.get("data") or {}).get("seats") or []
                return True, [str(s) for s in seats]
        except Exception:
            continue
    return False, []


async def _reserve_seated_inventory(
    *,
    slug: str,
    ticket_id: str,
    quantity: int,
    bearer: str,
    manifest: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], list[str]]:
    logs: list[str] = []
    event_key = manifest.get("event_key") or ""
    if not event_key:
        return None, logs

    preferred_blocks = target_blocks()
    await ensure_event_warm(event_key)
    snapshot = get_snapshot(event_key)

    async with SeatsioClient(event_key) as client:
        # First try from warmed snapshot
        try:
            if snapshot and snapshot.get("rendering_info"):
                object_ids, _ = await client.pick_and_hold_adjacent(
                    quantity,
                    target_blocks=preferred_blocks,
                    ticket_type=manifest.get("category") or "",
                    rendering_info=snapshot.get("rendering_info"),
                    statuses=snapshot.get("statuses") or {},
                )
                if object_ids:
                    logs.append("🔥 seats prewarmed snapshot hit")
                    return {
                        "selected_seats": object_ids,
                        "selected_seat_labels": object_ids,
                        "hold_token": client.hold_token,
                        "seat_hold_token": client.hold_token,
                        "holdToken": client.hold_token,
                        "seats_io_category": manifest.get("category") or "",
                    }, logs
        except Exception as e:
            logs.append(f"snapshot miss: {str(e)[:120]}")

        # Direct live attempt
        try:
            object_ids, _ = await client.pick_and_hold_adjacent(
                quantity,
                target_blocks=preferred_blocks,
                ticket_type=manifest.get("category") or "",
            )
            if object_ids:
                logs.append("🪑 seats held from live SeatCloud")
                return {
                    "selected_seats": object_ids,
                    "selected_seat_labels": object_ids,
                    "hold_token": client.hold_token,
                    "seat_hold_token": client.hold_token,
                    "holdToken": client.hold_token,
                    "seats_io_category": manifest.get("category") or "",
                }, logs
        except Exception as e:
            logs.append(f"live hold fail: {str(e)[:140]}")

        # Stalker mode: keep polling for released seats briefly
        if seatsio_stalker_enabled():
            deadline = time.time() + max(2.0, seatsio_stalker_max_wait())
            while time.time() < deadline:
                try:
                    statuses = await client.object_statuses()
                    object_ids, _ = await client.pick_and_hold_adjacent(
                        quantity,
                        target_blocks=preferred_blocks,
                        ticket_type=manifest.get("category") or "",
                        rendering_info=snapshot.get("rendering_info") if snapshot else None,
                        statuses=statuses,
                    )
                    if object_ids:
                        logs.append("🎯 stalker mode captured released seats")
                        return {
                            "selected_seats": object_ids,
                            "selected_seat_labels": object_ids,
                            "hold_token": client.hold_token,
                            "seat_hold_token": client.hold_token,
                            "holdToken": client.hold_token,
                            "seats_io_category": manifest.get("category") or "",
                        }, logs
                except Exception:
                    pass
                await asyncio.sleep(max(0.15, seatsio_stalker_poll_interval()))

    return None, logs


async def book_ticket_http(
    *,
    bearer: str,
    slug: str,
    ticket_id: str,
    quantity: int,
    payment_method: str = "credit_card",
    preferred_date: Optional[str] = None,
    ticket_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "payment_url": "",
        "order_id": "",
        "payment_session_id": "",
        "seat_info": {},
        "logs": [],
        "error": "",
    }
    if not bearer:
        result["error"] = "لا يوجد توكن JWT صالح (يحتاج تسجيل دخول جديد)"
        return result

    async with aiohttp.ClientSession() as session:
        meta = await fetch_event_meta(session, slug, bearer)
        if not meta.get("event_id"):
            result["error"] = "تعذّر جلب بيانات الفعالية"
            return result
        event_id = meta["event_id"]
        result["logs"].append(f"📋 event_id={event_id[:8]} seated={meta['is_seated']}")

        time_slot_id = None
        dates = meta.get("time_slot_dates") or []
        if dates:
            pick = preferred_date if preferred_date in dates else dates[0]
            time_slot_id = await fetch_timeslot_id(session, slug, pick, ticket_id, bearer)
            if time_slot_id:
                result["logs"].append(f"⏰ time_slot={pick}")

        seat_payload = None
        if meta.get("is_seated") and not meta.get("booking_seats_without_map"):
            manifest = await resolve_seated_manifest(
                session,
                slug,
                ticket_id,
                bearer,
                ticket_meta=ticket_meta,
                event_meta=meta,
            )
            if seatsio_enabled() and manifest.get("event_key"):
                seat_payload, seat_logs = await _reserve_seated_inventory(
                    slug=slug,
                    ticket_id=ticket_id,
                    quantity=quantity,
                    bearer=bearer,
                    manifest=manifest,
                )
                result["logs"].extend(seat_logs)
                if seat_payload:
                    result["seat_info"] = {
                        "seats": seat_payload.get("selected_seats") or [],
                        "hold_token": seat_payload.get("hold_token") or "",
                        "category": manifest.get("category") or "",
                        "event_key": manifest.get("event_key") or "",
                    }
                else:
                    ok, seats = await hold_adjacent_seats(
                        session,
                        slug=slug,
                        event_id=event_id,
                        ticket_id=ticket_id,
                        quantity=quantity,
                        time_slot_id=time_slot_id,
                        bearer=bearer,
                    )
                    if ok and seats:
                        seat_payload = {"selected_seats": seats, "selected_seat_labels": seats}
                        result["seat_info"] = {"seats": seats}
                        result["logs"].append("🪑 legacy hold endpoint worked")
                    else:
                        result["error"] = "تعذّر حجز مقاعد seats.io عبر HTTP"
                        return result
            else:
                result["logs"].append("⚠️ no SeatCloud event key found — fallback only")

        await clear_cart(session, event_id, bearer)
        ok, cart_data = await add_to_cart(
            session,
            ticket_id=ticket_id,
            quantity=quantity,
            parent_event_id=event_id,
            time_slot_id=time_slot_id,
            bearer=bearer,
            seat_payload=seat_payload,
        )
        if not ok:
            msg = (cart_data.get("message") or cart_data.get("error") or str(cart_data))[:300]
            result["error"] = f"فشل add-to-cart: {msg}"
            return result
        result["logs"].append(f"🛒 cart ok ({cart_data.get('item_quantity', quantity)} tickets)")

        ok, co_data = await create_checkout(
            session,
            slug=slug,
            event_id=event_id,
            ticket_id=ticket_id,
            quantity=quantity,
            time_slot_id=time_slot_id,
            bearer=bearer,
            payment_method=payment_method,
            seat_payload=seat_payload,
        )
        if not ok:
            msg = (co_data.get("message") or co_data.get("error") or str(co_data))[:350]
            result["error"] = f"فشل checkout: {msg}"
            return result

        pay_url = co_data.get("redirect_url") or (co_data.get("response") or {}).get("redirect_url")
        if not pay_url:
            result["error"] = "checkout نجح لكن لم يرجع redirect_url"
            return result

        result["ok"] = True
        result["payment_url"] = pay_url
        result["order_id"] = co_data.get("order_id", "")
        result["payment_session_id"] = co_data.get("payment_session_id", "")
        result["logs"].append("💳 PayTabs URL ready")
        return result
