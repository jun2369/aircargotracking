"""
Atlas Air  —  prefix 369
Public REST API, no authentication required.
GET https://jumpseat.atlasair.com/tracktraceapi/api/FreightContProvdr/GetFrieghtDtlByAwbNo
ULD data available in ULD-status events.
"""
import asyncio
from typing import Optional

import httpx
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

_API = (
    "https://jumpseat.atlasair.com/tracktraceapi/api"
    "/FreightContProvdr/GetFrieghtDtlByAwbNo"
)
_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://jumpseat.atlasair.com/aa/tracktracehtml/TrackTrace.html",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

_STATUS_MAP = {
    "BKD": "Booked",       "RCS": "Received",     "RCT": "Received",
    "MAN": "Manifested",   "ULD": "Departed",      "DEP": "Departed",
    "ARR": "Arrived",      "RCF": "Arrived",       "NFD": "Notified for Delivery",
    "DLV": "Delivered",    "TFD": "Transferred",
}
_DEP_STATUSES = {"ULD", "DEP"}
_ARR_STATUSES = {"ARR", "RCF"}

# Response cache: "prefix-number" → full API response
_response_cache: dict[str, dict] = {}


async def _fetch_api(prefix: str, number: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(_API, params={"prfx": prefix, "serial": number},
                                headers=_HEADERS)
    if resp.status_code != 200:
        raise HTTPException(502, f"Atlas Air API returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except Exception:
        raise HTTPException(502, "Atlas Air returned non-JSON")


# ── tracker ───────────────────────────────────────────────────────────────

class AtlasAirTracker(AirlineTracker):
    prefixes = ["369"]
    name = "Atlas Air"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb_display = f"{prefix}-{number}"
        data = await _fetch_api(prefix, number)

        if not data.get("Origin") and not data.get("LstFrieghtDtlEnhanced"):
            raise HTTPException(404, f"AWB {awb_display} not found on Atlas Air")

        _response_cache[awb_display] = data
        return self._parse(awb_display, data)

    def _parse(self, awb: str, data: dict) -> TrackingResult:
        status_list = data.get("LstStatus") or []
        last_code = status_list[-1] if status_list else ""
        status = _STATUS_MAP.get(last_code, last_code)

        events = data.get("LstFrieghtDtlEnhanced") or []
        flights = _build_legs(events)

        return TrackingResult(
            awb=awb,
            from_airport=data.get("Origin", ""),
            from_name="",
            to_airport=data.get("Destination", ""),
            to_name="",
            status=status,
            status_code=last_code,
            flights=flights,
            total_pieces=data.get("Pieces"),
            total_weight_kg=data.get("Weight"),
        )

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
        cache_key = f"{prefix}-{awb_number}"
        data = _response_cache.get(cache_key)
        if not data:
            data = await _fetch_api(prefix, awb_number)
            _response_cache[cache_key] = data

        ulds: list[ULDItem] = []
        for ev in data.get("LstFrieghtDtlEnhanced") or []:
            fn = ((ev.get("Carrier") or "") + (ev.get("FlightNo") or "")).strip()
            if fn != flight_no:
                continue
            if ev.get("Status") not in _DEP_STATUSES:
                continue
            for item in ev.get("LstULDDetail") or []:
                uld_no = (item.get("ULDNumber") or "").strip()
                if uld_no:
                    ulds.append(ULDItem(
                        uld=uld_no,
                        pieces=int(item.get("Pieces") or 0),
                    ))

        return ULDResult(
            flight_no=flight_no,
            departure_date=departure_date,
            departure=departure,
            arrival=arrival,
            ulds=ulds,
        )


# ── helpers ───────────────────────────────────────────────────────────────

def _build_legs(events: list) -> list[FlightLeg]:
    # Collect scheduled times from BKD events
    bkd: dict[str, dict] = {}
    for ev in events:
        if ev.get("Status") != "BKD":
            continue
        fn = ((ev.get("Carrier") or "") + (ev.get("FlightNo") or "")).strip()
        if fn:
            bkd[fn] = ev

    legs: dict[str, dict] = {}
    order: list[str] = []

    for ev in events:
        fn = ((ev.get("Carrier") or "") + (ev.get("FlightNo") or "")).strip()
        if not fn:
            continue
        status = ev.get("Status", "")
        if status not in (_DEP_STATUSES | _ARR_STATUSES):
            continue

        if fn not in legs:
            b = bkd.get(fn, {})
            sdep_t, sdep_d = _sched_time(b.get("DepatureDate"), b.get("DepatureTime"))
            sarr_t, sarr_d = _sched_time(b.get("ArrivalDate"),  b.get("ArrivalTime"))
            legs[fn] = {
                "flight_no":        fn,
                "from_airport":     ev.get("Origin", ""),
                "to_airport":       ev.get("Destination", ""),
                "departure_date":   sdep_d,
                "departure_time":   sdep_t,
                "departure_status": "scheduled",
                "arrival_date":     sarr_d,
                "arrival_time":     sarr_t,
                "arrival_status":   "scheduled",
                "flight_time":      "",
                "flrs_id":          1,
            }
            order.append(fn)

        leg = legs[fn]
        t, d = _fmt_dt(ev.get("DtTime") or "")
        if status in _DEP_STATUSES and t:
            leg["departure_time"]   = t
            leg["departure_date"]   = d
            leg["departure_status"] = "actual"
        elif status in _ARR_STATUSES and t:
            leg["arrival_time"]   = t
            leg["arrival_date"]   = d
            leg["arrival_status"] = "actual"

    return [FlightLeg(**legs[fn]) for fn in order]


def _fmt_dt(dt_str: str) -> tuple[str, str]:
    """'2026-05-15T01:00:00' → ('01:00', '2026/05/15')."""
    if not dt_str:
        return "", ""
    try:
        date_part, time_part = dt_str.split("T")
        return time_part[:5], date_part.replace("-", "/")
    except Exception:
        return "", ""


def _sched_time(date_iso: Optional[str], time_hhmm: Optional[str]) -> tuple[str, str]:
    """('2026-05-15T00:00:00', '0100') → ('01:00', '2026/05/15')."""
    if not date_iso or not time_hhmm:
        return "", ""
    try:
        date_part = date_iso.split("T")[0].replace("-", "/")
        t = f"{time_hhmm[:2]}:{time_hhmm[2:4]}"
        return t, date_part
    except Exception:
        return "", ""
