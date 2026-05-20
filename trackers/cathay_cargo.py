"""
Cathay Pacific Cargo  —  prefix 160
Akamai + PerimeterX protected.
Uses playwright-stealth + Chromium to simulate user operations on the backend:
fill AWB number, click search, intercept the tracking API JSON response.
"""
import asyncio
import json
import pathlib
import re
from typing import Optional

from fastapi import HTTPException
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

_TRACK_PAGE = "https://www.cathaycargo.com/en-us/track-and-trace.html"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Module-level response cache: "prefix-number" → full API response
# Avoids re-running Playwright when fetch_uld() is called shortly after track()
_response_cache: dict[str, dict] = {}

# ULD code embedded in shipHistory.statusMsg, e.g. "LOADED IN PMC63045R7 AND FLIGHT DEPARTED"
_ULD_RE = re.compile(r'\b([A-Z]{2,3}\d{4,6}[A-Z0-9]{1,4})\b')


async def _playwright_fetch(prefix: str, number: str) -> dict:
    """
    Stealth Chromium: navigate to cathaycargo.com, fill AWB form,
    submit, and intercept the /cargo-shipments/v1/tracking JSON response.
    """
    result_holder: dict = {}

    async with Stealth().use_async(async_playwright()) as pw:
        launch_opts = dict(headless=True)
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
                if response.status != 200:
                    return
                url = response.url
                if "json" not in response.headers.get("content-type", ""):
                    return
                if any(x in url for x in (".js", ".css", ".png", ".svg", ".ico",
                                           "analytics", "gtm", "oauth", "font")):
                    return
                try:
                    body = await response.json()
                except Exception:
                    return
                # Unwrap list
                if isinstance(body, list) and body and isinstance(body[0], dict):
                    body = body[0]
                if not isinstance(body, dict):
                    return
                if "bookingStatus" not in body and "routing" not in body and "shipHistory" not in body:
                    return
                # Only overwrite if new data has more bookingStatus entries (prevents empty overwriting real)
                cur = result_holder.get("data") or {}
                if len(body.get("bookingStatus") or []) >= len((cur.get("bookingStatus") or [])):
                    result_holder["data"] = body

            page.on("response", on_response)

            await page.goto(_TRACK_PAGE, wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(3000)

            for consent_sel in [
                "button:has-text('Accept all')", "button:has-text('Accept')",
                "button:has-text('I agree')", "#onetrust-accept-btn-handler",
            ]:
                try:
                    await page.click(consent_sel, timeout=2000)
                    await page.wait_for_timeout(500)
                    break
                except Exception:
                    pass

            for sel in ["input[name*='airlineCodeField']", "input[placeholder*='code']",
                        "input[aria-label*='airline']", "input[id*='airlineCode']"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click(click_count=3)
                        await el.fill(prefix)
                        break
                except Exception:
                    pass

            for sel in ["input[name*='airWaybill']", "input[placeholder*='AWB']",
                        "input[placeholder*='waybill']", "input[aria-label*='waybill']",
                        "input[id*='airWaybill']"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(number)
                        break
                except Exception:
                    pass

            submitted = False
            for sel in ["input[type='submit']", "button[type='submit']",
                        "button:has-text('Track')", "button:has-text('Search')"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click(timeout=8000)
                        submitted = True
                        break
                except Exception:
                    pass
            if not submitted:
                await page.keyboard.press("Enter")

            # Wait up to 30s; only exit when bookingStatus is non-empty
            # (avoids stopping early on empty template responses)
            for _ in range(60):
                d = result_holder.get("data")
                if d and (d.get("bookingStatus") or d.get("shipHistory")):
                    break
                await asyncio.sleep(0.5)

        finally:
            await browser.close()

    if "data" not in result_holder:
        raise HTTPException(504, f"Cathay Cargo: no response received for {prefix}-{number}")

    raw = result_holder["data"]

    # Write raw response to debug file so we can inspect the full structure
    # (used to confirm ULD field names — safe to keep in production)
    try:
        debug_path = pathlib.Path(__file__).parent.parent / "cathay_debug.json"
        debug_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass

    return raw


# ── tracker ───────────────────────────────────────────────────────────────

class CathayCargoTracker(AirlineTracker):
    prefixes = ["160"]
    name = "Cathay Pacific Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb_display = f"{prefix}-{number}"
        data = await _playwright_fetch(prefix, number)
        _response_cache[awb_display] = data
        return self._parse(awb_display, data)

    # ── response parsing ─────────────────────────────────────────────────

    def _parse(self, awb: str, data: dict) -> TrackingResult:
        if isinstance(data, list):
            data = data[0] if data else {}

        # Routing: overall origin / destination
        routing = data.get("routing") or []
        origin = routing[0].get("origin", "") if routing else ""
        dest   = routing[-1].get("destination", "") if routing else ""

        booking_statuses = data.get("bookingStatus") or []

        # Overall status: first bookingStatus entry without a "flight" key
        status = ""
        for entry in booking_statuses:
            if not entry.get("flight"):
                status = entry.get("status", "")
                break

        # Flight legs: bookingStatus entries that carry a "flight" key
        flights = [
            self._parse_booking_status(e)
            for e in booking_statuses
            if e.get("flight")
        ]

        # Weight
        weight_raw = data.get("weight")
        weight_kg: Optional[float] = None
        if isinstance(weight_raw, dict):
            weight_kg = weight_raw.get("value")
        elif isinstance(weight_raw, (int, float)):
            weight_kg = float(weight_raw)

        # Pieces
        pieces_raw = data.get("pieces")
        pieces: Optional[int] = None
        if pieces_raw is not None:
            try:
                pieces = int(pieces_raw)
            except (ValueError, TypeError):
                pass

        return TrackingResult(
            awb=awb,
            from_airport=origin,
            from_name="",
            to_airport=dest,
            to_name="",
            status=status,
            status_code=status,
            flights=flights,
            total_pieces=pieces,
            total_weight_kg=weight_kg,
        )

    def _parse_booking_status(self, entry: dict) -> FlightLeg:
        # "flight": "CX072/10MAY"  →  flight_no = "CX072"
        flight_raw = entry.get("flight", "")
        flight_no  = flight_raw.split("/")[0] if flight_raw else ""

        # From / To airports embedded in status string: "HKG TO MIA Confirmed..."
        from_ap, to_ap = _parse_route_from_status(entry.get("status", ""))

        # Departure: ATD (actual) preferred, fall back to STD (scheduled)
        dep_iso    = entry.get("ATD") or entry.get("STD") or ""
        dep_status = "actual" if entry.get("ATD") else "scheduled"
        dep_time, dep_date = _split_iso(dep_iso)

        # Arrival: ATA (actual) preferred, fall back to STA (scheduled)
        arr_iso    = entry.get("ATA") or entry.get("STA") or ""
        arr_status = "actual" if entry.get("ATA") else "scheduled"
        arr_time, arr_date = _split_iso(arr_iso)

        return FlightLeg(
            flight_no        = flight_no,
            from_airport     = from_ap,
            to_airport       = to_ap,
            departure_date   = dep_date,
            departure_time   = dep_time,
            departure_status = dep_status,
            arrival_date     = arr_date,
            arrival_time     = arr_time,
            arrival_status   = arr_status,
            flight_time      = "",
            flrs_id          = 1,   # ULD fetch enabled for all Cathay legs
        )

    # ── ULD lookup ────────────────────────────────────────────────────────

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
        awb_key = f"{prefix}-{awb_number}"
        data = _response_cache.get(awb_key)
        if not data:
            # Cache miss: re-run Playwright (e.g. after server restart)
            data = await _playwright_fetch(prefix, awb_number)
            _response_cache[awb_key] = data

        ulds = _extract_uld(data, flight_no)
        return ULDResult(
            flight_no=flight_no,
            departure_date=departure_date,
            departure=departure,
            arrival=arrival,
            ulds=ulds,
        )


# ── helpers ───────────────────────────────────────────────────────────────

def _extract_uld(data: dict, flight_no: str) -> list[ULDItem]:
    """
    ULD is embedded in shipHistory[].statusMsg text, e.g.
    "LOADED IN PMC63045R7 AND FLIGHT DEPARTED".
    Flight is identified by shipHistory[].flightInfo = "CX3194/17May".
    """
    ulds: list[ULDItem] = []
    seen: set[str] = set()

    for ev in data.get("shipHistory") or []:
        if ev.get("status") not in ("DEP", "MAN", "RCF"):
            continue
        # flightInfo = "CX3194/17May" → flight number = "CX3194"
        flt_info = ev.get("flightInfo") or ""
        flt_no = flt_info.split("/")[0].strip().upper()
        if flt_no and flt_no != flight_no.upper():
            continue
        # Extract ULD code(s) from statusMsg
        status_msg = ev.get("statusMsg", "")
        for m in _ULD_RE.finditer(status_msg):
            uld = m.group(1)
            if uld not in seen:
                seen.add(uld)
                try:
                    pcs = int(ev.get("pieces") or 0)
                except (ValueError, TypeError):
                    pcs = 0
                ulds.append(ULDItem(uld=uld, pieces=pcs))

    return ulds


def _parse_route_from_status(status: str) -> tuple[str, str]:
    """Extract ('HKG', 'MIA') from 'HKG TO MIA Confirmed (allotment)'."""
    m = re.search(r'\b([A-Z]{3})\s+TO\s+([A-Z]{3})\b', status)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _split_iso(iso: str) -> tuple[str, str]:
    """Return (HH:MM, YYYY/MM/DD) from an ISO-8601 string, or ('','')."""
    if not iso:
        return "", ""
    try:
        date_part, rest = iso.split("T")
        return rest[:5], date_part.replace("-", "/")
    except Exception:
        return "", ""
