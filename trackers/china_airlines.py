"""
China Airlines Cargo  —  prefix 297
GraphQL (iCargo Neo / IBS Software).
Anonymous JWT via loginAsAnonymousUser mutation (~10 h TTL), cached module-level.
"""
import asyncio
import time
from typing import Optional

import httpx
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

_GQL = "https://icargowebportal.china-airlines.com/portalgateway/graphql"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://icargowebportal.china-airlines.com",
    "Referer": "https://icargowebportal.china-airlines.com/icargoneoportal/app/main/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ── token cache ───────────────────────────────────────────────────────────

class _TokenCache:
    token: Optional[str] = None
    expires_at: float = 0
    _lock: asyncio.Lock = None

    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def valid(self) -> bool:
        return bool(self.token) and time.time() < self.expires_at

_token_cache = _TokenCache()


async def _get_token() -> str:
    async with _token_cache.lock():
        if not _token_cache.valid():
            await _refresh_token()
        return _token_cache.token


async def _refresh_token():
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(_GQL, headers=_HEADERS, json={
            "operationName": "loginAsAnonymousUser",
            "query": (
                "mutation loginAsAnonymousUser {"
                "  loginAsAnonymousUser {"
                "    security { id_token exp }"
                "    errors { error_code }"
                "  }"
                "}"
            ),
            "variables": {},
        })
    if resp.status_code != 200:
        raise HTTPException(502, f"China Airlines: token request HTTP {resp.status_code}")
    result = (resp.json().get("data") or {}).get("loginAsAnonymousUser") or {}
    security = result.get("security") or {}
    token = security.get("id_token")
    exp = int(security.get("exp") or 3600)
    if not token:
        raise HTTPException(502, "China Airlines: failed to get anonymous token")
    _token_cache.token = token
    _token_cache.expires_at = time.time() + exp - 300  # 5-min buffer


async def _gql(token: str, operation: str, query: str, variables: dict) -> dict:
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(_GQL, headers=headers, json={
            "operationName": operation,
            "query": query,
            "variables": variables,
        })
    if resp.status_code != 200:
        raise HTTPException(502, f"China Airlines: {operation} HTTP {resp.status_code}")
    return resp.json()


# ── GraphQL queries ───────────────────────────────────────────────────────

_Q_SUMMARY = """\
query GetShipmentsByAwbs($shipmentNumbers: [String]!) {
  GetShipmentsByAwbs(shipmentNumbers: $shipmentNumbers) {
    awb_number pieces stated_weight
    origin_airport_code destination_airport_code
    milestones { milestone status }
  }
}"""

_Q_SPLITS = """\
query GetShipmentSplitsByAwb($shipmentNumber: String!) {
  GetShipmentSplitsByAwb(shipmentNumber: $shipmentNumber) {
    split_number pieces milestone_status
    split_details {
      origin_airport_code milestone_status milestone_time
      carrier_code flight_number
    }
  }
}"""

_Q_ACTIVITY = """\
query GetShipmentActivityByAwb($shipmentNumber: String!) {
  GetShipmentActivityByAwb(shipmentNumber: $shipmentNumber) {
    items {
      event pieces airport_code
      uld_number
      flight { flight_carrier_code flight_number }
    }
  }
}"""


# ── tracker ───────────────────────────────────────────────────────────────

