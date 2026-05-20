"""
Cargolux  —  prefix 172
Public iCargo API, no authentication required.
GET https://cargolux-icargo-api-app-prod.politesmoke-46f514de.westeurope.azurecontainerapps.io/api/track/awbs/?numbers=172-XXXXXXXX
"""
import httpx
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult

_API = (
    "https://cargolux-icargo-api-app-prod.politesmoke-46f514de"
    ".westeurope.azurecontainerapps.io/api/track/awbs/"
)
_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.cargolux.com",
    "Referer": "https://www.cargolux.com/track-and-Trace",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# iCargo event codes that represent a departure from an airport
_DEP_EVENTS = {"DEP", "TFD"}
_ARR_EVENTS = {"ARR", "RCF"}


class CargoluxTracker(AirlineTracker):
    prefixes = ["172"]
    name = "Cargolux"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb = f"{prefix}-{number}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(_API, params={"numbers": awb}, headers=_HEADERS)

        if resp.status_code != 200:
            raise HTTPException(502, f"Cargolux API returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except Exception:
            raise HTTPException(502, "Cargolux returned non-JSON")

        invalid = data.get("invalidAwbNumbers", [])
        trackings = data.get("trackings", [])
        if not trackings or awb in invalid:
            raise HTTPException(404, f"AWB {awb} not found on Cargolux")

        return self._parse(awb, trackings[0])

    def _parse(self, awb: str, t: dict) -> TrackingResult:
        summary = t.get("shipmentSummary", {})
        airport_events = t.get("airportEvents", [])

        # Build flight legs from airport events
        # Each airport may have DEP / ARR events tied to a flight number
        flights = _extract_flights(airport_events)

        # Derive overall status from last event
        last_status = _last_status(airport_events)

        return TrackingResult(
            awb=awb,
            from_airport=summary.get("origin", ""),
            from_name="",
            to_airport=summary.get("destination", ""),
            to_name="",
            status=last_status,
            status_code=last_status,
            total_pieces=summary.get("statedPieces"),
            total_weight_kg=summary.get("statedWeight"),
            flights=flights,
        )


# ── helpers ───────────────────────────────────────────────────────────────

def _fmt_time(iso: str) -> tuple[str, str]:
    """Return (HH:MM, YYYY/MM/DD) from an ISO-8601 string."""
    if not iso:
        return "", ""
    try:
        # e.g. "2026-05-16T11:29:00+08:00"
        date_part, rest = iso.split("T")
        time_part = rest[:5]          # "HH:MM"
        d = date_part.replace("-", "/")
        return time_part, d
    except Exception:
        return "", ""


def _extract_flights(airport_events: list) -> list[FlightLeg]:
    """
    Build FlightLeg objects from the airport events list.
    Each unique flight number gets one leg.
    """
    # Collect raw data keyed by flight number
    legs: dict[str, dict] = {}

    for ap in airport_events:
        airport_code = ap.get("airportCode", "")
        for ev in ap.get("events", []):
            fn = ev.get("flightNumber") or ""
            if not fn:
                continue
            ev_type = ev.get("eventType", "")
            iso = ev.get("time", "")

            if fn not in legs:
                legs[fn] = {
                    "flight_no": fn,
                    "from_airport": "",
                    "to_airport": "",
                    "departure_date": "",
                    "departure_time": "",
                    "departure_status": "scheduled",
                    "arrival_date": "",
                    "arrival_time": "",
                    "arrival_status": "scheduled",
                }

            leg = legs[fn]
            if ev_type in _DEP_EVENTS:
                leg["from_airport"] = airport_code
                t, d = _fmt_time(iso)
                leg["departure_time"] = t
                leg["departure_date"] = d
                leg["departure_status"] = "actual" if ev_type == "DEP" else "scheduled"
            elif ev_type in _ARR_EVENTS:
                leg["to_airport"] = airport_code
                t, d = _fmt_time(iso)
                leg["arrival_time"] = t
                leg["arrival_date"] = d
                leg["arrival_status"] = "actual"

    return [
        FlightLeg(
            flight_no=v["flight_no"],
            from_airport=v["from_airport"],
            to_airport=v["to_airport"],
            departure_date=v["departure_date"],
            departure_time=v["departure_time"],
            departure_status=v["departure_status"],
            arrival_date=v["arrival_date"],
            arrival_time=v["arrival_time"],
            arrival_status=v["arrival_status"],
            flight_time="",
        )
        for v in legs.values()
    ]


def _last_status(airport_events: list) -> str:
    """Return the most recent event type string."""
    _label = {
        "BKD": "Booked", "FOH": "On Hand", "RCS": "Ready for Carriage",
        "MAN": "Manifested", "DEP": "Departed", "ARR": "Arrived",
        "RCF": "Received from Flight", "NFD": "Ready for Pickup",
        "DLV": "Delivered", "TFD": "Transfer to Carrier",
    }
    last_type = ""
    last_time = ""
    for ap in airport_events:
        for ev in ap.get("events", []):
            t = ev.get("time", "")
            if t > last_time:
                last_time = t
                last_type = ev.get("eventType", "")
    return _label.get(last_type, last_type)
