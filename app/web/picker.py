"""
Telegram WebApp Mini Picker — visual seats.io picker inside Telegram.

v2 architecture (matches the real seats_planner provider used by webook):

  • The page does NOT try to embed seats.io's external chart.js (which
    requires Cloudflare Turnstile + recaptcha completion in an iframe and
    fails inside Telegram WebApp). Instead, the FastAPI backend exposes a
    JSON endpoint that returns the same data the chart would show:
      GET /picker/{token}/data
        → {workspace, event_key, chart_key, blocks: [...], categories: [...]}
    The page renders the chart itself with plain SVG/HTML using the
    polygon coordinates from `content.areas[]`.

  • The user picks one block as PRIMARY (⭐) and any others as BACKUPS,
    optionally with a quantity per block. On confirm:
      Telegram.WebApp.sendData(JSON.stringify({primary, backups, seats}))
      → falls back to POST /picker/{token}/selection if sendData isn't
         available (i.e., page opened in a regular browser tab).

This bypasses Cloudflare Turnstile entirely because the page is served
from our own domain and we use webook's authenticated APIs server-side.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("picker")
router = APIRouter()


# ════════════════════════════════════════════════════════════════════════
# Picker session helpers (read/write the in-memory dict from handlers)
# ════════════════════════════════════════════════════════════════════════
def _get_session(session_token: str) -> dict[str, Any] | None:
    try:
        from app.bot.handlers import _PICKER_SESSIONS
        return _PICKER_SESSIONS.get(session_token)
    except Exception:
        return None


def _set_session_selection(session_token: str, payload: dict) -> bool:
    try:
        from app.bot.handlers import _PICKER_SESSIONS
        sess = _PICKER_SESSIONS.get(session_token)
        if not sess:
            return False
        primary = (payload.get("primary") or "").strip()
        backups = [str(b).strip() for b in (payload.get("backups") or []) if str(b).strip()]
        seats = [str(s) for s in (payload.get("seats") or []) if s]

        if primary:
            sess["primary"] = primary
        if backups:
            seen = set([primary] if primary else [])
            sess["backups"] = []
            for b in backups:
                if b in seen:
                    continue
                seen.add(b)
                sess["backups"].append(b)
        if seats:
            sess["preselected_seats"] = seats
        sess["webapp_completed"] = True
        return True
    except Exception as e:
        log.warning(f"set_session_selection error: {e}")
        return False


# ════════════════════════════════════════════════════════════════════════
# Public endpoints
# ════════════════════════════════════════════════════════════════════════
@router.get("/picker/{session_token}", response_class=HTMLResponse)
async def picker_page(session_token: str) -> HTMLResponse:
    sess = _get_session(session_token)
    if not sess:
        return HTMLResponse(_error_page("انتهت جلسة الاختيار.\nأعد المحاولة من البوت."),
                             status_code=410)
    return HTMLResponse(_picker_page_html(session_token, sess))


@router.get("/picker/{session_token}/data")
async def picker_data(session_token: str) -> JSONResponse:
    """Server-side fetch of map data (workspace + chart + event) and merge
    with our pre-cached blocks_meta. Returns a payload the JS picker uses
    to render the chart."""
    sess = _get_session(session_token)
    if not sess:
        raise HTTPException(410, "session expired")

    workspace_key = sess.get("workspace_key") or ""
    event_key = sess.get("event_key") or ""
    chart_key = sess.get("chart_key") or ""
    blocks_meta = sess.get("blocks_meta") or []

    payload: dict[str, Any] = {
        "workspace_key": workspace_key,
        "event_key": event_key,
        "chart_key": chart_key,
        "slug": sess.get("slug") or "",
        "fallback_used": bool(sess.get("fallback_used")),
        "blocks": [],
        "categories": [],
        "venue_name": "",
        "viewport": {"width": 600, "height": 600},
    }

    # If we don't have a chart_key/workspace, we can only return the
    # textual block list we built earlier.
    if not (workspace_key and chart_key):
        payload["blocks"] = [
            {
                "name": b.get("name", ""),
                "id": b.get("id") or b.get("name", ""),
                "category": b.get("category") or "",
                "free": b.get("free", -1),
                "total": b.get("total", -1),
            }
            for b in blocks_meta
        ]
        return JSONResponse(payload)

    # Fresh fetch from seats_planner (uses our v2 endpoints)
    try:
        from app.services.seatsio_client import (
            SeatsioClient, get_hold_token_from_webook,
        )
        from app.core.storage import list_accounts

        # Get a hold-token from webook so the chart shows live availability
        hold_token = ""
        try:
            event_id = ""
            # Try to get the event_id quickly via fetch_event_meta
            import aiohttp
            from app.services.booking_http import fetch_event_meta
            accs = list_accounts(status="ready")
            bearer = (accs[0].get("access_token") if accs else "") or ""
            if bearer:
                async with aiohttp.ClientSession() as s:
                    em = await fetch_event_meta(s, sess.get("slug") or "", bearer)
                    event_id = em.get("event_id") or ""
                if event_id:
                    ht, _ = await get_hold_token_from_webook(
                        slug=sess.get("slug") or "",
                        event_id=event_id, bearer=bearer,
                    )
                    hold_token = ht or ""
        except Exception as e:
            log.debug(f"picker hold-token soft-fail: {e}")

        async with SeatsioClient(
            event_key=event_key, workspace_key=workspace_key,
            chart_key=chart_key,
            provider=sess.get("seats_provider") or "",
            hold_token=hold_token,
        ) as c:
            event_data = await c.fetch_event()
            map_data = await c.fetch_map()
            statuses = await c.fetch_item_statuses()

        if not map_data:
            # Fallback to blocks_meta we already had
            payload["blocks"] = [
                {"name": b.get("name", ""), "id": b.get("id") or b.get("name", ""),
                 "category": b.get("category") or "",
                 "free": b.get("free", -1), "total": b.get("total", -1)}
                for b in blocks_meta
            ]
            return JSONResponse(payload)

        # Build a richer payload with polygon geometry for canvas rendering
        sold_specs = set()
        for sp in (event_data.get("specifications") or []):
            if isinstance(sp, dict):
                k = sp.get("id") if "id" in sp else sp.get("key")
                if k is not None:
                    try:
                        sold_specs.add(int(k))
                    except Exception:
                        pass

        levels = map_data.get("levels") or [{}]
        first_level = levels[0] if isinstance(levels, list) and levels else {}
        viewport = {
            "width": int(first_level.get("width") or 800),
            "height": int(first_level.get("height") or 800),
        }
        payload["viewport"] = viewport
        payload["venue_name"] = map_data.get("name") or ""

        # Categories
        for sp in (event_data.get("specifications") or []):
            if isinstance(sp, dict):
                k = sp.get("id") if "id" in sp else sp.get("key")
                payload["categories"].append({
                    "key": k,
                    "label": sp.get("name") or sp.get("label") or "",
                    "color": sp.get("color") or "",
                })

        # Areas → block list with polygons
        content = map_data.get("content") or {}
        for a in (content.get("areas") or []):
            if not isinstance(a, dict):
                continue
            if a.get("published") is False:
                continue
            spec = a.get("specification") or {}
            spec_key = spec.get("key") if "key" in spec else spec.get("id")
            try:
                spec_key_i = int(spec_key) if spec_key is not None else None
            except Exception:
                spec_key_i = None

            geom = a.get("geometry") or {}
            points = geom.get("points") or []
            occ = a.get("occupancy") or {}
            area_id = str(a.get("id") or a.get("name") or "")
            lbl = a.get("label")
            if isinstance(lbl, dict):
                name = a.get("name") or lbl.get("label") or area_id
            else:
                name = a.get("name") or area_id

            is_for_sale = (spec_key_i in sold_specs) if sold_specs else True
            status = statuses.get(area_id) or statuses.get(name) or ""

            payload["blocks"].append({
                "id": area_id,
                "name": name,
                "category": spec.get("label") or spec.get("name") or "",
                "category_key": spec_key,
                "color": spec.get("color") or "",
                "capacity": int(occ.get("capacity") or 0),
                "polygon": [{"x": p.get("x", 0), "y": p.get("y", 0)} for p in points],
                "center": geom.get("center") or {},
                "is_for_sale": is_for_sale,
                "status": status,
            })
        return JSONResponse(payload)
    except Exception as e:
        log.exception(f"picker_data error: {e}")
        # Fall back to whatever we already had cached
        payload["blocks"] = [
            {"name": b.get("name", ""), "id": b.get("id") or b.get("name", ""),
             "category": b.get("category") or "",
             "free": b.get("free", -1), "total": b.get("total", -1)}
            for b in blocks_meta
        ]
        payload["error"] = str(e)[:200]
        return JSONResponse(payload)


@router.post("/picker/{session_token}/selection")
async def picker_selection(session_token: str, request: Request):
    """Fallback for browsers without Telegram.WebApp.sendData."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    if not _set_session_selection(session_token, body or {}):
        raise HTTPException(404, "session not found")
    return JSONResponse({"ok": True})


