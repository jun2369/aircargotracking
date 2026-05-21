"""
ANA Cargo  —  prefix 205
iCargo Neo portal (IBS Software) at prd.intcgo.ana.co.jp.
The website opens a new popup window via ico.openPortal() + postMessage;
the popup calls /portalgateway/graphql internally.
We use Playwright-stealth to drive the ANA cargo page, wait for the popup,
then intercept the GraphQL response from within the popup.
"""
import asyncio
from typing import Optional

from fastapi import HTTPException
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from .base import AirlineTracker, FlightLeg, PW_ARGS, TrackingResult, ULDItem, ULDResult

_CARGO_PAGE = "https://www.anacargo.jp/en/int/"
_GQL_PATH   = "/portalgateway/graphql"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Cache: awb_display → parsed GraphQL response
_response_cache: dict[str, dict] = {}
# Cache: awb_display → {normalized_flight_no → list[ULDItem]}
_ana_uld_cache: dict[str, dict] = {}


async def _playwright_fetch(prefix: str, number: str) -> dict:
    result_holder: dict = {}
    popup_holder:  dict = {}

    async with Stealth().use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(headless=True, args=PW_ARGS)
        try:
            ctx = await browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 800},
            )

            async def on_new_page(page):
                popup_holder["page"] = page

            ctx.on("page", on_new_page)

            # Intercept GraphQL responses on ALL pages in context
            async def on_response(response):
                if _GQL_PATH in response.url and response.status == 200:
                    try:
                        body = await response.json()
                        data = (body.get("data") or {})
                        # Activity log
                        if any("getshipmentactivity" in k.lower() for k in data):
                            result_holder["activity"] = body
                            return
                        # Main tracking / detail (GetShipmentsByAwbs, GetShipmentDetailsByAwb)
                        if any(k.lower().startswith("getshipment") for k in data):
                            result_holder["data"] = body
                    except Exception:
                        pass

            ctx.on("response", on_response)

            main_page = await ctx.new_page()
            await main_page.goto(_CARGO_PAGE, wait_until="domcontentloaded", timeout=40_000)
            await main_page.wait_for_timeout(2000)

            await main_page.fill("input[name='code01']", prefix)
            await main_page.fill("input[name='code02']", number)

            search_btn = await main_page.query_selector(".trackshipments .search a")
            if not search_btn:
                search_btn = await main_page.query_selector("a:has-text('Search')")
            if search_btn:
                await search_btn.click()
            else:
                raise HTTPException(502, "ANA Cargo: could not find Search button")

            # Wait up to 30 s for main data (GetShipmentDetailsByAwb with splits)
            for _ in range(60):
                if "data" in result_holder:
                    break
                await asyncio.sleep(0.5)

            # Click "Activity View" in the popup to trigger the activity query (ULD info)
            if "data" in result_holder and "activity" not in result_holder:
                await asyncio.sleep(1)
                popup = popup_holder.get("page")
                if popup:
                    for sel in [
                        "button:has-text('Activity View')",
                        "a:has-text('Activity View')",
                        "[class*='activity']",
                    ]:
                        try:
                            el = popup.locator(sel).first
                            if await el.count():
                                await el.click(timeout=3000)
                                break
                        except Exception:
                            pass
                # Wait up to 10 s for activity response
                for _ in range(20):
                    if "activity" in result_holder:
                        break
                    await asyncio.sleep(0.5)

        finally:
            await browser.close()

    if "data" not in result_holder:
        raise HTTPException(504, f"ANA Cargo: no GraphQL response received for {prefix}-{number}")

    return result_holder["data"], result_holder.get("activity")


# ── parsing ───────────────────────────────────────────────────────────────

_MILESTONE_STATUS = {
    "DELIVERED":   "Delivered",
    "ARRIVED":     "Arrived",
    "DEPARTED":    "Departed",
    "ACCEPTED":    "Received",
    "BOOKED":      "Booked",
}


