"""
Kalitta Air  —  prefix 272
Cargo Network Manager (CNM) system at kalitta-cnm.com/tracktrace/.
POST form → HTML response. No authentication, no CAPTCHA.
Flight info extracted from event message text via regex.
"""
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

_URL = "https://kalitta-cnm.com/tracktrace/default.aspx"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STATUS_MAP: dict[str, str] = {
    "shipmentdelivered":  "Delivered",
    "arrived":            "Arrived",
    "departed":           "Departed",
    "cargocleared":       "Departed",
    "consignmentcustoms": "Customs",
    "customs":            "Customs",
    "awbdelivered":       "Notified for Delivery",
    "booked":             "Booked",
    "received":           "Received",
    "manifested":         "Manifested",
}

_MONTHS = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}

# "received from flight K4981 at LAX on 15MAY2026 14:48:28"
_ARR_RE = re.compile(
    r'received from flight\s+(\w+)\s+at\s+([A-Z]{3})',
    re.IGNORECASE,
)
# "transferred to carrier K4 at ANC on ..."
_DEP_RE = re.compile(
    r'transferred to carrier\s+\w+\s+at\s+([A-Z]{3})',
    re.IGNORECASE,
)


def _parse_event_dt(text: str) -> tuple[str, str]:
    """'15 MAY 2026 22:33' → ('22:33', '2026/05/15')."""
    m = re.match(r'(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\s+(\d{1,2}:\d{2})', text.strip().upper())
    if not m:
        return "", ""
    day = m.group(1).zfill(2)
    mo  = _MONTHS.get(m.group(2), "00")
    return m.group(4).zfill(5), f"{m.group(3)}/{mo}/{day}"


async def _fetch_html(awb: str) -> str:
    data = {
        "txtAWB":        awb,
        "action":        "SearchAWB",
        "txtSupportsdst": "true",
        "txtHemisphere": "N",
        "txtOffset":     "-300",
        "txtDstoffset":  "-240",
        "txtTZName":     "America/New_York",
    }
    headers = {"User-Agent": _UA, "Origin": "https://kalitta-cnm.com", "Referer": _URL}
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(_URL, data=data, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(502, f"Kalitta Air CNM: HTTP {resp.status_code}")
    return resp.text


def _parse(awb: str, html: str) -> TrackingResult:
    soup = BeautifulSoup(html, "html.parser")

    # AWB detail fields — div.fvp > div.fn (label) + div.fv span (value)
    details: dict[str, str] = {}
    for fvp in soup.find_all(class_="fvp"):
        fn = fvp.find(class_="fn")
        fv = fvp.find(class_="fv")
        if fn and fv:
            span = fv.find("span")
            val = span.get_text(strip=True) if span else fv.get_text(strip=True)
            details[fn.get_text(strip=True)] = val

    if not details:
        raise HTTPException(404, f"AWB {awb} not found on Kalitta Air")

    origin      = details.get("Origin", "")
    destination = details.get("Destination", "")

    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    try:
        pieces = int(details.get("Pieces", "").replace(",", ""))
    except (ValueError, TypeError):
        pass
    try:
        weight_kg = float(details.get("Weight", "").split()[0].replace(",", ""))
    except (ValueError, IndexError):
        pass

    # Status events (table.flighttable)
    events: list[dict] = []
    table = soup.find("table", class_="flighttable")
    if table:
        for tr in table.find_all("tr"):
            classes = tr.get("class") or []
            tds = tr.find_all("td")
            if not classes or not tds:
                continue
            span = tds[0].find("span")
            dt = span.get_text(strip=True) if span else tds[0].get_text(strip=True)
            msg = tds[1].get_text(strip=True) if len(tds) > 1 else ""
            events.append({"cls": classes[0], "dt": dt, "msg": msg})

    status = ""
    if events:
        status = _STATUS_MAP.get(events[0]["cls"], events[0]["cls"])

    flights = _build_legs(events, origin, destination)

    return TrackingResult(
        awb=awb,
        from_airport=origin,
        from_name="",
        to_airport=destination,
        to_name="",
        status=status,
        status_code=events[0]["cls"] if events else "",
        flights=flights,
        total_pieces=pieces,
        total_weight_kg=weight_kg,
    )


def _build_legs(events: list[dict], origin: str, destination: str) -> list[FlightLeg]:
    """Build flight legs from CNM event messages."""
    arrivals:  list[dict] = []   # {fn, to, arr_time, arr_date}
    dep_airport: str = ""
    dep_time: str = ""
    dep_date: str = ""

    for ev in events:
        msg = ev["msg"]
        # "received from flight K4981 at LAX on ..."
        m = _ARR_RE.search(msg)
        if m:
            t, d = _parse_event_dt(ev["dt"])
            arrivals.append({
                "fn":       m.group(1).upper(),
                "to":       m.group(2).upper(),
                "arr_time": t,
                "arr_date": d,
            })

        # "transferred to carrier K4 at ANC on ..."
        m2 = _DEP_RE.search(msg)
        if m2 and not dep_airport:
            dep_airport = m2.group(1).upper()
            dep_time, dep_date = _parse_event_dt(ev["dt"])

    if not arrivals:
        return []

    legs: list[FlightLeg] = []
    for i, arr in enumerate(arrivals):
        # departure airport: CNM transfer point, or origin if only one leg
        from_ap = dep_airport if dep_airport else origin
        legs.append(FlightLeg(
            flight_no        = arr["fn"],
            from_airport     = from_ap,
            to_airport       = arr["to"],
            departure_date   = dep_date if i == 0 else "",
            departure_time   = dep_time if i == 0 else "",
            departure_status = "actual" if dep_time else "scheduled",
            arrival_date     = arr["arr_date"],
            arrival_time     = arr["arr_time"],
            arrival_status   = "actual" if arr["arr_time"] else "scheduled",
            flight_time      = "",
            flrs_id          = 0,
        ))
    return legs


# ── tracker ───────────────────────────────────────────────────────────────

class KalittaAirTracker(AirlineTracker):
    prefixes = ["272"]
    name = "Kalitta Air"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb = f"{prefix}-{number}"
        html = await _fetch_html(awb)
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