# ════════════════════════════════════════════════════════════════════════
# HTML template
# ════════════════════════════════════════════════════════════════════════
_BASE_CSS = """
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; height: 100%; overscroll-behavior: contain;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Tahoma, sans-serif; }
body { background: var(--tg-theme-bg-color, #0f172a);
       color: var(--tg-theme-text-color, #e5e7eb); display: flex; flex-direction: column;
       min-height: 100vh; }
.topbar { padding: 12px 16px; background: rgba(255,255,255,.04);
          border-bottom: 1px solid rgba(255,255,255,.06);
          flex-shrink: 0; }
.topbar h1 { margin: 0; font-size: 15px; font-weight: 600; }
.topbar .sub { font-size: 11px; opacity: .65; margin-top: 4px; }
.legend { padding: 8px 16px; font-size: 12px; opacity: .85;
          border-bottom: 1px solid rgba(255,255,255,.05); flex-shrink: 0; }
.legend b { color: #fbbf24; }

#chart-wrap { flex: 1; position: relative; overflow: hidden;
              background: rgba(0,0,0,.2); }
#chart-svg  { width: 100%; height: 100%; touch-action: none;
              user-select: none; }
.block-poly { stroke: rgba(255,255,255,.25); stroke-width: 0.5;
              cursor: pointer; transition: opacity .15s, stroke-width .15s; }
.block-poly:hover { opacity: .85; stroke-width: 1.5; }
.block-poly.unavailable { opacity: .12; cursor: not-allowed; }
.block-poly.primary { stroke: #fbbf24; stroke-width: 2.5;
                      filter: drop-shadow(0 0 6px #fbbf24); }
.block-poly.backup  { stroke: #38bdf8; stroke-width: 2; }
.block-label { font-size: 9px; pointer-events: none; fill: #fff; font-weight: 600;
               text-anchor: middle; }

#fallback-list { padding: 0 16px 100px; overflow-y: auto; flex: 1; }
.block-row { display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; margin: 8px 0; background: rgba(255,255,255,.04);
  border: 1px solid rgba(255,255,255,.08); border-radius: 12px; cursor: pointer; }
.block-row.primary { border-color: rgba(251,191,36,.5); background: rgba(251,191,36,.08); }
.block-row.backup  { border-color: rgba(56,189,248,.4); background: rgba(56,189,248,.06); }
.block-row .name { font-weight: 600; font-size: 14px; }
.block-row .meta { font-size: 11px; opacity: .65; margin-top: 3px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
       margin-left: 6px; vertical-align: middle; }
.tag.star { background: rgba(251,191,36,.22); color: #fbbf24; }
.tag.bk   { background: rgba(56,189,248,.22); color: #38bdf8; }

.bottom { background: var(--tg-theme-bg-color, #111827);
          border-top: 1px solid rgba(255,255,255,.08);
          padding: 12px 16px;
          display: flex; gap: 10px; align-items: center; flex-shrink: 0;
          flex-wrap: wrap; }
.summary { flex: 1; font-size: 12px; line-height: 1.55; min-width: 0; }
.summary .row { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.btn { padding: 11px 18px; border-radius: 10px; border: 0; font-weight: 700;
       cursor: pointer; font-size: 14px; }
.btn.primary { background: var(--tg-theme-button-color, #2563eb);
                color: var(--tg-theme-button-text-color, #fff); }
.btn.primary:disabled { opacity: .4; cursor: not-allowed; }

#status { padding: 16px; text-align: center; font-size: 13px; opacity: .7; }
#error  { padding: 24px; text-align: center; color: #fca5a5; line-height: 1.7; }
.bg-img { position: absolute; pointer-events: none; opacity: 0.6; }

@media (min-width: 720px) {
  body { max-width: 720px; margin: 0 auto; }
}
"""