def _parse(awb: str, body: dict, activity_body: Optional[dict] = None) -> TrackingResult:
    data = body.get("data") or {}

    # Find the first GetShipment* key — value may be a list or dict
    item: dict = {}
    for k, v in data.items():
        if k.lower().startswith("getshipment"):
            if isinstance(v, list) and v:
                item = v[0]         # GetShipmentsByAwbs → list[dict]
            elif isinstance(v, dict):
                item = v            # GetShipmentByAwb  → dict
            break

    if not item or item.get("errors"):
        raise HTTPException(404, f"AWB {awb} not found on ANA Cargo")

    # ── New API: GetShipmentsByAwbs ──────────────────────────────────────
    if "origin_airport_code" in item:
        origin = item.get("origin_airport_code", "")
        dest   = item.get("destination_airport_code", "")

        # Status: last milestone marked "done" or "in_progress"
        status = ""
        for ms in reversed(item.get("milestones") or []):
            if ms.get("status") in ("done", "in_progress"):
                status = _MILESTONE_STATUS.get(ms.get("milestone", "").upper(),
                                               ms.get("milestone", ""))
                break

        pieces: Optional[int] = None
        weight_kg: Optional[float] = None
        try:
            pieces = int(item.get("pieces") or 0) or None
        except (ValueError, TypeError):
            pass
        try:
            wt = item.get("stated_weight")
            # Convert LB to KG if needed
            unit = (item.get("units_of_measure") or {}).get("weight", "K")
            if wt:
                weight_kg = float(wt) * 0.453592 if unit == "L" else float(wt)
        except (ValueError, TypeError):
            pass

        # Priority 1: splits from GetShipmentDetailsByAwb (most reliable)
        splits_raw = data.get("GetShipmentSplitsByAwb")
        flights = _legs_from_splits(splits_raw) if splits_raw else []

        # Priority 2: activity log
        if not flights and activity_body:
            flights = _legs_from_activity(activity_body)

        # Fallback: single summary leg
        if not flights:
            dep_time, dep_date = _parse_dt_dmy(item.get("departure_time", ""))
            arr_time, arr_date = _parse_dt_dmy(item.get("arrival_time", ""))
            flights = [FlightLeg(
                flight_no        = "NH",
                from_airport     = origin,
                to_airport       = dest,
                departure_date   = dep_date,
                departure_time   = dep_time,
                departure_status = "actual" if dep_time else "scheduled",
                arrival_date     = arr_date,
                arrival_time     = arr_time,
                arrival_status   = "actual" if arr_time else "scheduled",
                flight_time      = "",
                flrs_id          = 0,
            )] if origin else []

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

    # ── Legacy API: iCargo bookingStatus structure ───────────────────────
    routing = item.get("routing") or []
    origin = routing[0].get("origin", "") if routing else item.get("origin_airport", "")
    dest   = routing[-1].get("destination", "") if routing else item.get("destination_airport", "")

    booking_statuses = item.get("bookingStatus") or item.get("booking_status") or []
    status = ""
    for entry in booking_statuses:
        if not entry.get("flight"):
            status = entry.get("status", "")
            break

    pieces = None
    weight_kg = None
    try:
        pieces = int(item.get("total_pieces") or item.get("totalPieces") or 0) or None
    except (ValueError, TypeError):
        pass
    w_raw = item.get("total_weight") or item.get("totalWeight") or item.get("weight")
    if isinstance(w_raw, dict):
        w_raw = w_raw.get("value")
    try:
        weight_kg = float(w_raw) or None
    except (ValueError, TypeError):
        pass

    flights = [_parse_leg(e) for e in booking_statuses if e.get("flight")]

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


def _fn_key(fn: str) -> str:
    """Normalize flight number for cache lookup: remove dashes/spaces, uppercase."""
    return fn.replace("-", "").replace(" ", "").upper()


def _extract_uld_from_activity(activity_body: dict) -> dict[str, list]:
    """Return {normalized_flight_no: [ULDItem, ...]} from DEPARTURE events in activity log."""
    data = activity_body.get("data") or {}
    events = None
    for k, v in data.items():
        if "getshipmentactivity" in k.lower():
            if isinstance(v, list):
                events = v
            elif isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list):
                        events = vv
                        break
            break
    if not events:
        return {}

    result: dict[str, list] = {}
    for ev in events:
        etype = (
            ev.get("eventType") or ev.get("event_type") or
            ev.get("status") or ev.get("activityType") or ""
        ).upper()
        if "DEP" not in etype:
            continue

        fn_raw = (
            ev.get("flightNumber") or ev.get("flight_number") or
            ev.get("flightNo") or ev.get("flight") or ""
        ).strip()
        if not fn_raw:
            continue
        fn_key = _fn_key(fn_raw)

        uld = (
            ev.get("uldNumber") or ev.get("uld_number") or ev.get("uld") or
            ev.get("containerNo") or ev.get("container_no") or
            ev.get("uldNo") or ""
        ).strip()

        try:
            pcs = int(ev.get("pieces") or 0)
        except (ValueError, TypeError):
            pcs = 0
        try:
            wgt = float(ev.get("weight") or ev.get("grossWeight") or 0)
        except (ValueError, TypeError):
            wgt = 0.0

        if fn_key not in result:
            result[fn_key] = []
        result[fn_key].append(ULDItem(
            uld=uld if uld else (f"{wgt} kg" if wgt else ""),
            pieces=pcs,
        ))
    return result


