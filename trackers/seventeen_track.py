"""
17track REST API helper  —  server-to-server, no Playwright needed.
Used by AA Cargo (001) and Cathay Pacific (160) to bypass Akamai/cloud IP blocks.

API key is read from the SEVENTEEN_TRACK_API_KEY environment variable.
Free tier: 200 queries/month.
"""
import asyncio
import os
from typing import Optional

import httpx

from .base import FlightLeg, TrackingResult

_API_BASE = "https://api.17track.net/track/v2.2"


def _key() -> str:
    return os.environ.get("SEVENTEEN_TRACK_API_KEY", "")


def _parse_iso(s: str) -> tuple[str, str]:
    """Return (HH:MM, YYYY/MM/DD) from ISO datetime string."""
    if not s:
        return "", ""
    try:
        if "T" in s:
            date_part, rest = s.split("T", 1)
            return rest[:5], date_part.replace("-", "/")
        if " " in s:
            date_part, rest = s.split(" ", 1)
            return rest[:5], date_part.replace("-", "/")
    except Exception:
        pass
    return "", ""


def _build_result(awb: str, track: dict) -> Optional[TrackingResult]:
    awb_info       = track.get("awb_info") or {}
    latest_status  = track.get("latest_status") or {}
    transport_infos = track.get("transport_infos") or track.get("awb_transport_infos") or []

    status = (latest_status.get("status") or "").strip()
    if not status or status in ("NotFound", "Expired"):
        return None

    origin      = awb_info.get("origin_iata") or ""
    destination = awb_info.get("destination_iata") or ""

    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    try:
        pieces = int(awb_info.get("pieces") or 0) or None
    except (ValueError, TypeError):
        pass
    try:
        weight_kg = float(awb_info.get("weight") or 0) or None
    except (ValueError, TypeError):
        pass

    legs: list[FlightLeg] = []
    for seg in transport_infos:
        fn      = (seg.get("flight_no") or "").strip()
        from_ap = seg.get("origin_iata") or ""
        to_ap   = seg.get("destination_iata") or ""
        if not fn and not from_ap:
            continue

        atd = seg.get("atd") or ""
        ata = seg.get("ata") or ""
        std = seg.get("std") or ""
        sta = seg.get("sta") or ""

        dep_time, dep_date = _parse_iso(atd or std)
        arr_time, arr_date = _parse_iso(ata or sta)

        legs.append(FlightLeg(
            flight_no        = fn,
            from_airport     = from_ap,
            to_airport       = to_ap,
            departure_date   = dep_date,
            departure_time   = dep_time,
            departure_status = "actual" if atd else "scheduled",
            arrival_date     = arr_date,
            arrival_time     = arr_time,
            arrival_status   = "actual" if ata else "scheduled",
            flight_time      = "",
            pieces           = pieces,
            weight_kg        = weight_kg,
            flrs_id          = 1,
        ))

    return TrackingResult(
        awb             = awb,
        from_airport    = origin,
        from_name       = "",
        to_airport      = destination,
        to_name         = "",
        status          = status,
        status_code     = status,
        flights         = legs,
        total_pieces    = pieces,
        total_weight_kg = weight_kg,
    )


async def fetch_tracking(awb: str) -> Optional[TrackingResult]:
    """
    Call 17track REST API to get tracking info for the given AWB.
    Returns None if API key not configured, AWB not found, or on error.
    """
    api_key = _key()
    if not api_key:
        return None

    headers = {
        "17token": api_key,
        "Content-Type": "application/json",
    }
    payload = [{"number": awb}]

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Register the AWB (idempotent — safe to call every time)
            await client.post(f"{_API_BASE}/register", json=payload, headers=headers)

            # Retry up to 3 times: 17track may need ~15s to fetch from carrier
            # on first registration before getRealTimeTrackInfo returns data.
            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(15)

                resp = await client.post(
                    f"{_API_BASE}/getRealTimeTrackInfo",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code != 200:
                    return None

                data = resp.json()
                if data.get("code") != 0:
                    return None

                accepted = (data.get("data") or {}).get("accepted") or []
                if not accepted:
                    return None

                track = accepted[0].get("track") or {}
                result = _build_result(awb, track)
                if result is not None:
                    return result
                # NotFound/Expired on this attempt — retry if more attempts left

            return None

    except Exception:
        return None
