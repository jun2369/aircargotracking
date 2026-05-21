"""
China Southern Cargo  —  prefix 784
ASP.NET WebForms with slide CAPTCHA (jquery.verify plugin) at tang.csair.com.
Uses Playwright-stealth to:
  1. Navigate and fill AWB fields.
  2. Trigger the search (opens slide-CAPTCHA dialog).
  3. Solve the slider via ddddocr slide_match + Playwright mouse drag.
  4. Intercept the ASP.NET UpdatePanel PostBack HTML response.
No ULD data available (flrs_id=0 on all legs).
"""
import asyncio
import re
from typing import Optional

from fastapi import HTTPException
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

from .base import AirlineTracker, FlightLeg, PW_ARGS, PW_SEMAPHORE, TrackingResult, ULDItem, ULDResult

_FORM_URL = (
    "https://tang.csair.com/EN/WebFace/Tang.WebFace.Cargo/"
    "AgentAwbBrower.aspx?lan=en-us"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STATUS_MAP: dict[str, str] = {
    "booked":       "Booked",
    "accepted":     "Received",
    "received":     "Received",
    "manifested":   "Manifested",
    "departed":     "Departed",
    "arrived":      "Arrived",
    "delivered":    "Delivered",
    "出发":         "Departed",
    "到达":         "Arrived",
    "收运":         "Received",
}

_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _detect_gap_x(bg_bytes: bytes, piece_bytes: bytes) -> float:
    """
    Detect the x-position of the jigsaw gap in the background image using brightness.
    The gap is shown as a white/bright cutout; find where the background is brightest
    under the piece's alpha mask.
    Returns the x coordinate (in natural image pixel space).
    """
    try:
        import numpy as np
        from PIL import Image
        import io as _io

        bg    = Image.open(_io.BytesIO(bg_bytes)).convert("L")   # grayscale
        piece = Image.open(_io.BytesIO(piece_bytes)).convert("RGBA")

        bg_arr    = np.array(bg,    dtype=np.float32)
        piece_arr = np.array(piece, dtype=np.float32)

        # Build binary mask from the piece's alpha channel
        alpha = piece_arr[:, :, 3] / 255.0
        mask  = (alpha > 0.5).astype(np.float32)
        mask_sum = mask.sum()
        if mask_sum < 1:
            return 180.0

        ph, pw = mask.shape
        bh, bw = bg_arr.shape

        # Slide the mask across the background; score = mean brightness under mask.
        # The gap appears white/bright, so the highest-score position is the gap.
        best_x     = pw // 2
        best_score = -1.0
        for x in range(0, bw - pw + 1):
            region = bg_arr[:ph, x : x + pw]
            if region.shape[1] != pw:
                continue
            score = float((region * mask).sum() / mask_sum)
            if score > best_score:
                best_score = score
                best_x     = x

        return float(best_x)
    except Exception:
        return 180.0


async def _solve_captcha_api(page, captcha_state: dict) -> bool:
    """
    Solve the slide CAPTCHA by:
    1. Detecting the bright white gap in the background image with brightness analysis.
    2. Calling /ValidateImage.ashx directly from inside the browser (same-origin fetch).
    3. On success the page JS closes the dialog and calls CheckIsSameAwb() automatically.
    Returns True if the dialog closed (CAPTCHA solved).
    """
    img_id    = captcha_state.get("img_id", "")
    bg_shot   = captcha_state.get("bg")
    piece_shot = captcha_state.get("piece")

    if not img_id or not bg_shot or not piece_shot:
        return False

    # Detect the gap position; try small offsets to handle sub-pixel errors
    target_x = _detect_gap_x(bg_shot, piece_shot)
    offsets   = [0, -3, 3, -6, 6, -9, 9]

    for attempt in range(4):          # up to 4 refresh cycles
        for off in offsets:
            slide_x = round(max(0.0, target_x + off), 1)
            try:
                rc = await page.evaluate(f"""async () => {{
                    try {{
                        var r = await fetch('/ValidateImage.ashx?imgid={img_id}&slidex={slide_x}', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/json; charset=utf-8'}},
                            credentials: 'include'
                        }});
                        var d = await r.json();
                        return d && d.resultCode != null ? parseInt(d.resultCode) : -1;
                    }} catch(e) {{ return -99; }}
                }}""")
            except Exception:
                rc = -99

            if rc == 0:
                # Server accepted — close dialog and trigger the search
                await page.evaluate(f"""() => {{
                    try {{ $('#verifyDialog').dialog('close'); }} catch(e) {{}}
                    try {{ $('#ctl00_ContentPlaceHolder1_txtImgId').val('{img_id}'); }} catch(e) {{}}
                    try {{ if (typeof CheckIsSameAwb === 'function') CheckIsSameAwb(); }} catch(e) {{}}
                }}""")
                await asyncio.sleep(1.0)
                return True

            if rc in (2, 3):
                break   # retries exhausted for this imgId

        # Refresh CAPTCHA to get a new image with a fresh imgId
        captcha_state.pop("bg",     None)
        captcha_state.pop("piece",  None)
        captcha_state.pop("img_id", None)
        try:
            await page.evaluate("""() => {
                var btn = document.querySelector('#slide-refresh-btn, .slide-img-reflash');
                if (btn) btn.click();
            }""")
        except Exception:
            pass
        # Wait for fresh images
        for _ in range(24):
            if captcha_state.get("bg") and captcha_state.get("piece") and captcha_state.get("img_id"):
                break
            await asyncio.sleep(0.5)

        img_id     = captcha_state.get("img_id",  img_id)
        bg_shot    = captcha_state.get("bg",       bg_shot)
        piece_shot = captcha_state.get("piece",    piece_shot)
        target_x   = _detect_gap_x(bg_shot, piece_shot)

    return False


async def _playwright_fetch(prefix: str, number: str) -> str:
    result_holder: dict = {}
    captcha_state: dict = {}

    async with PW_SEMAPHORE:
      async with Stealth().use_async(async_playwright()) as pw:
        # Prefer installed Chrome/Edge for better anti-bot bypass
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
                url = response.url
                # Capture tracking data — any sizeable POST to the form page;
                # always overwrite so the CAPTCHA-dialog response is replaced by the
                # real tracking response when it arrives.
                if "AgentAwbBrower.aspx" in url and response.request.method == "POST":
                    try:
                        text = await response.text()
                        if len(text) > 500:
                            result_holder["html"] = text
                    except Exception:
                        pass
                # Capture CAPTCHA images from network (avoids DPR/screenshot issues)
                if "verifyImage/bigImage" in url or "bigimage" in url.lower():
                    try:
                        captcha_state["bg"] = await response.body()
                        captcha_state["img_id"] = url.rstrip("/").split("/")[-1]
                    except Exception:
                        pass
                if "verifyImage/smallImage" in url or "smallimage" in url.lower():
                    try:
                        captcha_state["piece"] = await response.body()
                    except Exception:
                        pass

            ctx.on("response", on_response)

            await page.goto(_FORM_URL, wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(2000)

            await page.fill("#ctl00_ContentPlaceHolder1_txtPrefix", prefix)
            await page.fill("#ctl00_ContentPlaceHolder1_txtNo", number)

            # Click visible Search button (triggers CAPTCHA dialog)
            await page.click("#btnSearch")

            # Wait for CAPTCHA dialog to appear
            captcha_solved = False
            try:
                await page.wait_for_selector(
                    "#verifyDialog, .verify-dialog, [class*='verify-wrap']",
                    state="visible", timeout=12_000,
                )
                # Wait up to 12 s for both CAPTCHA images to arrive from network
                # (bg image is ~170 KB and takes time to download)
                for _ in range(24):
                    if captcha_state.get("bg") and captcha_state.get("piece"):
                        break
                    await asyncio.sleep(0.5)
                captcha_solved = await _solve_captcha_api(page, captcha_state)
                await page.wait_for_timeout(3000)
            except Exception:
                pass  # CAPTCHA didn't appear or already gone

            # If CAPTCHA was solved but still no tracking data, try clicking Search again
            if captcha_solved and "html" not in result_holder:
                try:
                    await page.click("#btnSearch", timeout=3000)
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass

            # Wait up to 40 s for real tracking data (not the CAPTCHA dialog POST)
            for _ in range(80):
                html = result_holder.get("html", "")
                if html and ("FlightNo" in html or "CargoStatus" in html
                             or "panelNewState" in html):
                    break
                await asyncio.sleep(0.5)

        finally:
            await browser.close()

    html = result_holder.get("html", "")
    if not html or not ("FlightNo" in html or "CargoStatus" in html or "panelNewState" in html):
        raise HTTPException(504, f"China Southern: no response for {prefix}-{number}")
    return html


# ── parsing ───────────────────────────────────────────────────────────────

def _extract_panel(raw: str) -> str:
    """
    ASP.NET UpdatePanel responses are pipe-delimited:
      len|updatePanel|id|content|...
    Extract the content for panelNewState; fall back to the full raw text.
    """
    # Try UpdatePanel wire format
    m = re.search(
        r'\d+\|updatePanel\|[^|]*panelNewState[^|]*\|(.*?)\|(?:\d+\||\Z)',
        raw, re.DOTALL
    )
    if m:
        return m.group(1)
    # Regular HTML fallback
    soup = BeautifulSoup(raw, "html.parser")
    panel = (
        soup.find(id="ctl00_ContentPlaceHolder1_panelNewState")
        or soup.find(id=re.compile(r"panelNewState", re.I))
    )
    return str(panel) if panel else raw


def _parse_date(raw: str) -> tuple[str, str]:
    """
    Handles various date-time strings:
      '2026-05-16 15:20'  → ('15:20', '2026/05/16')
      '20260516 15:20'    → ('15:20', '2026/05/16')
      '16-MAY-2026 15:20' → ('15:20', '2026/05/16')
    Returns (time_str, date_str) or ('', '') if unparseable.
    """
    s = raw.strip()
    # ISO-ish: YYYY-MM-DD HH:MM
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})', s)
    if m:
        return m.group(4), f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    # Compact: YYYYMMDD HH:MM
    m = re.match(r'(\d{4})(\d{2})(\d{2})\s+(\d{2}:\d{2})', s)
    if m:
        return m.group(4), f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    # DD-MON-YYYY HH:MM
    m = re.match(r'(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}:\d{2})', s)
    if m:
        mo = _MONTHS.get(m.group(2).lower(), "00")
        return m.group(4), f"{m.group(3)}/{mo}/{m.group(1).zfill(2)}"
    return "", ""


