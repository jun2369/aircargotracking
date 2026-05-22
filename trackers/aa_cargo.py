"""
American Airlines Cargo  —  prefix 001
Direct POST to /api/tracking/awbs/ with:
  {"airwayBills": [{"awbCode": "001", "awbNumber": "XXXXXXXX", "awbId": "0"}]}
Falls back to Playwright if Akamai blocks.
"""
import asyncio
import json
import re
from typing import Optional

import httpx
from fastapi import HTTPException
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .base import AirlineTracker, FlightLeg, PW_ARGS, PW_SEMAPHORE_HEAVY as PW_SEMAPHORE, TrackingResult, ULDItem, ULDResult

_TRACK_PAGE    = "https://www.aacargo.com/AACargo/tracking?awbCode0={prefix}&awbNum0={number}"
_API_PATH      = "/api/tracking/awbs/"
_TRACK_API     = "https://www.aacargo.com/api/tracking/awbs/"
_17TRACK_URL   = "https://t.17track.net/en#nums={awb}"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://www.aacargo.com",
    "referer": "https://www.aacargo.com/AACargo/tracking",
    "user-agent": _UA,
}

# Cache: awb_display → full AA Cargo API response list
_response_cache: dict[str, list] = {}
# Cache: awb_display → 17track shipment dict (for fetch_uld cargo info)
_17track_cache: dict[str, dict] = {}
# Cache: awb_display → (pieces, weight_kg) for direct/playwright path
_aa_cargo_cache: dict[str, tuple] = {}


async def _direct_fetch(prefix: str, number: str) -> list | None:
    """Try direct POST — works if Akamai allows same-origin-style calls."""
    body = {"airwayBills": [{"awbCode": prefix, "awbNumber": number, "awbId": "0"}]}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(_TRACK_API, json=body, headers=_HEADERS)
            if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
                data = resp.json()
                if isinstance(data, list) and data and not data[0].get("error"):
                    return data
    except Exception:
        pass
    return None


