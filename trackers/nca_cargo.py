"""
NCA (Nippon Cargo Airlines)  —  prefix 933
IBS iCargo portal at nca.aero.

The portal loads tracking data via AJAX after the initial page load.
We use Playwright-stealth to navigate the page, wait for the tracking
panel to populate, then parse the rendered HTML.

Two-step HTTP flow (replicated by Playwright automatically):
  GET  /icargoportal/portal/trackshipments?trkTxnValue=933-<awb>
  POST (AJAX, X-Requested-With: XMLHttpRequest) → fills #trackPanel
"""
import asyncio
import re
from typing import Optional

from bs4 import BeautifulSoup
from fastapi import HTTPException
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

# Cache: awb → {normalized_flight_no → [uld_string, ...]}
_nca_uld_cache: dict[str, dict[str, list[str]]] = {}

_ULD_RE = re.compile(r'\b([A-Z]{3}\d{4,6}[A-Z]{2,3})\b')

_PORTAL_BASE = "https://www.nca.aero/icargoportal/portal/trackshipments"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STATUS_MAP: dict[str, str] = {
    "booked":           "Booked",
    "accepted":         "Received",
    "received":         "Received",
    "shipment accepted":"Received",
    "pre-manifested":   "Manifested",
    "manifested":       "Manifested",
    "departed":         "Departed",
    "arrived":          "Arrived",
    "delivered":        "Delivered",
    "goods delivered":  "Delivered",
    "out for delivery": "Delivered",
}

_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _fn_key(fn: str) -> str:
    return fn.replace(" ", "").upper()


