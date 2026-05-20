"""
EVA Air Cargo / BR Cargo  —  prefix 695
QuickTrackingGet:       POST  /NEC_WEB/Tracking/QuickTracking/QuickTrackingGet
QuickTrackingDetail:    POST  /NEC_WEB/Tracking/QuickTracking/QuickTrackingDetail
QuickTrackingULDDetail: POST  /NEC_WEB/Tracking/QuickTracking/QuickTrackingULDDetail
"""
import asyncio
import json
import re
import urllib.parse
import uuid

import httpx
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

_BASE   = "https://www.brcargo.com"
_INDEX  = f"{_BASE}/NEC_WEB/Tracking/QuickTracking/Index"
_TRACK  = f"{_BASE}/NEC_WEB/Tracking/QuickTracking/QuickTrackingGet"
_DETAIL = f"{_BASE}/NEC_WEB/Tracking/QuickTracking/QuickTrackingDetail"
_ULD    = f"{_BASE}/NEC_WEB/Tracking/QuickTracking/QuickTrackingULDDetail"
_UA     = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _base_headers() -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "nectabid": str(uuid.uuid4()),
        "origin": _BASE,
        "referer": _INDEX,
        "x-requested-with": "XMLHttpRequest",
        "user-agent": _UA,
    }


def _tracking_body(prefix: str, number: str) -> str:
    return (
        f"prefix={prefix}"
        f"&AWBNo={number}"
        f"&_isRobot=N"
        f"&_verification_code="
        f"&_verification_id={uuid.uuid4()}"
    )


class BRCargoTracker(AirlineTracker):
    prefixes = ["695"]
    name     = "EVA Air Cargo (BR)"

    # ── main tracking ─────────────────────────────────────────────────

    async def track(self, prefix: str, number: str) -> TrackingResult:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            await client.get(_INDEX, headers={"user-agent": _UA})

            # call QuickTrackingGet and QuickTrackingDetail in parallel
            body = _tracking_body(prefix, number)
            r_get, r_detail = await asyncio.gather(
                client.post(_TRACK,  headers=_base_headers(), content=body),
                client.post(_DETAIL, headers=_base_headers(), content=body),
                return_exceptions=True,
            )

        # --- parse QuickTrackingGet (JSON) ---
        if isinstance(r_get, Exception) or r_get.status_code != 200:
            raise HTTPException(502, "BR Cargo QuickTrackingGet failed")
        try:
            data = r_get.json()
        except Exception:
            raise HTTPException(502, "BR Cargo returned non-JSON")
        if not data.get("AWBNo"):
            raise HTTPException(404, f"AWB {prefix}-{number} not found on BR Cargo")

        # --- extract FLRS_IDs from QuickTrackingDetail (HTML) ---
        flrs_map: dict[tuple[str, str], int] = {}
        if not isinstance(r_detail, Exception) and r_detail.status_code == 200:
            flrs_map = _extract_flrs_ids(r_detail.text)

        return self._parse(data, flrs_map)

    def _parse(self, d: dict, flrs_map: dict[tuple[str, str], int]) -> TrackingResult:
        flights = []
        for f in d.get("FlightInfoList", []):
            dep = f.get("DepartureTime", "")
            arr = f.get("ArrivalTime", "")
            key = (f.get("FlightNo", "").strip(), f.get("DepartureDate", ""))
            flights.append(FlightLeg(
                flight_no        = key[0],
                from_airport     = f.get("Departure", ""),
                to_airport       = f.get("Arrival", ""),
                departure_date   = key[1],
                departure_time   = self.clean(dep),
                departure_status = self.classify(dep),
                arrival_date     = (f.get("ArrivalDate") or "").split()[0],
                arrival_time     = self.clean(arr),
                arrival_status   = self.classify(arr),
                flight_time      = f.get("FlightTime", ""),
                pieces           = f.get("Pieces"),
                weight_kg        = f.get("Weight"),
                flrs_id          = flrs_map.get(key, 0),
            ))
        return TrackingResult(
            awb             = d.get("AWBNo", ""),
            from_airport    = d.get("From", ""),
            from_name       = d.get("FromName", ""),
            to_airport      = d.get("To", ""),
            to_name         = d.get("ToName", ""),
            status          = d.get("CurrentStatus", ""),
            status_code     = d.get("Status", ""),
            total_pieces    = d.get("TotalPieces"),
            total_weight_kg = d.get("TotalWeight"),
            flights         = flights,
        )

    # ── ULD detail ────────────────────────────────────────────────────

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
        body = "&".join([
            f"prefix={urllib.parse.quote(prefix)}",
            f"AWBNo={urllib.parse.quote(awb_number)}",
            f"FlightNo={urllib.parse.quote(flight_no)}",
            f"Departure={urllib.parse.quote(departure)}",
            f"Arrival={urllib.parse.quote(arrival)}",
            f"DepartureDate={urllib.parse.quote(departure_date)}",
            f"FLRS_ID={flrs_id}",
        ])
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            await client.get(_INDEX, headers={"user-agent": _UA})
            resp = await client.post(_ULD, headers=_base_headers(), content=body)

        if resp.status_code != 200:
            raise HTTPException(502, f"ULD detail returned HTTP {resp.status_code}")

        return _parse_uld_html(resp.text, flight_no, departure_date, departure, arrival)


# ── module-level helpers ──────────────────────────────────────────────

def _extract_flrs_ids(html: str) -> dict[tuple[str, str], int]:
    """
    Extract FLRS_ID from the QuickTrackingDetail HTML response.
    The page embeds: var QUTR01DetailData = { ... ConsignmentList [...] ... };
    """
    match = re.search(r'var\s+QUTR01DetailData\s*=\s*(\{.*?\});\s*\n', html, re.DOTALL)
    if not match:
        return {}
    try:
        detail = json.loads(match.group(1))
    except Exception:
        return {}

    result: dict[tuple[str, str], int] = {}
    for consignment in detail.get("ConsignmentList", []):
        for leg in consignment.get("ItineraryList", []):
            if not leg:
                continue
            fn  = (leg.get("FlightNo") or "").strip()
            dd  = leg.get("DepartureDate", "")
            fid = leg.get("FLRS_ID") or 0
            if fn and dd and fid:
                result[(fn, dd)] = fid
    return result


def _parse_uld_html(
    html: str,
    flight_no: str,
    departure_date: str,
    departure: str,
    arrival: str,
) -> ULDResult:
    rows = re.findall(
        r'<td>\s*([^<]+?)\s*</td>\s*<td>\s*(\d+)\s*</td>',
        html,
    )
    ulds = [
        ULDItem(uld=uld.strip(), pieces=int(pcs))
        for uld, pcs in rows
        if uld.strip().upper() not in ("ULD", "PIECES")
    ]
    return ULDResult(
        flight_no      = flight_no.strip(),
        departure_date = departure_date,
        departure      = departure,
        arrival        = arrival,
        ulds           = ulds,
    )