async def _17track_fetch(prefix: str, number: str) -> dict | None:
    """Fetch via t.17track.net — bypasses Akamai on AA Cargo entirely.

    17track may return shipment code 4 (fetching) on first query for a new AWB,
    then code 200 after ~10s once it retrieves data from the carrier.
    We wait up to 25s to catch that second response.
    """
    awb = f"{prefix}-{number}"
    result_holder: dict = {}

    async with PW_SEMAPHORE:
      async with Stealth().use_async(async_playwright()) as pw:
        try:
            browser = await pw.chromium.launch(channel="chrome", headless=True, args=PW_ARGS)
        except Exception:
            browser = await pw.chromium.launch(headless=True, args=PW_ARGS)
        try:
            ctx = await browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()

            async def on_response(response):
                if "17track.net" not in response.url or response.status != 200:
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                if data.get("meta", {}).get("code") == 200:
                    shipments = data.get("shipments") or []
                    if shipments and shipments[0].get("code") == 200:
                        result_holder["data"] = data
                    elif "pending" not in result_holder:
                        result_holder["pending"] = data

            page.on("response", on_response)

            await page.goto(
                _17TRACK_URL.format(awb=awb),
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            # Wait up to 45s — 17track polls the carrier; second response arrives ~10s later
            for _ in range(90):
                if "data" in result_holder:
                    break
                await asyncio.sleep(0.5)

        finally:
            await browser.close()

    return result_holder.get("data") or result_holder.get("pending")


async def _playwright_fetch(prefix: str, number: str) -> list:
    result_holder: dict = {}

    async with PW_SEMAPHORE:
      async with Stealth().use_async(async_playwright()) as pw:
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
                user_agent=_UA,
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()

            async def on_response(response):
                if _API_PATH in response.url and response.status == 200:
                    try:
                        result_holder["data"] = await response.json()
                    except Exception:
                        pass

            page.on("response", on_response)

            # Navigate directly with search params — page should auto-submit on load
            track_url = _TRACK_PAGE.format(prefix=prefix, number=number)
            await page.goto(track_url, wait_until="domcontentloaded", timeout=45_000)

            # Wait up to 15s for auto-submit; mouse moves warm up Akamai sensor data
            for i in range(30):
                if "data" in result_holder:
                    break
                await page.mouse.move(200 + (i % 8) * 60, 300 + (i % 3) * 40)
                await asyncio.sleep(0.5)

            # ctx.request.post() carries ALL browser cookies (incl. Akamai _abck/bm_sz)
            # unlike page.evaluate(fetch()) which may miss httpOnly cookies
            if "data" not in result_holder:
                payload = {"airwayBills": [{"awbCode": prefix, "awbNumber": number, "awbId": "0"}]}
                try:
                    resp = await ctx.request.post(
                        _TRACK_API,
                        data=json.dumps(payload),
                        headers={
                            "accept": "application/json, text/plain, */*",
                            "content-type": "application/json",
                            "origin": "https://www.aacargo.com",
                            "referer": track_url,
                        },
                    )
                    if resp.ok:
                        body = await resp.json()
                        if isinstance(body, list) and body and not body[0].get("error"):
                            result_holder["data"] = body
                except Exception:
                    pass

            # form fill fallback — visible click triggers the page's own tracking call
            if "data" not in result_holder:
                for sel in [
                    "button.btn-search", "button[type='submit']",
                    "input[type='submit']", "button:has-text('Track')",
                    "button:has-text('Search')",
                ]:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        break
                else:
                    await page.keyboard.press("Enter")

                for _ in range(30):
                    if "data" in result_holder:
                        break
                    await asyncio.sleep(0.5)

        finally:
            await browser.close()

    if "data" not in result_holder:
        raise HTTPException(504, f"AA Cargo: no API response received for {prefix}-{number}")

    return result_holder["data"]


# ── parsing ───────────────────────────────────────────────────────────────

def _parse(awb: str, data: list) -> TrackingResult:
    if not data:
        raise HTTPException(404, f"AWB {awb} not found on AA Cargo")

    item = data[0] if isinstance(data, list) else data

    if item.get("error"):
        raise HTTPException(404, f"AWB {awb} not found on AA Cargo")

    origin      = item.get("aaOriginAirportCode") or item.get("originAirportCode") or ""
    destination = item.get("aaDestinationAirportCode") or item.get("destinationAirportCode") or ""

    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    try:
        pieces = int(item.get("numberOfPieces") or 0) or None
    except (ValueError, TypeError):
        pass
    try:
        weight_kg = float(item.get("grossWeight") or 0) or None
    except (ValueError, TypeError):
        pass

    # Current status
    status = (
        item.get("statusMessage") or item.get("latestStatus")
        or item.get("currentAWBStatus") or ""
    ).strip()

    # Flight segments
    flights = _build_legs(item)

    return TrackingResult(
        awb=awb,
        from_airport=origin,
        from_name="",
        to_airport=destination,
        to_name="",
        status=status,
        status_code=status,
        flights=flights,
        total_pieces=pieces,
        total_weight_kg=weight_kg,
    )


def _parse_17track(awb: str, data: dict) -> TrackingResult:
    shipments = data.get("shipments") or []
    if not shipments:
        raise HTTPException(404, f"AWB {awb} not found on 17track")
    s_code = shipments[0].get("code")
    if s_code != 200:
        if s_code in (4, 100):
            raise HTTPException(504, f"AA Cargo data for {awb} is still loading, retry in a few seconds")
        raise HTTPException(404, f"AWB {awb} not found on 17track (code {s_code})")

    s = shipments[0]
    shipment = s.get("shipment") or {}
    awb_info = shipment.get("awb_info") or {}
    latest_status = shipment.get("latest_status") or {}

    # 17track sync failure: code=200 but no actual data retrieved from carrier
    if (latest_status.get("status") == "NotFound"
            and not awb_info.get("origin_iata")
            and not awb_info.get("destination_iata")):
        raise HTTPException(504, f"AA Cargo data for {awb} temporarily unavailable (17track sync failed), retry later")

    origin      = awb_info.get("origin_iata") or ""
    destination = awb_info.get("destination_iata") or ""

    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    try:
        pieces = int(awb_info.get("pieces") or 0) or None
    except (ValueError, TypeError):
        pass
    try:
        weight_kg = float(awb_info.get("weight") or 0) or None
    except (ValueError, TypeError):
        pass

    status = (latest_status.get("status") or "").strip()

    flights = _build_legs_17track(shipment)

    return TrackingResult(
        awb=awb,
        from_airport=origin,
        from_name="",
        to_airport=destination,
        to_name="",
        status=status,
        status_code=status,
        flights=flights,
        total_pieces=pieces,
        total_weight_kg=weight_kg,
    )


def _build_legs_17track(shipment: dict) -> list[FlightLeg]:
    transport_infos = shipment.get("awb_transport_infos") or []
    awb_info = shipment.get("awb_info") or {}

    # AWB-level totals as fallback when segment has no pieces/weight
    total_pcs: Optional[int] = None
    total_wgt: Optional[float] = None
    try:
        total_pcs = int(awb_info.get("pieces") or 0) or None
    except (ValueError, TypeError):
        pass
    try:
        total_wgt = float(awb_info.get("weight") or 0) or None
    except (ValueError, TypeError):
        pass

    legs: list[FlightLeg] = []
    for seg in transport_infos:
        fn = (seg.get("flight_no") or "").strip()
        from_ap = seg.get("origin_iata") or ""
        to_ap   = seg.get("destination_iata") or ""

        if not fn or (not from_ap and not to_ap):
            continue

        atd = seg.get("atd") or ""
        ata = seg.get("ata") or ""
        std = seg.get("std") or ""
        sta = seg.get("sta") or ""

        dep_raw    = atd or std
        arr_raw    = ata or sta
        dep_status = "actual" if atd else "scheduled"
        arr_status = "actual" if ata else "scheduled"

        dep_time, dep_date = _split_iso(dep_raw)
        arr_time, arr_date = _split_iso(arr_raw)

        seg_pcs: Optional[int] = None
        seg_wgt: Optional[float] = None
        try:
            v = int(seg.get("pieces") or 0)
            seg_pcs = v if v > 0 else total_pcs
        except (ValueError, TypeError):
            seg_pcs = total_pcs
        try:
            v = float(seg.get("weight") or 0)
            seg_wgt = v if v > 0 else total_wgt
        except (ValueError, TypeError):
            seg_wgt = total_wgt

        legs.append(FlightLeg(
            flight_no        = fn,
            from_airport     = from_ap,
            to_airport       = to_ap,
            departure_date   = dep_date,
            departure_time   = dep_time,
            departure_status = dep_status,
            arrival_date     = arr_date,
            arrival_time     = arr_time,
            arrival_status   = arr_status,
            flight_time      = "",
            pieces           = seg_pcs,
            weight_kg        = seg_wgt,
            flrs_id          = 1,
        ))
    return legs


def _build_legs(item: dict) -> list[FlightLeg]:
    # Build actual-time lookup from tracking history (DEP/ARR events keyed by fltNum)
    history = item.get("airWayBillTrackingHistoryDtos") or []
    actual_dep: dict[str, str] = {}   # fltNum → timeStampDate of DEP event
    actual_arr: dict[str, str] = {}   # fltNum → timeStampDate of ARR event
    for ev in history:
        code = ev.get("statusCode", "")
        flt = str(ev.get("fltNum") or "").strip()
        ts = ev.get("timeStampDate") or ""
        if flt and ts:
            if code == "DEP":
                actual_dep.setdefault(flt, ts)
            elif code == "ARR":
                actual_arr.setdefault(flt, ts)

    # Try new field name first, then legacy, then booked-flight list
    segments = (
        item.get("flightSegments")
        or item.get("segments")
        or item.get("bookedFlightDetailsList")
        or []
    )
    legs: list[FlightLeg] = []
    for seg in segments:
        carrier = seg.get("carrierCode") or seg.get("airlineCode") or seg.get("airlineIATACode") or ""
        flt_num = str(seg.get("flightNumber") or seg.get("fltNum") or "").strip()
        fn = (carrier + flt_num).strip()
        from_ap = (
            seg.get("originAirportCode") or seg.get("departureAirport")
            or seg.get("departureStationCode") or ""
        )
        to_ap = (
            seg.get("destinationAirportCode") or seg.get("arrivalAirport")
            or seg.get("scheduledArrivalAirport") or ""
        )

        # Prefer actual times from history, fall back to scheduled
        dep_raw = (actual_dep.get(flt_num)
                   or seg.get("actualDepartureDateTime")
                   or seg.get("scheduledDepartureDateTime")
                   or seg.get("scheduledDepartureDate") or "")
        arr_raw = (actual_arr.get(flt_num)
                   or seg.get("actualArrivalDateTime")
                   or seg.get("scheduledArrivalDateTime")
                   or seg.get("scheduledArrivalDate") or "")
        dep_status = "actual" if (actual_dep.get(flt_num) or seg.get("actualDepartureDateTime")) else "scheduled"
        arr_status = "actual" if (actual_arr.get(flt_num) or seg.get("actualArrivalDateTime")) else "scheduled"

        dep_time, dep_date = _split_iso(dep_raw)
        arr_time, arr_date = _split_iso(arr_raw)

        if fn:
            legs.append(FlightLeg(
                flight_no        = fn,
                from_airport     = from_ap,
                to_airport       = to_ap,
                departure_date   = dep_date,
                departure_time   = dep_time,
                departure_status = dep_status,
                arrival_date     = arr_date,
                arrival_time     = arr_time,
                arrival_status   = arr_status,
                flight_time      = "",
                flrs_id          = 1,
            ))
    return legs


def _split_iso(iso: str) -> tuple[str, str]:
    """Return (HH:MM, YYYY/MM/DD) from ISO or MM/DD/YYYY HH:MM AM/PM strings."""
    if not iso:
        return "", ""
    try:
        # ISO: "2026-05-13T08:47:00..."
        if "T" in iso:
            date_part, rest = iso.split("T", 1)
            return rest[:5], date_part.replace("-", "/")
        # AA Cargo: "05/13/2026 08:55 AM"
        m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)?', iso, re.I)
        if m:
            mm, dd, yyyy, hh, mins, ampm = m.groups()
            h = int(hh)
            if ampm:
                if ampm.upper() == "PM" and h != 12:
                    h += 12
                elif ampm.upper() == "AM" and h == 12:
                    h = 0
            return f"{h:02d}:{mins}", f"{yyyy}/{mm.zfill(2)}/{dd.zfill(2)}"
        # History timeStampDate: "13-May-2026 07:47:00"
        _MONTHS = {"jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
                   "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"}
        m = re.match(r'(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}:\d{2})', iso)
        if m:
            dd, mon, yyyy, hhmm = m.groups()
            mo = _MONTHS.get(mon.lower(), "00")
            return hhmm, f"{yyyy}/{mo}/{dd.zfill(2)}"
    except Exception:
        pass
    return "", ""


