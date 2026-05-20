"""
SF Airlines  —  prefix 921
REST JSON API at sf-airlines.com, protected by ICaptcha (SF Express SDK).
Uses Playwright-stealth to solve the captcha via page interaction,
then intercepts the JSON response from the API call.
"""
import asyncio
import re
from typing import Optional

from fastapi import HTTPException
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .base import AirlineTracker, FlightLeg, TrackingResult

_TRACK_PAGE = "https://www.sf-airlines.com/track/index.html"
_API_HOST   = "sfa-gwgw-inn.sf-airlines.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STATUS_MAP: dict[str, str] = {
    "bkd": "Booked",
    "rcs": "Received",
    "dep": "Departed",
    "arr": "Arrived",
    "rcf": "Arrived",
    "nfd": "Arrived",
    "dlv": "Delivered",
}
_STATUS_CHINESE: dict[str, str] = {
    "货物已收运": "Received",
    "货物已离港": "Departed",
    "货物已到港": "Arrived",
    "货物已提取": "Delivered",
    "已收运":     "Received",
    "已离港":     "Departed",
    "已到港":     "Arrived",
    "已提取":     "Delivered",
}


def _split_dt(raw: str) -> tuple[str, str]:
    """'2026-05-10 05:55:00' → ('05:55', '2026/05/10')"""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})(?:[T\s])(\d{2}:\d{2})', (raw or "").strip())
    if m:
        return m.group(4), f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return "", ""


async def _playwright_fetch(prefix: str, number: str) -> dict:
    awb = f"{prefix}-{number}"
    result_holder: dict = {}

    async with Stealth().use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = await browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )

            # Intercept the cargo API response
            async def on_response(response):
                if _API_HOST in response.url and response.status == 200:
                    try:
                        body = await response.json()
                        if body.get("success") and body.get("obj"):
                            result_holder["data"] = body
                    except Exception:
                        pass

            ctx.on("response", on_response)

            page = await ctx.new_page()
            await page.goto(_TRACK_PAGE, wait_until="domcontentloaded", timeout=45_000)
            await asyncio.sleep(2)

            # Fill in the AWB number
            awb_input = (
                page.locator("input[placeholder*='运单']").first
                or page.locator("input[type='text']").first
            )
            try:
                await awb_input.fill(awb, timeout=5_000)
            except Exception:
                # Try by index if selector fails
                inputs = await page.query_selector_all("input[type='text']")
                if inputs:
                    await inputs[0].fill(awb)

            # Click the search/query button
            for sel in [
                "button:has-text('查询')",
                "button:has-text('查')",
                "button[type='submit']",
                ".search-btn",
                ".query-btn",
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.count():
                        await btn.click(timeout=3_000)
                        break
                except Exception:
                    pass

            # Wait for the API response (captcha may auto-complete in headless)
            for _ in range(30):
                if "data" in result_holder:
                    break
                await asyncio.sleep(0.5)

            # If captcha blocks us, try clicking a visible slider or checkbox
            if "data" not in result_holder:
                for captcha_sel in [
                    ".nc_iconfont",
                    ".nc-lang-cnt",
                    "[class*='slider']",
                    "[class*='verify']",
                    "[class*='captcha']",
                ]:
                    try:
                        el = page.locator(captcha_sel).first
                        if await el.count():
                            box = await el.bounding_box()
                            if box:
                                await page.mouse.move(box["x"] + 10, box["y"] + box["height"] / 2)
                                await page.mouse.down()
                                await page.mouse.move(box["x"] + box["width"] - 5, box["y"] + box["height"] / 2)
                                await page.mouse.up()
                                await asyncio.sleep(2)
                                break
                    except Exception:
                        pass

                for _ in range(20):
                    if "data" in result_holder:
                        break
                    await asyncio.sleep(0.5)

        finally:
            await browser.close()

    if "data" not in result_holder:
        raise HTTPException(504, f"SF Airlines: no API response for {awb}")
    return result_holder["data"]


def _parse(awb: str, body: dict) -> TrackingResult:
    if not body.get("success"):
        raise HTTPException(404, f"AWB {awb} not found on SF Airlines")

    items = body.get("obj") or []
    if not items:
        raise HTTPException(404, f"AWB {awb} not found on SF Airlines")

    item = items[0]
    base = item.get("waybillBaseInfo") or {}

    origin = base.get("waybillDep", "")
    dest   = base.get("waybillArr", "")
    pieces: Optional[int]   = base.get("bpcs")
    weight: Optional[float] = base.get("bwgt")

    # Status from cargoRouting (last meaningful event)
    routing = item.get("cargoRouting") or []
    status  = ""
    for ev in reversed(routing):
        fsu = (ev.get("fsuType") or "").lower()
        txt = ev.get("showClientText") or ""
        if fsu:
            status = _STATUS_MAP.get(fsu, "")
            if not status:
                for k, v in _STATUS_CHINESE.items():
                    if k in txt:
                        status = v
                        break
        if status:
            break

    # Flights from flightLeg
    flights: list[FlightLeg] = []
    seen: set[str] = set()
    for leg in (item.get("flightLeg") or []):
        fn      = leg.get("legFlightNo") or leg.get("opCodeName") or ""
        if not fn:
            continue
        from_ap = leg.get("airportCode") or origin
        to_ap   = ""

        # Find DEP event for this flight in cargoRouting
        dep_time = dep_date = arr_time = arr_date = ""
        for ev in routing:
            if fn in (ev.get("flightNo") or ""):
                fsu = (ev.get("fsuType") or "").upper()
                dt  = ev.get("localDateTime") or ""
                if fsu == "DEP" and not dep_time:
                    dep_time, dep_date = _split_dt(dt)
                    from_ap = ev.get("occurPlace") or from_ap
                elif fsu in ("ARR", "RCF") and not arr_time:
                    arr_time, arr_date = _split_dt(dt)
                    to_ap = ev.get("occurPlace") or to_ap

        if not to_ap:
            to_ap = dest

        key = f"{fn}-{from_ap}"
        if key in seen:
            continue
        seen.add(key)

        flights.append(FlightLeg(
            flight_no        = fn,
            from_airport     = from_ap,
            to_airport       = to_ap,
            departure_date   = dep_date,
            departure_time   = dep_time,
            departure_status = "actual" if dep_time else "scheduled",
            arrival_date     = arr_date,
            arrival_time     = arr_time,
            arrival_status   = "actual" if arr_time else "scheduled",
            flight_time      = "",
            flrs_id          = 0,
        ))

    if not status and not origin:
        raise HTTPException(404, f"AWB {awb} not found on SF Airlines")

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
        total_weight_kg = weight,
    )


class SFAirlinesTracker(AirlineTracker):
    prefixes = ["921"]
    name     = "SF Airlines"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb  = f"{prefix}-{number}"
        body = await _playwright_fetch(prefix, number)
        return _parse(awb, body)
