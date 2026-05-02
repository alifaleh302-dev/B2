"""
Background event monitor.

Two loops:
  A) fetch_loop  — sitemap/home discovery of new events
  B) sniper_loop — active ticket monitoring + prewarming for seated events
"""
from __future__ import annotations

import asyncio
import logging

from app.core.config import EVENT_POLL_INTERVAL, SNIPER_POLL_INTERVAL
from app.core.storage import list_snipers, set_sniper_status, upsert_event, list_accounts
from app.services.booking_http import prewarm_event_from_slug
from app.services.event_discovery import enrich_all, fetch_event_slugs
from app.services.webook_api import get_event_tickets

log = logging.getLogger("monitor")
_BOOTSTRAPPED = False


async def fetch_loop(notifier=None) -> None:
    await asyncio.sleep(10)
    while True:
        try:
            await _run_once(notifier)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"fetch_loop error: {e}")
        await asyncio.sleep(EVENT_POLL_INTERVAL)


async def _run_once(notifier) -> None:
    global _BOOTSTRAPPED
    slugs = await fetch_event_slugs()
    if not slugs:
        return
    enriched = await enrich_all(slugs, concurrency=4)
    from app.core.config import telegram_chat_id as _cid
    from app.bot import tokens as tok

    TELEGRAM_CHAT_ID = _cid()
    new_events = []
    for ev in enriched:
        is_new = upsert_event(ev["slug"], ev)
        if is_new:
            new_events.append(ev)

    if not _BOOTSTRAPPED:
        _BOOTSTRAPPED = True
        log.info(f"monitor bootstrap complete — cached {len(enriched)} events")
        return

    if not notifier or not TELEGRAM_CHAT_ID:
        return

    for ev in new_events[:5]:
        evt_tok = tok.put({"slug": ev["slug"]})
        rkb = {
            "inline_keyboard": [
                [{"text": "🎟️ فتح الفعالية", "callback_data": f"evt:{evt_tok}"}],
                [{"text": "📁 كل الفعاليات", "callback_data": "events:0"}],
            ]
        }
        txt = (
            f"🆕 <b>فعالية جديدة على Webook</b>\n\n"
            f"🎭 {ev.get('title') or ev.get('slug')}\n"
            f"🎟️ أنواع التذاكر: <b>{len(ev.get('tickets') or [])}</b>\n"
            f"🪑 محجوزة بمقاعد: <b>{'نعم' if ev.get('is_seated') else 'لا'}</b>\n\n"
            f"تم رصدها من أحدث فعاليات المنصة."
        )
        try:
            await notifier.send(TELEGRAM_CHAT_ID, txt, reply_markup=rkb)
        except Exception as e:
            log.debug(f"alert send failed: {e}")


async def sniper_loop(notifier=None) -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await _sniper_tick(notifier)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"sniper_loop error: {e}")
        await asyncio.sleep(SNIPER_POLL_INTERVAL)


async def _sniper_tick(notifier) -> None:
    tasks = list_snipers(status="active")
    if not tasks:
        return

    by_slug: dict[str, list[dict]] = {}
    for t in tasks:
        by_slug.setdefault(t["event_slug"], []).append(t)

    for slug, task_list in by_slug.items():
        data = await get_event_tickets(slug)
        if not data:
            continue
        tickets = {t["id"]: t for t in data.get("tickets") or []}

        for task in task_list:
            ttype = tickets.get(task["ticket_type_id"])
            if not ttype:
                continue

            # Prewarm seated inventory before opening moment.
            if data.get("is_seated") or ttype.get("seats_io_category"):
                asyncio.create_task(prewarm_event_from_slug(slug, ttype.get("id") or ""))

            if ttype.get("sale_status") != "ongoing":
                continue

            from app.services.booking_orchestrator import book_all
            from app.services.distributor import distribute

            accounts = [a for a in list_accounts(status="ready") if a.get("access_token")]
            if not accounts:
                continue
            try:
                plan, _meta = distribute(
                    task["quantity"],
                    accounts=accounts,
                    max_per_order=ttype["max_per_order"],
                    min_per_order=ttype["min_per_order"],
                )
            except Exception as e:
                log.warning(f"sniper distribute err: {e}")
                continue

            set_sniper_status(task["id"], "firing")
            log.info(f"🎯 SNIPER FIRING task={task['id']} slug={slug}")
            results = await book_all(
                plan,
                event_slug=slug,
                event_title=data["event"].get("title") or slug,
                ticket_id=ttype["id"],
                ticket_title=ttype["title"],
                ticket_price=ttype.get("display_price") or ttype.get("price") or 0,
                currency=ttype["currency"],
                chat_id=str(task["chat_id"]),
                notifier=notifier,
                ticket_meta=ttype,
            )
            set_sniper_status(task["id"], "done")
            if notifier:
                await _notify_sniper_result(notifier, task, results)


async def _notify_sniper_result(notifier, task: dict, results: list[dict]):
    succ = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    lines = [
        f"🎯 <b>تنفيذ القنّاص — مهمة #{task['id']}</b>\n",
        f"✅ ناجح: {len(succ)}    ❌ فاشل: {len(fail)}\n",
    ]
    for r in succ:
        seat_info = r.get("seat_info") or {}
        seats = seat_info.get("seats") or []
        seat_line = f"\n  🪑 المقاعد: {', '.join(seats[:8])}" if seats else ""
        lines.append(
            f"• <code>{r['label']}</code> → {r['quantity']} تذكرة"
            f"{seat_line}\n  💳 <a href=\"{r['payment_url']}\">رابط الدفع</a>"
        )
    for r in fail:
        lines.append(f"• <code>{r.get('label') or r.get('account_id')}</code>: {r.get('error')}")
    try:
        await notifier.send(str(task["chat_id"]), "\n".join(lines))
    except Exception:
        pass