def _extract_ulds(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Parse History table rows and return {normalized_flight_no: [uld, ...]}."""
    result: dict[str, list[str]] = {}
    for row in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not cells:
            continue
        row_text = " ".join(cells)
        ulds = _ULD_RE.findall(row_text)
        if not ulds:
            continue
        fn_m = re.search(r'\b(KZ\s*\d{3,4})\b', row_text, re.I)
        key = _fn_key(fn_m.group(1)) if fn_m else "__all__"
        bucket = result.setdefault(key, [])
        for u in ulds:
            if u not in bucket:
                bucket.append(u)
    return result


def _parse_date(raw: str) -> tuple[str, str]:
    """
    Handles formats seen in iCargo portals:
      '15-05-2026 10:30' → ('10:30', '2026/05/15')
      '15-MAY-2026 10:30' → ('10:30', '2026/05/15')
      '2026-05-15 10:30'  → ('10:30', '2026/05/15')
    Returns (time, date) or ('', '').
    """
    s = raw.strip()
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})', s)
    if m:
        return m.group(4), f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = re.match(r'(\d{2})-(\d{2})-(\d{4})\s+(\d{2}:\d{2})', s)
    if m:
        return m.group(4), f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    m = re.match(r'(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}:\d{2})', s)
    if m:
        mo = _MONTHS.get(m.group(2).lower(), "00")
        return m.group(4), f"{m.group(3)}/{mo}/{m.group(1).zfill(2)}"
    return "", ""


async def _playwright_fetch(prefix: str, number: str) -> str:
    """
    Navigate to the NCA tracking page and return the populated
    #trackPanel HTML after the AJAX call completes.
    """
    panel_html: list[str] = []

    url = f"{_PORTAL_BASE}?trkTxnValue={prefix}-{number}"

    async with Stealth().use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = await browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )

            # Intercept the AJAX POST to ?fragments=trackPanel — this returns
            # the rendered tracking panel HTML (31 KB) after JavaScript fires.
            async def on_response(response):
                rurl = response.url
                if "trackshipments" not in rurl:
                    return
                if response.status != 200:
                    return
                if "fragments=trackPanel" not in rurl and response.request.method != "POST":
                    return
                try:
                    text = await response.text()
                    if len(text) > 500 and not panel_html:
                        panel_html.append(text)
                except Exception:
                    pass

            ctx.on("response", on_response)

            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            # Wait for the AJAX call to fire and fill #trackPanel
            # (the widget fires on DOMReady after requirejs finishes loading)
            for _ in range(40):
                if panel_html:
                    break
                await asyncio.sleep(0.5)

            # If response was not captured via listener, read rendered DOM directly
            if not panel_html:
                await page.wait_for_load_state("networkidle", timeout=15_000)
                panel_el = page.locator("#trackPanel")
                if await panel_el.count():
                    inner = await panel_el.inner_html(timeout=5_000)
                    if len(inner) > 500:
                        panel_html.append(inner)

        finally:
            await browser.close()

    return panel_html[0] if panel_html else ""


def _parse_icargo_date(raw: str) -> tuple[str, str]:
    """
    Parse NCA iCargo date cell: '07-May 2026 | 02:25'
    → ('02:25', '2026/05/07')
    """
    m = re.match(r'(\d{1,2})-([A-Za-z]{3})\s+(\d{4})\s*\|\s*(\d{2}:\d{2})', raw.strip())
    if m:
        mo = _MONTHS.get(m.group(2).lower(), "00")
        return m.group(4), f"{m.group(3)}/{mo}/{m.group(1).zfill(2)}"
    return "", ""


def _parse(awb: str, html: str) -> tuple[TrackingResult, dict[str, list[str]]]:
    soup = BeautifulSoup(html, "html.parser")

    # The AJAX response IS the #trackPanel content; use directly
    body_text = soup.get_text(" ", strip=True)
    if not body_text or len(body_text) < 20:
        raise HTTPException(404, f"AWB {awb} not found on NCA")

    # ── Route: "HKG>ORD" ────────────────────────────────────────────────
    origin = dest = ""
    m = re.search(r'\b([A-Z]{3})>([A-Z]{3})\b', body_text)
    if m:
        origin, dest = m.group(1), m.group(2)

    # ── Status: "in Goods Delivered status" ─────────────────────────────
    status = ""
    m = re.search(r'in\s+([\w\s]+?)\s+status', body_text, re.I)
    if m:
        raw_s  = m.group(1).strip()
        status = _STATUS_MAP.get(raw_s.lower(), raw_s)

    # ── Pieces / weight from "62 Pcs. | 402.0 Kg" ───────────────────────
    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    m = re.search(r'Shipment\s+(\d+)\s*Pcs?\.\s*\|\s*([\d.]+)\s*Kg', body_text, re.I)
    if m:
        try:
            pieces    = int(m.group(1))
            weight_kg = float(m.group(2))
        except (ValueError, TypeError):
            pass
    if pieces is None:
        m = re.search(r'(\d+)\s*Pcs?\.\s*\|\s*([\d.]+)\s*Kg', body_text, re.I)
        if m:
            try:
                pieces    = int(m.group(1))
                weight_kg = float(m.group(2))
            except (ValueError, TypeError):
                pass

    # ── Flight legs ───────────────────────────────────────────────────────
    # NCA iCargo shows flights in a "Flight Booking" table.
    # Text pattern per row (all on one line in get_text() output):
    #   KZ0202 HKG NRT 07-May 2026 | 02:25(STD) 07-May 2026 | 07:50(STA)
    #                               07-May 2026 | 03:09(ATD) 07-May 2026 | 08:29(ATA)
    #
    # We find the flight table by looking for "Flight Booking" header,
    # then regex-scan rows for the KZ flight pattern.
    flights: list[FlightLeg] = []

    # Date-time token: DD-Mon YYYY | HH:MM
    _DT = r'(\d{1,2}-[A-Za-z]{3}\s+\d{4}\s*\|\s*\d{2}:\d{2})'

    flight_pat = re.compile(
        r'\b(KZ\s*\d{3,4})\s+([A-Z]{3})\s+([A-Z]{3})\s+'
        + _DT + r'\s*\(STD\)'
        + r'(?:.*?' + _DT + r'\s*\(STA\))?'
        + r'.*?' + _DT + r'\s*\(ATD\)'
        + r'.*?' + _DT + r'\s*\(ATA\)',
        re.DOTALL | re.I,
    )

    # Only scan the "Flight Booking" section to avoid false positives
    fb_idx = body_text.find("Flight Booking")
    hist_idx = body_text.find("History")
    scan_text = (
        body_text[fb_idx:hist_idx] if fb_idx >= 0 and hist_idx > fb_idx
        else body_text[fb_idx:] if fb_idx >= 0
        else body_text
    )

    for m in flight_pat.finditer(scan_text):
        fn     = m.group(1).replace(" ", "")
        from_a = m.group(2)
        to_a   = m.group(3)
        # group(4)=STD, group(5)=STA (optional), group(6)=ATD, group(7)=ATA
        atd_raw = m.group(6)
        ata_raw = m.group(7)
        dep_time, dep_date = _parse_icargo_date(atd_raw) if atd_raw else ("", "")
        arr_time, arr_date = _parse_icargo_date(ata_raw) if ata_raw else ("", "")

        if not origin and from_a:
            origin = from_a
        if not dest and to_a:
            dest = to_a

        flights.append(FlightLeg(
            flight_no        = fn,
            from_airport     = from_a,
            to_airport       = to_a,
            departure_date   = dep_date,
            departure_time   = dep_time,
            departure_status = "actual" if dep_time else "scheduled",
            arrival_date     = arr_date,
            arrival_time     = arr_time,
            arrival_status   = "actual" if arr_time else "scheduled",
            flight_time      = "",
            flrs_id          = 1,
        ))

    if not status and not flights and not origin:
        raise HTTPException(404, f"AWB {awb} not found on NCA")

    uld_map = _extract_ulds(soup)

    return TrackingResult(
        awb             = awb,
        from_airport    = origin,
        from_name       = "",
        to_airport      = dest,
        to_name         = "",
        status          = status,
        status_code     = status,
        flights         = flights,
        total_pieces    = pieces,
        total_weight_kg = weight_kg,
    ), uld_map


class NCACargoTracker(AirlineTracker):
    prefixes = ["933"]
    name     = "NCA (Nippon Cargo Airlines)"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb  = f"{prefix}-{number}"
        html = await _playwright_fetch(prefix, number)
        if not html:
            raise HTTPException(504, f"NCA: no response for {awb}")
        result, uld_map = _parse(awb, html)
        if uld_map:
            _nca_uld_cache[awb] = uld_map
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
        awb     = f"{prefix}-{awb_number}"
        uld_map = _nca_uld_cache.get(awb, {})
        key     = _fn_key(flight_no)
        # Use flight-specific ULDs; only fall back to __all__ if no flight entries exist at all
        flight_keys = [k for k in uld_map if k != "__all__"]
        if uld_map.get(key):
            ulds_raw = uld_map[key]
        elif not flight_keys:
            ulds_raw = uld_map.get("__all__") or []
        else:
            ulds_raw = []
        ulds = [ULDItem(uld=u, pieces=0) for u in ulds_raw]
        return ULDResult(
            flight_no      = flight_no,
            departure_date = departure_date,
            departure      = departure,
            arrival        = arrival,
            ulds           = ulds,
        )
