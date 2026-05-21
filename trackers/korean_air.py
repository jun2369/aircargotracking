"""
Korean Air Cargo  —  prefix 180
Drupal/React portal. Session + anonymous JWT cookie obtained by
GETting the tracking page; re-used for up to 1 hour.
ULD detail fetched via /cargoportal/services/getboardinglist.
"""
import asyncio
import json
import random
import re
import string
import time
from typing import Optional

import httpx
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

_BASE    = "https://cargo.koreanair.com"
_TRACK_P = f"{_BASE}/en/tracking"
_API     = f"{_BASE}/cargoportal/services"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REQ_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": _BASE,
    "Referer": _TRACK_P,
}

# ── ULD cache: awb → {flight_no → {uldNos, carCode, fltNoRaw, fltType}} ──
_ke_uld_cache: dict[str, dict[str, dict]] = {}

# ── session cache ─────────────────────────────────────────────────────────

class _Session:
    session_id: str = ""
    region: str = "America"
    cookies: dict = {}
    expires_at: float = 0
    _lock: asyncio.Lock = None

    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def valid(self) -> bool:
        return bool(self.session_id) and time.time() < self.expires_at

_sess = _Session()


async def _get_session() -> tuple[str, str, dict]:
    """Returns (session_id, region, cookies_dict)."""
    async with _sess.lock():
        if not _sess.valid():
            await _refresh_session()
        return _sess.session_id, _sess.region, dict(_sess.cookies)


async def _refresh_session():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(_TRACK_P, headers={"User-Agent": _UA})

    m = re.search(
        r'data-drupal-selector="drupal-settings-json"[^>]*>([^<]+)<',
        resp.text,
    )
    if not m:
        raise HTTPException(502, "Korean Air: cannot find drupalSettings in page")
    try:
        settings = json.loads(m.group(1))
    except Exception:
        raise HTTPException(502, "Korean Air: invalid drupalSettings JSON")

    _sess.session_id = settings.get("session_id", "")
    _sess.region     = settings.get("region", "America")
    _sess.cookies    = dict(resp.cookies)
    _sess.expires_at = time.time() + 3600


def _general_info(session_id: str) -> dict:
    return {
        "sessionId": session_id,
        "lang": "EN",
        "time": time.strftime("%d %b %Y %H:%M"),
        "txnId": "".join(random.choices(string.ascii_lowercase + string.digits, k=16)),
    }


def _user_info(region: str) -> dict:
    return {
        "userId": "", "agentCode": "",
        "region": region, "branch": "", "userType": "GUEST",
    }


# ── tracker ───────────────────────────────────────────────────────────────

