"""
Turkish Cargo  —  prefix 235
Direct POST to /api/proxy/onlineServices/shipmentTracking.
Response is an array of shipment events; each event may carry flightNo, etd, eta,
actualDatetime, station, actualPieces, actualWeight.
Falls back to Playwright if Akamai blocks the direct call.
"""
import asyncio
import re
from typing import Optional

import httpx
from fastapi import HTTPException
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .base import AirlineTracker, FlightLeg, PW_ARGS, TrackingResult, ULDItem, ULDResult

_TRACK_URL  = "https://www.turkishcargo.com/en/cargo-tracking"
_cargo_cache: dict[str, tuple[Optional[int], Optional[float]]] = {}  # awb → (pieces, weight_kg)
_TRACK_API  = "https://www.turkishcargo.com/api/proxy/onlineServices/shipmentTracking"
_UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "EN",
    "content-type": "application/json",
    "origin": "https://www.turkishcargo.com",
    "referer": "https://www.turkishcargo.com/en/cargo-tracking",
    "user-agent": _UA_DESKTOP,
}

# ── direct API attempt ────────────────────────────────────────────────────────

async def _direct_fetch(prefix: str, number: str) -> dict | None:
    """Try calling the tracking API directly without a browser."""
    body = {"trackingFilters": [{"shipmentPrefix": prefix, "masterDocumentNumber": number}]}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(_TRACK_API, json=body, headers=_HEADERS)
            if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
                data = resp.json()
                if isinstance(data, dict) and data.get("status") == "SUCCESS":
                    return data
    except Exception:
        pass
    return None


# ── Playwright fallback ───────────────────────────────────────────────────────

async def _playwright_fetch(prefix: str, number: str) -> list | dict:
    import json as _json
    result_holder: dict = {}

    async with Stealth().use_async(async_playwright()) as pw:
        # Prefer installed Chrome/Edge (better Akamai bypass)
        launch_opts = dict(headless=True, args=PW_ARGS)
        try:
            browser = await pw.chromium.launch(channel="chrome", **launch_opts)
        except Exception:
            try:
                browser = await pw.chromium.launch(channel="msedge", **launch_opts)
            except Exception:
                browser = await pw.chromium.launch(**launch_opts)
        try:
            ctx = await browser.new_context(
                user_agent=_UA_DESKTOP,
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()

            async def on_response(response):
                url = response.url
                if ("/shipmentTracking" in url or "/api/proxy/onlineServices" in url) and response.status == 200:
                    try:
                        body = await response.json()
                        if body:
                            result_holder["data"] = body
                    except Exception:
                        pass

            ctx.on("response", on_response)

            # Navigate to page (no params) — let Akamai scripts warm up
            await page.goto(_TRACK_URL, wait_until="domcontentloaded", timeout=45_000)

            # Simulate user interaction while Akamai sensor data collects (~7s)
            await page.wait_for_timeout(2500)
            for mx, my in [(300, 250), (500, 350), (700, 300), (450, 400), (600, 350)]:
                await page.mouse.move(mx, my)
                await asyncio.sleep(0.3)
            await page.wait_for_timeout(4000)

            # Strategy 1: trigger the API call from inside the browser using correct payload
            if "data" not in result_holder:
                body_fmt = {"trackingFilters": [{"shipmentPrefix": prefix, "masterDocumentNumber": number}]}
                try:
                    await page.evaluate(f"""async () => {{
                        try {{
                            await fetch('/api/proxy/onlineServices/shipmentTracking', {{
                                method: 'POST',
                                headers: {{
                                    'accept': 'application/json, text/plain, */*',
                                    'content-type': 'application/json',
                                    'accept-language': 'EN',
                                }},
                                body: JSON.stringify({_json.dumps(body_fmt)})
                            }});
                        }} catch(e) {{}}
                    }}""")
                    await asyncio.sleep(2)
                except Exception:
                    pass

            # Strategy 2: fill form properly (AWB number field is a TAG input — needs Enter)
            if "data" not in result_holder:
                # Fill prefix
                for sel in ["input[placeholder*='prefix']", "input[name*='prefix']",
                            "input[id*='prefix']", "input[placeholder*='Prefix']"]:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.triple_click()
                            await el.fill(prefix)
                            break
                    except Exception:
                        pass

                # Fill AWB number in tag-input: type then press Enter to confirm tag
                for sel in ["input[placeholder*='AWB']", "input[placeholder*='no']",
                            "input[name*='awbNo']", "input[name*='awbNum']",
                            "input[id*='awbNo']", "input[placeholder*='number']"]:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.fill(number)
                            await el.press("Enter")   # tag input requires Enter
                            await asyncio.sleep(0.5)
                            break
                    except Exception:
                        pass

                # Click Search button
                for btn_sel in [
                    "button[type='submit']", "button:has-text('Search')",
                    "button:has-text('Track')", "button:has-text('Sorgula')",
                    "[class*='search']", "[class*='btn-primary']",
                ]:
                    try:
                        el = page.locator(btn_sel).first
                        if await el.count():
                            await el.click(timeout=5000)
                            break
                    except Exception:
                        pass

                for _ in range(50):   # 25s
                    if "data" in result_holder:
                        break
                    await asyncio.sleep(0.5)

        finally:
            await browser.close()

    if "data" not in result_holder:
        raise HTTPException(504, f"Turkish Cargo: no response for {prefix}-{number}")
    return result_holder["data"]


# ── date parsing ──────────────────────────────────────────────────────────────

_MONTHS = {
    "jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
    "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12",
}

def _parse_tc_date(s: str) -> tuple[str, str]:
    """Parse '07-May-2026 09:40:00' or ISO → (time, date)."""
    if not s:
        return "", ""
    s = s.strip()
    # "07-May-2026 09:40:00"
    m = re.match(r'(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}:\d{2})', s)
    if m:
        mo = _MONTHS.get(m.group(2).lower(), "00")
        return m.group(4), f"{m.group(3)}/{mo}/{m.group(1).zfill(2)}"
    # ISO "2026-05-07T09:40:00"
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2})', s)
    if m:
        return m.group(4), f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return "", ""