def _picker_page_html(session_token: str, sess: dict) -> str:
    slug = sess.get("slug") or ""
    return f"""<!doctype html>
<html lang="ar" dir="rtl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<title>Webook Picker</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>{_BASE_CSS}</style>
</head><body>

<div class="topbar">
  <h1>🗺️ اختيار البلوكات المرئي</h1>
  <div class="sub">{html.escape(slug)[:80]}</div>
</div>
<div class="legend">
  ⭐ <b>الرئيسي</b> — اضغط على بلوك أول لتعيينه ·
  بقية الضغطات تُضاف كـ <span style="color:#38bdf8">احتياطية</span> ·
  ضغطة ثانية على نفس البلوك تُلغي تحديده.
</div>

<div id="chart-wrap">
  <div id="status">🔄 جارٍ تحميل خريطة المقاعد…</div>
  <svg id="chart-svg" style="display:none" xmlns="http://www.w3.org/2000/svg"></svg>
  <div id="error" style="display:none"></div>
</div>

<div id="fallback-list" style="display:none"></div>

<div class="bottom">
  <div class="summary">
    <div class="row">⭐ الرئيسي: <b id="sum-primary">—</b></div>
    <div class="row">🔁 الاحتياطية: <b id="sum-backups">—</b></div>
  </div>
  <button id="btn-confirm" class="btn primary" disabled>تأكيد ➜</button>
</div>

<script>
const SESSION_TOKEN = {json.dumps(session_token)};
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {{ try {{ tg.expand(); tg.ready(); }} catch (e) {{}} }}

const STATE = {{
  primary: '',
  backups: [],
  blocks: [],     // populated from /picker/.../data
}};

function $(id) {{ return document.getElementById(id); }}

function updateSummary() {{
  $('sum-primary').textContent = STATE.primary || '—';
  $('sum-backups').textContent = STATE.backups.length ? STATE.backups.join(' → ') : '—';
  $('btn-confirm').disabled = !STATE.primary;
}}

function colorFromCss(rgba) {{
  // accept 'rgba(r,g,b,a)' or '#hex' or empty → fallback
  if (!rgba) return '#475569';
  return rgba;
}}

function tap(blockName) {{
  const b = STATE.blocks.find(x => x.name === blockName);
  if (!b) return;
  if (b.is_for_sale === false) return;   // can't pick sold-out
  if (STATE.primary === blockName) {{
    // toggle off primary → demote to backup
    STATE.primary = '';
    if (!STATE.backups.includes(blockName)) STATE.backups.push(blockName);
  }} else if (STATE.backups.includes(blockName)) {{
    // toggle off backup → remove
    STATE.backups = STATE.backups.filter(n => n !== blockName);
  }} else {{
    if (!STATE.primary) STATE.primary = blockName;
    else STATE.backups.push(blockName);
  }}
  redraw();
  updateSummary();
}}

function send() {{
  const payload = {{
    primary: STATE.primary,
    backups: STATE.backups,
    seats: [],   // generalAdmission → no individual seats
  }};
  if (tg && typeof tg.sendData === 'function') {{
    try {{ tg.sendData(JSON.stringify(payload)); return; }}
    catch (e) {{ console.warn('sendData failed', e); }}
  }}
  fetch('/picker/' + SESSION_TOKEN + '/selection', {{
    method: 'POST', headers: {{'content-type': 'application/json'}},
    body: JSON.stringify(payload),
  }}).then(r => r.json()).then(() => {{
    document.body.innerHTML =
      '<div style="padding:32px;text-align:center;color:#86efac;font-size:14px">' +
      '✅ تم إرسال اختيارك. يمكنك الرجوع للبوت الآن.</div>';
  }}).catch(() => {{ alert('تعذّر الإرسال. أعد المحاولة من البوت.'); }});
}}

document.addEventListener('DOMContentLoaded', () => {{
  $('btn-confirm').addEventListener('click', send);
  loadData();
}});

async function loadData() {{
  try {{
    const r = await fetch('/picker/' + SESSION_TOKEN + '/data');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    STATE.blocks = d.blocks || [];
    if (d.error) console.warn('picker data error:', d.error);

    // Decide rendering mode based on whether we have polygon geometry
    const hasPolygons = STATE.blocks.some(b => Array.isArray(b.polygon) && b.polygon.length >= 3);
    if (hasPolygons) {{
      renderSvg(d);
    }} else {{
      renderFallbackList();
    }}
  }} catch (e) {{
    console.error(e);
    showError('تعذّر تحميل بيانات الخريطة من السيرفر.');
  }}
}}

function showError(msg) {{
  $('status').style.display = 'none';
  $('chart-svg').style.display = 'none';
  $('error').style.display = 'block';
  $('error').textContent = msg;
}}

function renderSvg(d) {{
  const svg = $('chart-svg');
  $('status').style.display = 'none';
  svg.style.display = 'block';

  const vp = d.viewport || {{width:800, height:800}};
  // compute bounding box from polygons
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const b of STATE.blocks) {{
    for (const p of (b.polygon || [])) {{
      if (p.x < minX) minX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.x > maxX) maxX = p.x;
      if (p.y > maxY) maxY = p.y;
    }}
  }}
  if (!isFinite(minX)) {{ minX = 0; minY = 0; maxX = vp.width; maxY = vp.height; }}
  const pad = 20;
  const vbW = (maxX - minX) + pad*2;
  const vbH = (maxY - minY) + pad*2;
  svg.setAttribute('viewBox', `${{minX - pad}} ${{minY - pad}} ${{vbW}} ${{vbH}}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  drawAll();
}}

function drawAll() {{
  const svg = $('chart-svg');
  // wipe
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  const ns = 'http://www.w3.org/2000/svg';
  for (const b of STATE.blocks) {{
    if (!Array.isArray(b.polygon) || b.polygon.length < 3) continue;
    const points = b.polygon.map(p => `${{p.x}},${{p.y}}`).join(' ');
    const poly = document.createElementNS(ns, 'polygon');
    poly.setAttribute('points', points);
    poly.setAttribute('class', 'block-poly' +
      (b.is_for_sale === false ? ' unavailable' : '') +
      (STATE.primary === b.name ? ' primary' : '') +
      (STATE.backups.includes(b.name) ? ' backup' : '')
    );
    poly.setAttribute('fill', colorFromCss(b.color));
    poly.setAttribute('fill-opacity', b.is_for_sale === false ? '0.15' : '0.55');
    poly.dataset.name = b.name;
    poly.addEventListener('click', () => tap(b.name));
    poly.addEventListener('touchend', e => {{ e.preventDefault(); tap(b.name); }}, {{passive: false}});
    svg.appendChild(poly);

    // label at center
    if (b.center && typeof b.center.x === 'number') {{
      const t = document.createElementNS(ns, 'text');
      t.setAttribute('x', b.center.x);
      t.setAttribute('y', b.center.y);
      t.setAttribute('class', 'block-label');
      t.setAttribute('dominant-baseline', 'middle');
      t.textContent = b.name;
      svg.appendChild(t);
    }}
  }}
}}

function redraw() {{
  if ($('chart-svg').style.display !== 'none') {{
    drawAll();
  }} else {{
    renderFallbackList();
  }}
}}

function renderFallbackList() {{
  $('status').style.display = 'none';
  $('chart-svg').style.display = 'none';
  const list = $('fallback-list');
  list.style.display = 'block';
  list.innerHTML = '';

  // Group by category for readability
  const byCat = {{}};
  for (const b of STATE.blocks) {{
    const c = b.category || 'Other';
    (byCat[c] = byCat[c] || []).push(b);
  }}
  for (const cat of Object.keys(byCat)) {{
    const heading = document.createElement('div');
    heading.style.cssText = 'padding:14px 4px 4px;font-weight:700;font-size:13px;opacity:.85';
    heading.textContent = cat;
    list.appendChild(heading);
    for (const b of byCat[cat]) {{
      const row = document.createElement('div');
      const isPrim = STATE.primary === b.name;
      const isBack = STATE.backups.includes(b.name);
      row.className = 'block-row' + (isPrim ? ' primary' : (isBack ? ' backup' : ''));
      let badge = '';
      if (isPrim) badge = '<span class="tag star">⭐ رئيسي</span>';
      else if (isBack) badge = '<span class="tag bk">#' + (STATE.backups.indexOf(b.name) + 1) + '</span>';
      let counts = '';
      if (typeof b.capacity === 'number' && b.capacity > 0) counts = ' · سعة ' + b.capacity;
      else if (typeof b.free === 'number' && b.free >= 0) counts = ' · ' + b.free + '/' + b.total;
      const cls = b.is_for_sale === false ? ';opacity:.4' : '';
      row.style.cssText += cls;
      row.innerHTML =
        '<div><div class="name">' + (b.name || '') + badge + '</div>' +
        '<div class="meta">' + (b.category || '') + counts + '</div></div>';
      if (b.is_for_sale !== false) {{
        row.addEventListener('click', () => tap(b.name));
      }}
      list.appendChild(row);
    }}
  }}
}}
</script>
</body></html>"""


def _error_page(msg: str) -> str:
    return f"""<!doctype html><html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Webook Picker</title>
<style>body{{margin:0;background:#0f172a;color:#e5e7eb;font-family:-apple-system,sans-serif;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center}}
.card{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
border-radius:14px;padding:28px;max-width:340px;line-height:1.7}}</style></head>
<body><div class="card">⚠️<br>{html.escape(msg).replace(chr(10),'<br>')}</div></body></html>"""