class KoreanAirCargoTracker(AirlineTracker):
    prefixes = ["180"]
    name = "Korean Air Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb_display = f"{prefix}-{number}"
        _ke_uld_cache.pop(awb_display, None)  # clear before fresh fetch
        session_id, region, cookies = await _get_session()

        async with httpx.AsyncClient(
            timeout=20, cookies=cookies, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.post(
                f"{_API}/trackawb",
                headers=_REQ_HEADERS,
                json={
                    "generalInfo": _general_info(session_id),
                    "userInfo": _user_info(region),
                    "payLoad": [{"awbPrefix": prefix, "awbDocNo": number}],
                },
            )

        if resp.status_code != 200:
            raise HTTPException(502, f"Korean Air: API returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            raise HTTPException(502, "Korean Air: non-JSON response")

        payload_list = data.get("payLoad") or []
        if not payload_list or not payload_list[0].get("origin"):
            raise HTTPException(404, f"AWB {awb_display} not found on Korean Air Cargo")

        p = payload_list[0]
        # Cache ULD info from DEP events so fetch_uld() avoids a redundant re-fetch
        uld_by_fn: dict[str, dict] = {}
        for ev in (p.get("eventDetails") or []):
            if ev.get("eventCode") != "DEP":
                continue
            flt_info = ev.get("fltDetail") or {}
            fn = (flt_info.get("carCode", "") + flt_info.get("fltNo", "")).strip()
            if fn and (ev.get("uldNo") or []):
                uld_by_fn[fn] = {
                    "uldNos":    ev["uldNo"],
                    "carCode":   flt_info.get("carCode", ""),
                    "fltNoRaw":  flt_info.get("fltNo", ""),
                    "fltType":   flt_info.get("fltType", "FREIGHTER"),
                }
        if uld_by_fn:
            _ke_uld_cache[awb_display] = uld_by_fn

        return self._parse(awb_display, p)

    def _parse(self, awb: str, p: dict) -> TrackingResult:
        flt_details = p.get("fltDetails") or []
        flights = [self._parse_flt(f) for f in flt_details if f]

        return TrackingResult(
            awb=awb,
            from_airport=p.get("origin", ""),
            from_name="",
            to_airport=p.get("destination", ""),
            to_name="",
            status=p.get("shipmentStatus", ""),
            status_code=p.get("shipmentStatus", ""),
            flights=flights,
            total_pieces=p.get("pieces"),
            total_weight_kg=_weight_kg(p.get("wgtDetail")),
        )

    def _parse_flt(self, f: dict) -> FlightLeg:
        flt_info = f.get("fltDetail") or {}
        fn = (flt_info.get("carCode", "") + flt_info.get("fltNo", "")).strip()

        dep_actual = f.get("actualDepDate", "")
        dep_sched  = f.get("depDate", "")
        arr_actual = f.get("actualArrvlDate", "")
        arr_sched  = f.get("arrvlDate", "")

        dep_t, dep_d = _fmt_ke(dep_actual or dep_sched)
        arr_t, arr_d = _fmt_ke(arr_actual or arr_sched)

        return FlightLeg(
            flight_no        = fn,
            from_airport     = f.get("origin", ""),
            to_airport       = f.get("destination", ""),
            departure_date   = dep_d,
            departure_time   = dep_t,
            departure_status = "actual" if dep_actual else "scheduled",
            arrival_date     = arr_d,
            arrival_time     = arr_t,
            arrival_status   = "actual" if arr_actual else "scheduled",
            flight_time      = "",
            pieces           = f.get("pieces"),
            weight_kg        = f.get("weight"),
            flrs_id          = 1,
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
        awb_display = f"{prefix}-{awb_number}"
        cached = (_ke_uld_cache.get(awb_display) or {}).get(flight_no)
        if not cached:
            return ULDResult(
                flight_no=flight_no, departure_date=departure_date,
                departure=departure, arrival=arrival, ulds=[],
            )

        uld_nos    = cached["uldNos"]
        car_code   = cached["carCode"]
        flt_no_raw = cached["fltNoRaw"]
        flt_type   = cached["fltType"]

        session_id, region, cookies = await _get_session()
        client_kwargs = dict(
            timeout=20, cookies=cookies, headers={"User-Agent": _UA}
        )

        # Get per-ULD piece counts from getboardinglist
        flt_date_str = _date_to_ke(departure_date)
        async with httpx.AsyncClient(**client_kwargs) as client:
            board_resp = await client.post(
                f"{_API}/getboardinglist",
                headers=_REQ_HEADERS,
                json={
                    "generalInfo": _general_info(session_id),
                    "userInfo": _user_info(region),
                    "payLoad": {
                        "reqIndicator": "ULD_INFO_LIST",
                        "awbPrefix": prefix,
                        "awbDocNo": awb_number,
                        "uldNo": uld_nos,
                        "fltDate": flt_date_str,
                        "fltDetail": {
                            "fltNo": flt_no_raw,
                            "carCode": car_code,
                            "fltType": flt_type,
                        },
                    },
                },
            )

        ulds: list[ULDItem] = []
        if board_resp.status_code == 200:
            try:
                board_data = board_resp.json()
                on_board = (board_data.get("payLoad") or {}).get("onBoardDetails") or []
                for ob in on_board:
                    seg = (ob.get("segmentDetails") or {})
                    for seg_info in seg.get("segmentInfo") or []:
                        uld_no = (seg_info.get("uldNo") or "").strip()
                        pcs    = seg_info.get("pieces") or 0
                        if uld_no:
                            ulds.append(ULDItem(uld=uld_no, pieces=int(pcs)))
            except Exception:
                pass

        return ULDResult(
            flight_no=flight_no,
            departure_date=departure_date,
            departure=departure,
            arrival=arrival,
            ulds=ulds,
        )


# ── helpers ───────────────────────────────────────────────────────────────

_MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}
_MONTHS_R = {v: k for k, v in _MONTHS.items()}


def _fmt_ke(s: str) -> tuple[str, str]:
    """'10 May 2026 05:25' → ('05:25', '2026/05/10')."""
    if not s:
        return "", ""
    try:
        parts = s.strip().split()
        day, mon, year, t = parts[0], parts[1], parts[2], parts[3]
        mo = _MONTHS.get(mon[:3], "00")
        return t[:5], f"{year}/{mo}/{day.zfill(2)}"
    except Exception:
        return "", ""


def _date_to_ke(d: str) -> str:
    """'2026/05/10' → '10 May 2026'."""
    try:
        y, m, day = d.split("/")
        return f"{int(day)} {_MONTHS_R.get(m, m)} {y}"
    except Exception:
        return d


def _weight_kg(wgt: Optional[dict]) -> Optional[float]:
    if not wgt:
        return None
    try:
        v = float(wgt.get("quantity") or 0)
        return v or None
    except (ValueError, TypeError):
        return None
