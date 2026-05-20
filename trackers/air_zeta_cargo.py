"""
Air Zeta Cargo  —  prefix 994
SITA iCargo portal at portal.airzetacargo.com.
Identical API structure to Asiana Cargo (prefix 988).

Two-step flow (httpx):
  GET  /tracking/viewTraceAirWaybill.do          → JSESSIONID + CSRF token
  POST /tracking/searchTraceAirWaybillResult.do  → JSON tracking data
"""
import re
from typing import Optional

import httpx
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult

_BASE     = "https://portal.airzetacargo.com"
_GET_URL  = f"{_BASE}/tracking/viewTraceAirWaybill.do"
_POST_URL = f"{_BASE}/tracking/searchTraceAirWaybillResult.do"
_UA       = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_EVENT_STATUS: dict[str, str] = {
    "BKD": "Booked",
    "RCS": "Received",
    "MAN": "Manifested",
    "DEP": "Departed",
    "ARR": "Arrived",
    "RCF": "Arrived",
    "NFD": "Arrived",
    "DLV": "Delivered",
    "AWD": "Arrived",
}


def _split_dt(raw: str) -> tuple[str, str]:
    """'2026-05-19 08:50' → ('08:50', '2026/05/19')"""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})', (raw or "").strip())
    if m:
        return m.group(4), f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return "", ""


async def _fetch(prefix: str, number: str) -> dict:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Step 1: GET to establish session and get CSRF token
        r1 = await client.get(
            _GET_URL,
            headers={"User-Agent": _UA, "Accept": "text/html,*/*"},
        )
        csrf = ""
        m = re.search(r'<meta[^>]+name=["\']_csrf["\'][^>]+content=["\']([^"\']+)["\']', r1.text)
        if m:
            csrf = m.group(1)

        # Step 2: POST tracking query
        r2 = await client.post(
            _POST_URL,
            headers={
                "User-Agent":        _UA,
                "X-CSRF-TOKEN":      csrf,
                "X-Requested-With":  "XMLHttpRequest",
                "Content-Type":      "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer":           _GET_URL,
            },
            content=f"prefix={prefix}&awbNumber={number}",
        )

    if r2.status_code != 200:
        raise HTTPException(502, f"Air Zeta Cargo returned HTTP {r2.status_code}")
    try:
        return r2.json()
    except Exception:
        raise HTTPException(502, "Air Zeta Cargo returned non-JSON response")


def _parse(awb: str, body: dict) -> TrackingResult:
    if not body.get("success"):
        raise HTTPException(404, f"AWB {awb} not found on Air Zeta Cargo")

    items = body.get("data") or []
    if not items:
        raise HTTPException(404, f"AWB {awb} not found on Air Zeta Cargo")

    item  = items[0]
    smry  = item.get("shipmentSummaryVo") or {}

    origin = smry.get("origin", "")
    dest   = smry.get("destination", "")
    pieces: Optional[int]   = smry.get("statedPieces")
    weight: Optional[float] = smry.get("statedWeight")

    ev_code = item.get("latestEvent", "")
    ev_msg  = item.get("latestEventMessage", "")
    status  = _EVENT_STATUS.get(ev_code.upper(), ev_msg or ev_code)

    flights: list[FlightLeg] = []
    for leg in (item.get("fetchFlightDetailsList") or []):
        carrier = leg.get("flightCarrierCode", "")
        fn_num  = leg.get("flightNumber", "")
        fn      = (carrier + fn_num).strip()
        from_a  = leg.get("segmentOrigin", "")
        to_a    = leg.get("segmentDestination", "")

        atd_raw = leg.get("actualDepartureDate") or leg.get("estimatedDepartureDate") or leg.get("scheduledDepartureDate") or ""
        ata_raw = leg.get("actualArrivalDate")   or leg.get("estimatedArrivalDate")   or leg.get("scheduledArrivalDate")   or ""
        dep_status = "actual"    if leg.get("actualDepartureDate")   else \
                     "estimated" if leg.get("estimatedDepartureDate") else "scheduled"
        arr_status = "actual"    if leg.get("actualArrivalDate")     else \
                     "estimated" if leg.get("estimatedArrivalDate")   else "scheduled"

        dep_time, dep_date = _split_dt(atd_raw)
        arr_time, arr_date = _split_dt(ata_raw)

        flights.append(FlightLeg(
            flight_no        = fn,
            from_airport     = from_a,
            to_airport       = to_a,
            departure_date   = dep_date,
            departure_time   = dep_time,
            departure_status = dep_status,
            arrival_date     = arr_date,
            arrival_time     = arr_time,
            arrival_status   = arr_status,
            flight_time      = "",
            pieces           = leg.get("manifestedPieces"),
            weight_kg        = leg.get("manifestedWeight"),
            flrs_id          = 0,
        ))

    return TrackingResult(
        awb             = awb,
        from_airport    = origin,
        from_name       = smry.get("originName", ""),
        to_airport      = dest,
        to_name         = smry.get("destinationName", ""),
        status          = status,
        status_code     = ev_code,
        flights         = flights,
        total_pieces    = pieces,
        total_weight_kg = weight,
    )


class AirZetaCargoTracker(AirlineTracker):
    prefixes = ["994"]
    name     = "Air Zeta Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb  = f"{prefix}-{number}"
        body = await _fetch(prefix, number)
        return _parse(awb, body)
