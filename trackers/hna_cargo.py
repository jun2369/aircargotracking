"""
HNA Cargo  —  prefix 871
ASP.NET WebForms with image CAPTCHA at hnacargo.com.
Uses Playwright-stealth to:
  1. Navigate to the tracking page.
  2. Fetch the CAPTCHA image and solve with ddddocr.
  3. Fill in the AWB and CAPTCHA, submit the form.
  4. Parse the HTML result from #MainContent_divContent.
"""
import asyncio
import re
from typing import Optional

from bs4 import BeautifulSoup
from fastapi import HTTPException
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .base import AirlineTracker, FlightLeg, PW_SEMAPHORE, TrackingResult

_TRACK_URL = "https://www.hnacargo.com/Portal2/AwbSearch.aspx"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STATUS_MAP: dict[str, str] = {
    "bkd": "Booked",
    "foh": "Received",
    "rcs": "Received",
    "man": "Manifested",
    "dep": "Departed",
    "出发": "Departed",
    "awd": "Arrived",
    "arr": "Arrived",
    "rcf": "Arrived",
    "nfd": "Arrived",
    "dlv": "Delivered",
    "已提货": "Delivered",
}


def _extract_iata(text: str) -> str:
    """'NKG(南京)' → 'NKG'"""
    m = re.match(r'^([A-Z]{3})', text.strip())
    return m.group(1) if m else ""


def _parse_ymd(raw: str) -> tuple[str, str]:
    """'2026-05-10 05:55:00' or '2026-05-10 05:55' → ('05:55', '2026/05/10')"""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})', (raw or "").strip())
    if m:
        return m.group(4), f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return "", ""


def _parse_flight_info(info: str) -> dict:
    """
    Parse the '航班信息' cell text.
    Formats observed:
      'Y87459,南京-安克雷奇,实际起飞：2026-05-10 05:55:00,实际到达：2026-05-09 22:26:00'
      '离港航班：Y87459/5月10日/NKG-LAX,实际出发06:11,预计到达00:00'
    """
    result: dict = {}
    # Flight number
    fn_m = re.search(r'\b([A-Z]{1,2}\d{3,5})\b', info)
    if fn_m:
        result["flight_no"] = fn_m.group(1)
    # Actual departure
    dep_m = re.search(r'实际(?:起飞|出发)[：:]?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)', info)
    if dep_m:
        result["atd"] = dep_m.group(1)
    # Actual arrival
    arr_m = re.search(r'实际(?:到达)[：:]?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)', info)
    if arr_m:
        result["ata"] = arr_m.group(1)
    return result


def _has_results(html: str) -> bool:
    """Check if the page has actual tracking results (not just an empty form)."""
    from bs4 import BeautifulSoup as _BS
    soup = _BS(html, "html.parser")
    div  = soup.find(id="MainContent_divContent")
    if not div:
        return False
    return bool(div.find_all("table", class_=re.compile(r"result-list")))