def _legs_from_splits(splits_raw: list) -> list[FlightLeg]:
    """Extract flight legs from GetShipmentSplitsByAwb data.

    split_details: each entry with carrier_code+flight_number is a departure;
    the final entry (no flight) is the delivery node whose origin = destination airport.
    """
    if not isinstance(splits_raw, list) or not splits_raw:
        return []
    split_details = (splits_raw[0].get("split_details") or [])
    if not split_details:
        return []

    id_to_entry: dict = {sd["item_id"]: sd for sd in split_details if sd.get("item_id")}

    legs: list[FlightLeg] = []
    for sd in split_details:
        carrier = sd.get("carrier_code") or ""
        fn_raw  = sd.get("flight_number") or ""
        if not carrier or not fn_raw:
            continue
        fn      = carrier + str(fn_raw)
        from_ap = (sd.get("origin_airport_code") or "")[:3]

        next_id   = sd.get("next_item_id")
        next_entry = id_to_entry.get(next_id) if next_id else None
        to_ap     = (next_entry.get("origin_airport_code") or "")[:3] if next_entry else ""

        dep_raw    = sd.get("milestone_time") or ""
        postfix    = (sd.get("milestone_time_postfix") or "").upper()
        dep_status = "actual" if postfix == "A" else "scheduled"
        dep_time, dep_date = _parse_dt_dmy(dep_raw)

        # Arrival time: only available when next entry is the final delivery node
        arr_time = arr_date = ""
        arr_status = "scheduled"
        if next_entry and not next_entry.get("carrier_code"):
            arr_raw    = next_entry.get("milestone_time") or ""
            arr_postfix = (next_entry.get("milestone_time_postfix") or "").upper()
            arr_status  = "actual" if arr_postfix == "A" else "scheduled"
            arr_time, arr_date = _parse_dt_dmy(arr_raw)

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


def _legs_from_activity(activity_body: dict) -> list[FlightLeg]:
    """Extract flight legs from GetShipmentActivityByAwb response."""
    import re
    data = activity_body.get("data") or {}
    events = None
    for k, v in data.items():
        if "getshipmentactivity" in k.lower():
            events = v if isinstance(v, list) else None
            break
    if not events:
        return []

    # Collect DEP events to build legs; each event is one departure
    seen: set[str] = set()
    legs: list[FlightLeg] = []
    for ev in events:
        fn = ev.get("flightNumber") or ev.get("flight_number") or ""
        if not fn:
            continue
        from_ap = ev.get("departureStation") or ev.get("origin") or ""
        to_ap   = ev.get("arrivalStation") or ev.get("destination") or ""
        dep_raw = ev.get("actualDepartureTime") or ev.get("scheduledDepartureTime") or ev.get("eventDate") or ""
        arr_raw = ev.get("actualArrivalTime") or ev.get("scheduledArrivalTime") or ""

        dep_time, dep_date = _parse_dt_dmy(dep_raw) if dep_raw else _split_iso(dep_raw)
        arr_time, arr_date = _parse_dt_dmy(arr_raw) if arr_raw else _split_iso(arr_raw)
        key = f"{fn}-{from_ap}"
        if key in seen:
            continue
        seen.add(key)
        legs.append(FlightLeg(
            flight_no        = fn,
            from_airport     = from_ap[:3],
            to_airport       = to_ap[:3],
            departure_date   = dep_date,
            departure_time   = dep_time,
            departure_status = "actual" if dep_time else "scheduled",
            arrival_date     = arr_date,
            arrival_time     = arr_time,
            arrival_status   = "actual" if arr_time else "scheduled",
            flight_time      = "",
            flrs_id          = 0,
        ))
    return legs


def _parse_dt_dmy(raw: str) -> tuple[str, str]:
    """'06-05-2026 00:49:32' (DD-MM-YYYY) → ('00:49', '2026/05/06')"""
    import re
    m = re.match(r'(\d{2})-(\d{2})-(\d{4})\s+(\d{2}:\d{2})', (raw or "").strip())
    if m:
        return m.group(4), f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return "", ""


def _parse_leg(entry: dict) -> FlightLeg:
    flight_raw = entry.get("flight", "")
    flight_no  = flight_raw.split("/")[0] if flight_raw else ""

    dep_iso    = entry.get("ATD") or entry.get("STD") or ""
    dep_status = "actual" if entry.get("ATD") else "scheduled"
    dep_time, dep_date = _split_iso(dep_iso)

    arr_iso    = entry.get("ATA") or entry.get("STA") or ""
    arr_status = "actual" if entry.get("ATA") else "scheduled"
    arr_time, arr_date = _split_iso(arr_iso)

    import re
    m = re.search(r'\b([A-Z]{3})\s+TO\s+([A-Z]{3})\b', entry.get("status", ""))
    from_ap, to_ap = (m.group(1), m.group(2)) if m else ("", "")

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
        flrs_id          = 0,
    )


def _split_iso(iso: str) -> tuple[str, str]:
    if not iso:
        return "", ""
    try:
        date_part, rest = iso.split("T")
        return rest[:5], date_part.replace("-", "/")
    except Exception:
        return "", ""


# ── tracker ───────────────────────────────────────────────────────────────

class ANACargoTracker(AirlineTracker):
    prefixes = ["205"]
    name = "ANA Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb = f"{prefix}-{number}"
        body, activity = await _playwright_fetch(prefix, number)
        _response_cache[awb] = body
        if activity:
            uld_map = _extract_uld_from_activity(activity)
            if uld_map:
                _ana_uld_cache[awb] = uld_map
        return _parse(awb, body, activity)

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
        awb = f"{prefix}-{awb_number}"
        ulds: list[ULDItem] = []
        uld_map = _ana_uld_cache.get(awb)
        if uld_map:
            key = _fn_key(flight_no)
            ulds = uld_map.get(key) or []
        return ULDResult(
            flight_no=flight_no,
            departure_date=departure_date,
            departure=departure,
            arrival=arrival,
            ulds=ulds,
        )