# ── tracker ───────────────────────────────────────────────────────────────

class AACargoTracker(AirlineTracker):
    prefixes = ["001"]
    name = "American Airlines Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb = f"{prefix}-{number}"
        # Fast path: direct AA Cargo API (blocked by Akamai in practice)
        data = await _direct_fetch(prefix, number)
        if data is not None:
            _response_cache[awb] = data
            result = _parse(awb, data)
            _aa_cargo_cache[awb] = (result.total_pieces, result.total_weight_kg)
            return result
        # Primary path: 17track as proxy (reliable, bypasses Akamai)
        data17 = await _17track_fetch(prefix, number)
        if data17 is not None:
            shipments = data17.get("shipments") or []
            if shipments:
                _17track_cache[awb] = shipments[0]
            return _parse_17track(awb, data17)
        # Last resort: direct Playwright on aacargo.com
        data = await _playwright_fetch(prefix, number)
        _response_cache[awb] = data
        result = _parse(awb, data)
        _aa_cargo_cache[awb] = (result.total_pieces, result.total_weight_kg)
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

        # Try 17track cache first
        cached17 = _17track_cache.get(awb)
        if cached17:
            awb_info = (cached17.get("shipment") or {}).get("awb_info") or {}
            try:
                pcs = int(awb_info.get("pieces") or 0)
            except (ValueError, TypeError):
                pcs = 0
            try:
                wgt = float(awb_info.get("weight") or 0)
            except (ValueError, TypeError):
                wgt = 0.0
            if pcs or wgt:
                ulds = [ULDItem(uld=f"{wgt} kg" if wgt else "", pieces=pcs)]

        # Fallback: direct/playwright cache
        if not ulds:
            cached_aa = _aa_cargo_cache.get(awb)
            if cached_aa:
                pcs, wgt = cached_aa
                if pcs or wgt:
                    ulds = [ULDItem(uld=f"{wgt} kg" if wgt else "", pieces=pcs or 0)]

        return ULDResult(
            flight_no=flight_no,
            departure_date=departure_date,
            departure=departure,
            arrival=arrival,
            ulds=ulds,
        )