async def _playwright_fetch(prefix: str, number: str) -> str:
    awb = f"{prefix}-{number}"

    async with PW_SEMAPHORE:
      async with Stealth().use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = await browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()
            await page.goto(_TRACK_URL, wait_until="domcontentloaded", timeout=40_000)
            await asyncio.sleep(1.5)

            # Try up to 4 times (CAPTCHA OCR may fail on first attempt)
            for attempt in range(4):
                # Fill AWB
                await page.locator("#txtAwbCode").fill(awb)

                # Solve CAPTCHA with ddddocr
                captcha_code = ""
                try:
                    import ddddocr
                    captcha_bytes = await page.evaluate("""async () => {
                        const r = await fetch('/VerifyCode.aspx?s=login&r=' + Math.random(),
                                              {credentials: 'include'});
                        const buf = await r.arrayBuffer();
                        return Array.from(new Uint8Array(buf));
                    }""")
                    img_bytes = bytes(captcha_bytes)
                    ocr = ddddocr.DdddOcr(show_ad=False)
                    captcha_code = ocr.classification(img_bytes)
                    captcha_code = re.sub(r'[^A-Za-z0-9]', '', captcha_code).strip()
                except Exception:
                    pass

                await page.locator("#MainContent_txtVerifyCode").fill(captcha_code)
                await page.locator("#lkbtnSearch").click()
                await asyncio.sleep(3)

                content = await page.content()
                if _has_results(content):
                    return content

                # Wrong CAPTCHA — refresh page and retry
                if attempt < 3:
                    await page.reload(wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(1)

            return await page.content()

        finally:
            await browser.close()

    return ""


def _parse(awb: str, html: str) -> TrackingResult:
    soup = BeautifulSoup(html, "html.parser")
    content_div = (
        soup.find(id="MainContent_divContent")
        or soup.find(id=re.compile(r"divContent", re.I))
    )
    if not content_div:
        raise HTTPException(404, f"AWB {awb} not found on HNA Cargo")

    tables = content_div.find_all("table", class_=re.compile(r"result-list"))
    if not tables:
        raise HTTPException(404, f"AWB {awb} not found on HNA Cargo")

    # ── Route / header from first table ─────────────────────────────────
    origin = dest = ""
    pieces: Optional[int] = None
    weight_kg: Optional[float] = None

    header_table = tables[0]
    tds = header_table.find_all("td")
    # Parse label/value pairs — each pair is two consecutive <td>s
    for i, td in enumerate(tds):
        label = td.get_text(strip=True)
        value = tds[i + 1].get_text(strip=True) if i + 1 < len(tds) else ""

        # Route cell contains "→"
        if "→" in label or ("→" in value):
            t = label if "→" in label else value
            sep = "→"
            parts = [p.strip() for p in t.split(sep)]
            origin = _extract_iata(parts[0]) if parts else ""
            dest   = _extract_iata(parts[-1]) if len(parts) > 1 else ""

        # Weight labels (Chinese): 计费重量, 毛重, 重量
        elif any(kw in label for kw in ("重量", "毛重", "Gross", "Weight")):
            if weight_kg is None:
                try:
                    weight_kg = float(re.sub(r'[^\d.]', '', value))
                except (ValueError, TypeError):
                    pass

        # Pieces labels: 件数, Pieces
        elif any(kw in label for kw in ("件数", "Pieces", "件")):
            if pieces is None:
                try:
                    pieces = int(re.sub(r'[^\d]', '', value))
                except (ValueError, TypeError):
                    pass

    # ── Status events from second table ─────────────────────────────────
    status = ""
    flights: list[FlightLeg] = []

    if len(tables) >= 2:
        status_table = tables[1]
        rows = status_table.find_all("tr")

        for tr in rows:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            # Skip header row
            if cells[0] in ("站点", "Station", ""):
                continue

            station_raw = cells[0]
            time_raw    = cells[1] if len(cells) > 1 else ""
            status_raw  = cells[2] if len(cells) > 2 else ""
            flight_info = cells[3] if len(cells) > 3 else ""
            pcs_raw     = cells[4] if len(cells) > 4 else ""
            wt_raw      = cells[5] if len(cells) > 5 else ""

            # Extract status code from "DEP(已离港)" or "出发"
            sc_m = re.match(r'^([A-Z]{3})', status_raw)
            sc   = sc_m.group(1).lower() if sc_m else status_raw.lower()

            # Always overwrite — last event in the table wins (chronological order)
            mapped = _STATUS_MAP.get(sc, "")
            if mapped:
                status = mapped

            # Build flight leg from DEP events
            if sc in ("dep", "出发"):
                fi   = _parse_flight_info(flight_info)
                fn   = fi.get("flight_no", "")
                if not fn:
                    continue

                from_ap = _extract_iata(station_raw) or origin
                atd_raw = fi.get("atd", "")
                ata_raw = fi.get("ata", "")
                dep_time, dep_date = _parse_ymd(atd_raw) if atd_raw else _parse_ymd(time_raw)
                arr_time, arr_date = _parse_ymd(ata_raw)

                # Try to extract destination from flight info "NKG-LAX"
                to_ap = dest
                route_m = re.search(r'([A-Z]{3})-([A-Z]{3})', flight_info)
                if route_m:
                    to_ap = route_m.group(2)

                # Pieces and weight from this row
                row_pcs: Optional[int] = None
                row_wt:  Optional[float] = None
                try:
                    row_pcs = int(pcs_raw) if pcs_raw else None
                except (ValueError, TypeError):
                    pass
                try:
                    row_wt = float(wt_raw) if wt_raw else None
                except (ValueError, TypeError):
                    pass

                flights.append(FlightLeg(
                    flight_no        = fn,
                    from_airport     = from_ap,
                    to_airport       = to_ap,
                    departure_date   = dep_date,
                    departure_time   = dep_time,
                    departure_status = "actual" if atd_raw else "scheduled",
                    arrival_date     = arr_date,
                    arrival_time     = arr_time,
                    arrival_status   = "actual" if ata_raw else "scheduled",
                    flight_time      = "",
                    pieces           = row_pcs,
                    weight_kg        = row_wt,
                    flrs_id          = 0,
                ))

            # Update pieces/weight from DLV or last event if not yet set
            if not pieces and pcs_raw:
                try:
                    pieces = int(pcs_raw)
                except (ValueError, TypeError):
                    pass
            if not weight_kg and wt_raw:
                try:
                    weight_kg = float(wt_raw)
                except (ValueError, TypeError):
                    pass

    if not status and not origin:
        raise HTTPException(404, f"AWB {awb} not found on HNA Cargo")

    # Deduplicate flights (same flight_no + from_airport)
    seen: set[str] = set()
    unique_flights: list[FlightLeg] = []
    for f in flights:
        key = f"{f.flight_no}-{f.from_airport}"
        if key not in seen:
            seen.add(key)
            unique_flights.append(f)

    return TrackingResult(
        awb             = awb,
        from_airport    = origin,
        from_name       = "",
        to_airport      = dest,
        to_name         = "",
        status          = status,
        status_code     = status,
        flights         = unique_flights,
        total_pieces    = pieces,
        total_weight_kg = weight_kg,
    )


class HNACargoTracker(AirlineTracker):
    prefixes = ["871"]
    name     = "HNA Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb  = f"{prefix}-{number}"
        html = await _playwright_fetch(prefix, number)
        if not html:
            raise HTTPException(504, f"HNA Cargo: no response for {awb}")
        return _parse(awb, html)