# ── parsing ───────────────────────────────────────────────────────────────────

def _parse(awb: str, raw: dict) -> TrackingResult:
    if not isinstance(raw, dict) or raw.get("status") != "SUCCESS":
        raise HTTPException(404, f"AWB {awb} not found on Turkish Cargo")

    trackings = (raw.get("result") or {}).get("shipmentTrackings") or []
    if not trackings:
        raise HTTPException(404, f"AWB {awb} not found on Turkish Cargo")

    item = trackings[0]

    origin      = item.get("originCode") or ""
    destination = item.get("destinationCode") or ""
    status      = (item.get("actualStatus") or "").strip()

    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    try:
        pieces = int(item.get("pieces") or 0) or None
    except (ValueError, TypeError):
        pass
    try:
        weight_kg = float(item.get("weight") or 0) or None
    except (ValueError, TypeError):
        pass

    # Build actual dep/arr times from trackingDiagramDetails
    actual_dep: dict[str, str] = {}  # flightNo → actual dep datetime string
    actual_arr: dict[str, str] = {}  # flightNo → actual arr datetime string (from RCF)
    for ev in (item.get("trackingDiagramDetails") or []):
        fn  = (ev.get("flightNo") or "").strip()
        s   = ev.get("status", "")
        dt  = ev.get("actualDatetime") or ""
        if not fn or not dt:
            continue
        if s == "DEP":
            actual_dep.setdefault(fn, dt)
        elif s == "RCF":
            actual_arr.setdefault(fn, dt)

    # Build legs from bookingFlightDetails
    legs: list[FlightLeg] = []
    for seg in (item.get("bookingFlightDetails") or []):
        carrier = seg.get("carrierCode") or ""
        num     = str(seg.get("flightNumber") or "").strip()
        fn      = (carrier + num).strip()
        if not fn:
            continue

        from_ap = seg.get("originCode") or ""
        to_ap   = seg.get("destinationCode") or ""

        atd = actual_dep.get(fn, "")
        ata = actual_arr.get(fn, "")

        dep_raw = atd or seg.get("etd") or ""
        arr_raw = ata or seg.get("eta") or ""

        dep_time, dep_date = _parse_tc_date(dep_raw)
        arr_time, arr_date = _parse_tc_date(arr_raw)

        legs.append(FlightLeg(
            flight_no        = fn,
            from_airport     = from_ap,
            to_airport       = to_ap,
            departure_date   = dep_date,
            departure_time   = dep_time,
            departure_status = "actual" if atd else "scheduled",
            arrival_date     = arr_date,
            arrival_time     = arr_time,
            arrival_status   = "actual" if ata else "scheduled",
            flight_time      = "",
            pieces           = pieces,
            weight_kg        = weight_kg,
            flrs_id          = 1,
        ))

    return TrackingResult(
        awb             = awb,
        from_airport    = origin,
        from_name       = "",
        to_airport      = destination,
        to_name         = "",
        status          = status,
        status_code     = status,
        flights         = legs,
        total_pieces    = pieces,
        total_weight_kg = weight_kg,
    )


# ── tracker ───────────────────────────────────────────────────────────────────

class TurkishCargoTracker(AirlineTracker):
    prefixes = ["235"]
    name = "Turkish Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb = f"{prefix}-{number}"
        raw = await _direct_fetch(prefix, number)
        if raw is None:
            raw = await _playwright_fetch(prefix, number)
        result = _parse(awb, raw)
        _cargo_cache[awb] = (result.total_pieces, result.total_weight_kg)
        return result

    async def fetch_uld(
        self,
        prefix: str,
        awb_number: str,
        flight_no: str,
        departure: str,
        arrival: str,
        departure_date: str,
        flrs_id: int,
    ) -> ULDResult:
        ulds: list[ULDItem] = []
        awb = f"{prefix}-{awb_number}"
        cached = _cargo_cache.get(awb)
        if cached:
            pcs, wgt = cached
            if pcs or wgt:
                ulds = [ULDItem(uld=f"{wgt} kg" if wgt else "", pieces=pcs or 0)]
        return ULDResult(
            flight_no=flight_no,
            departure_date=departure_date,
            departure=departure,
            arrival=arrival,
            ulds=ulds,
        )
