"""
IAG Cargo  —  prefix 125  (British Airways / Iberia cargo)
JSON REST API at api.tracking.iagcargo.com — no login required.

GET https://api.tracking.iagcargo.com/tracking/{prefix}-{number}
Returns JSON with journeyStations and per-milestone events.
"""
import re
from typing import Optional

import httpx
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

_API_URL = "https://api.tracking.iagcargo.com/tracking"
_UA = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36"
)

# Cache: awb → (pieces, weight_kg)
_iag_cache: dict[str, tuple[Optional[int], Optional[float]]] = {}

_STATUS_MAP: dict[str, str] = {
    "collected":  "Delivered",
    "delivered":  "Delivered",
    "arrived":    "Arrived",
    "in_transit": "Departed",
    "departed":   "Departed",
    "accepted":   "Received",
    "received":   "Received",
    "booked":     "Booked",
}


def _parse_iso(raw: str) -> tuple[str, str]:
    """'2026-05-19T10:34:00.000' → ('10:34', '2026/05/19')"""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})', (raw or "").strip())
    if m:
        return m.group(4), f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return "", ""


def _norm_fn(fn: str) -> str:
    """BA0207 → BA207 (strip leading zeros in numeric part)."""
    return re.sub(r'^([A-Z]{1,3})0+(\d)', r'\1\2', (fn or "").strip())


def _parse(awb: str, data: dict) -> TrackingResult:
    awb_info = data.get("awb") or {}
    origin = awb_info.get("originCode", "")
    dest   = awb_info.get("destinationCode", "")

    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    try:
        pieces = int(awb_info.get("totalPackages") or 0) or None
    except (ValueError, TypeError):
        pass
    try:
        wt   = awb_info.get("totalWeight")
        unit = awb_info.get("unitOfTotalWeight", "K")
        if wt:
            weight_kg = float(wt) * 0.453592 if unit == "L" else float(wt)
    except (ValueError, TypeError):
        pass

    # Overall status
    raw_s  = ((data.get("shipmentStatusDetails") or {}).get("status") or "").lower()
    status = _STATUS_MAP.get(raw_s, raw_s.replace("_", " ").capitalize() if raw_s else "")

    stations = data.get("journeyStations") or []

    # Pre-collect the best ARR milestone per normalized flight number
    # Best = timeIndicator A, not duplicated, not INCOMPLETE
    arr_by_fn: dict[str, tuple[str, str, str]] = {}
    for station in stations:
        for ms in (station.get("milestones") or []):
            if ms.get("milestoneCode") != "ARR":
                continue
            if ms.get("duplicated") or ms.get("status") == "INCOMPLETE":
                continue
            fn = _norm_fn(ms.get("flightNumber") or "")
            if not fn or fn in arr_by_fn:
                continue
            arr_time, arr_date = _parse_iso(ms.get("eventTime") or "")
            indicator  = ms.get("timeIndicator", "")
            arr_status = "actual" if indicator == "A" else "estimated" if indicator == "E" else "scheduled"
            arr_by_fn[fn] = (arr_time, arr_date, arr_status)

    # Build flight legs from DEP milestones
    flights: list[FlightLeg] = []
    seen: set[str] = set()

    for station in stations:
        if station.get("missed"):
            continue

        # Pick best DEP: prefer actual (A), skip INCOMPLETE
        dep_ms = None
        for ms in (station.get("milestones") or []):
            if ms.get("milestoneCode") != "DEP":
                continue
            if ms.get("status") == "INCOMPLETE":
                continue
            fn = _norm_fn(ms.get("flightNumber") or "")
            if not fn:
                continue
            if dep_ms is None:
                dep_ms = ms
            elif ms.get("timeIndicator") == "A" and dep_ms.get("timeIndicator") != "A":
                dep_ms = ms  # upgrade to actual

        if not dep_ms:
            continue

        fn       = _norm_fn(dep_ms.get("flightNumber") or "")
        loc_code = (station.get("location") or {}).get("locationCode", "")
        key      = f"{fn}-{loc_code}"
        if key in seen:
            continue
        seen.add(key)

        from_ap = (dep_ms.get("sectorOrigin") or {}).get("locationCode") or loc_code
        to_ap   = (dep_ms.get("sectorDestination") or {}).get("locationCode") or dest

        dep_time, dep_date = _parse_iso(dep_ms.get("eventTime") or "")
        indicator  = dep_ms.get("timeIndicator", "")
        dep_status = "actual" if indicator == "A" else "estimated" if indicator == "E" else "scheduled"

        arr_time, arr_date, arr_status = arr_by_fn.get(fn, ("", "", "scheduled"))

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
            flrs_id          = 1,
        ))

    if not origin and not flights:
        raise HTTPException(404, f"AWB {awb} not found on IAG Cargo")

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
    )


class IAGCargoTracker(AirlineTracker):
    prefixes = ["125"]
    name     = "IAG Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb = f"{prefix}-{number}"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                f"{_API_URL}/{awb}",
                headers={
                    "User-Agent": _UA,
                    "Accept":     "application/json, text/plain, */*",
                    "Origin":     "https://ui.tracking.iagcargo.com",
                    "Referer":    "https://ui.tracking.iagcargo.com/",
                },
            )
        if resp.status_code == 404:
            raise HTTPException(404, f"AWB {awb} not found on IAG Cargo")
        if resp.status_code != 200:
            raise HTTPException(502, f"IAG Cargo returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            raise HTTPException(502, "IAG Cargo: invalid JSON response")
        result = _parse(awb, data)
        _iag_cache[awb] = (result.total_pieces, result.total_weight_kg)
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
        awb = f"{prefix}-{awb_number}"
        pieces, weight_kg = _iag_cache.get(awb, (None, None))
        ulds = []
        if weight_kg is not None:
            label = f"{weight_kg:.1f} kg"
            ulds = [ULDItem(uld=label, pieces=pieces or 0)]
        return ULDResult(
            flight_no      = flight_no,
            departure_date = departure_date,
            departure      = departure,
            arrival        = arrival,
            ulds           = ulds,
        )