class ChinaAirlinesTracker(AirlineTracker):
    prefixes = ["297"]
    name = "China Airlines Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb_display = f"{prefix}-{number}"
        shipment_no = f"{prefix}{number}"  # no dash: "29767379535"

        token = await _get_token()
        summary_r, splits_r, activity_r = await asyncio.gather(
            _gql(token, "GetShipmentsByAwbs", _Q_SUMMARY,
                 {"shipmentNumbers": [shipment_no]}),
            _gql(token, "GetShipmentSplitsByAwb", _Q_SPLITS,
                 {"shipmentNumber": shipment_no}),
            _gql(token, "GetShipmentActivityByAwb", _Q_ACTIVITY,
                 {"shipmentNumber": shipment_no}),
        )

        summaries = (summary_r.get("data") or {}).get("GetShipmentsByAwbs") or []
        if not summaries:
            raise HTTPException(404, f"AWB {awb_display} not found on China Airlines Cargo")
        summary = summaries[0]

        splits = (splits_r.get("data") or {}).get("GetShipmentSplitsByAwb") or []
        items = (
            (activity_r.get("data") or {})
            .get("GetShipmentActivityByAwb", {})
            .get("items") or []
        )

        return self._parse(awb_display, summary, splits, items)

    def _parse(self, awb: str, summary: dict, splits: list, items: list) -> TrackingResult:
        milestones = summary.get("milestones") or []
        status = ""
        for m in reversed(milestones):
            if m.get("status") == "done":
                status = m.get("milestone", "").title()
                break

        # Build legs from splits (reliable routing + dep times)
        # then enrich arrival times from activity events
        flights = _build_legs(splits, items)

        weight_kg: Optional[float] = None
        try:
            v = float(summary.get("stated_weight") or 0)
            weight_kg = v or None
        except (ValueError, TypeError):
            pass

        pieces: Optional[int] = None
        try:
            v = int(summary.get("pieces") or 0)
            pieces = v or None
        except (ValueError, TypeError):
            pass

        return TrackingResult(
            awb=awb,
            from_airport=summary.get("origin_airport_code", ""),
            from_name="",
            to_airport=summary.get("destination_airport_code", ""),
            to_name="",
            status=status,
            status_code=status,
            flights=flights,
            total_pieces=pieces,
            total_weight_kg=weight_kg,
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
        shipment_no = f"{prefix}{awb_number}"
        token = await _get_token()
        activity_r = await _gql(
            token, "GetShipmentActivityByAwb", _Q_ACTIVITY,
            {"shipmentNumber": shipment_no},
        )
        items = (
            (activity_r.get("data") or {})
            .get("GetShipmentActivityByAwb", {})
            .get("items") or []
        )

        ulds: list[ULDItem] = []
        seen: set[str] = set()
        for item in items:
            flt = item.get("flight") or {}
            fn = ((flt.get("flight_carrier_code") or "") + (flt.get("flight_number") or "")).strip()
            if fn != flight_no:
                continue
            if item.get("event") not in ("DEP", "MAN"):
                continue
            uld = (item.get("uld_number") or "").strip()
            if uld and uld not in seen:
                seen.add(uld)
                ulds.append(ULDItem(uld=uld, pieces=item.get("pieces") or 0))

        return ULDResult(
            flight_no=flight_no,
            departure_date=departure_date,
            departure=departure,
            arrival=arrival,
            ulds=ulds,
        )


# ── helpers ───────────────────────────────────────────────────────────────

def _build_legs(splits: list, items: list) -> list[FlightLeg]:
    """
    Build FlightLeg list from GetShipmentSplitsByAwb data (reliable routing
    and departure times), then fill in arrival times from activity events.
    """
    if not splits:
        return []

    # Use first split (non-split shipments have just one)
    split_details = splits[0].get("split_details") or []

    # Build legs: detail[i] → detail[i+1]
    legs: dict[str, dict] = {}
    ordered_fns: list[str] = []

    for i, sd in enumerate(split_details):
        fn = ((sd.get("carrier_code") or "") + (sd.get("flight_number") or "")).strip()
        if not fn:
            continue  # last entry is destination node (no outbound flight)

        next_sd = split_details[i + 1] if i + 1 < len(split_details) else {}
        dep_t, dep_d = _fmt_ci(sd.get("milestone_time", ""))
        is_actual = (sd.get("milestone_status", "") == "Departed")

        # Arrival of last leg = final node's milestone_time
        arr_t, arr_d = "", ""
        arr_status = "scheduled"
        if i + 1 == len(split_details) - 1:
            final = split_details[-1]
            arr_t, arr_d = _fmt_ci(final.get("milestone_time", ""))
            if final.get("milestone_status") in ("Delivered", "Arrived"):
                arr_status = "actual"

        leg = {
            "flight_no":        fn,
            "from_airport":     sd.get("origin_airport_code") or "",
            "to_airport":       next_sd.get("origin_airport_code") or "",
            "departure_date":   dep_d,
            "departure_time":   dep_t,
            "departure_status": "actual" if is_actual else "scheduled",
            "arrival_date":     arr_d,
            "arrival_time":     arr_t,
            "arrival_status":   arr_status,
            "flight_time":      "",
            "flrs_id":          1,
        }
        legs[fn] = leg
        ordered_fns.append(fn)

    # Enrich intermediate arrival times from activity RCF/ARR events
    for item in items:
        flt = item.get("flight") or {}
        fn = ((flt.get("flight_carrier_code") or "") + (flt.get("flight_number") or "")).strip()
        if fn not in legs:
            continue
        ev = item.get("event", "")
        if ev not in ("ARR", "RCF"):
            continue
        airport = item.get("airport_code") or ""
        if airport:
            legs[fn]["to_airport"] = legs[fn]["to_airport"] or airport
            legs[fn]["arrival_status"] = "actual"

    return [FlightLeg(**legs[fn]) for fn in ordered_fns]


def _fmt_ci(s: str) -> tuple[str, str]:
    """'10-05-2026 04:06:00' → ('04:06', '2026/05/10')."""
    if not s:
        return "", ""
    try:
        date_part, time_part = s.split(" ")
        d, m, y = date_part.split("-")
        return time_part[:5], f"{y}/{m}/{d}"
    except Exception:
        return "", ""