def _parse(awb: str, raw_html: str) -> TrackingResult:
    panel_html = _extract_panel(raw_html)
    soup = BeautifulSoup(panel_html, "html.parser")

    # Extract status text from a span/div labelled CargoStatus
    status = ""
    for el in soup.find_all(id=re.compile(r"CargoStatus", re.I)):
        t = el.get_text(strip=True)
        if t:
            status = _STATUS_MAP.get(t.lower(), t)
            break

    # Origin / destination — look for airport codes in dedicated cells or labels
    origin = dest = ""
    for el in soup.find_all(id=re.compile(r"(Origin|lblFrom)", re.I)):
        t = el.get_text(strip=True)
        if re.match(r'^[A-Z]{3}$', t):
            origin = t
            break
    for el in soup.find_all(id=re.compile(r"(Dest|lblTo)", re.I)):
        t = el.get_text(strip=True)
        if re.match(r'^[A-Z]{3}$', t):
            dest = t
            break

    # Pieces / weight
    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    for el in soup.find_all(id=re.compile(r"Pieces?", re.I)):
        try:
            pieces = int(el.get_text(strip=True).replace(",", ""))
            break
        except (ValueError, TypeError):
            pass
    for el in soup.find_all(id=re.compile(r"Weight", re.I)):
        try:
            weight_kg = float(el.get_text(strip=True).split()[0].replace(",", ""))
            break
        except (ValueError, IndexError, TypeError):
            pass

    # Flight legs from a table containing "FlightNo"
    flights: list[FlightLeg] = []
    flight_table = None
    for tbl in soup.find_all("table"):
        header_text = tbl.get_text()
        if "FlightNo" in header_text or "Flight No" in header_text:
            flight_table = tbl
            break

    if flight_table:
        # Map header names → column indices
        headers = [th.get_text(strip=True) for th in flight_table.find_all("th")]
        col = {h.lower().replace(" ", ""): i for i, h in enumerate(headers)}

        def _ci(keys: list[str]) -> int:
            for k in keys:
                for h, i in col.items():
                    if k in h:
                        return i
            return -1

        fn_idx   = _ci(["flightno", "flight"])
        from_idx = _ci(["from", "origin", "dep"])
        to_idx   = _ci(["to", "dest", "arr"])
        atd_idx  = _ci(["atd", "actualdep", "actualdeparture"])
        std_idx  = _ci(["std", "scheduleddep", "scheduleddeparture"])
        ata_idx  = _ci(["ata", "actualarr", "actualarrival"])
        sta_idx  = _ci(["sta", "scheduledarr", "scheduledarrival"])

        for tr in flight_table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue

            def _get(idx: int) -> str:
                return cells[idx].strip() if 0 <= idx < len(cells) else ""

            fn = _get(fn_idx)
            if not fn:
                continue

            dep_raw = _get(atd_idx) or _get(std_idx)
            arr_raw = _get(ata_idx) or _get(sta_idx)
            dep_status = "actual" if _get(atd_idx) else "scheduled"
            arr_status = "actual" if _get(ata_idx) else "scheduled"
            dep_time, dep_date = _parse_date(dep_raw)
            arr_time, arr_date = _parse_date(arr_raw)

            from_ap = _get(from_idx)
            to_ap   = _get(to_idx)
            if not origin and from_ap:
                origin = from_ap
            if not dest and to_ap:
                dest = to_ap

            flights.append(FlightLeg(
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
                flrs_id          = 0,
            ))

    if not flights and not status:
        raise HTTPException(404, f"AWB {awb} not found on China Southern")

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


# ── tracker ───────────────────────────────────────────────────────────────

class ChinaSouthernTracker(AirlineTracker):
    prefixes = ["784"]
    name = "China Southern Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb = f"{prefix}-{number}"
        html = await _playwright_fetch(prefix, number)
        return _parse(awb, html)

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
        return ULDResult(
            flight_no=flight_no,
            departure_date=departure_date,
            departure=departure,
            arrival=arrival,
            ulds=[],
        )
