"""
China Eastern Cargo  —  prefix 112
ASP.NET WebForms with image CAPTCHA (75×30 RGBA PNG, 4-char alphanumeric).
Flow: GET form → GET captcha → OCR with ddddocr → POST (retry up to 3×).
Response is GB2312-encoded HTML containing an XML fragment with 4 tables.
No ULD data available (flrs_id=0 on all legs).
"""
import asyncio
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException

from .base import AirlineTracker, FlightLeg, TrackingResult, ULDItem, ULDResult

_BASE = "http://cargo2.ceair.com/mu"
_TRACK_URL = _BASE + "/Service/getawbinfo.aspx?strCul=zh-CN&strValue="
_CAPTCHA_URL = _BASE + "/VerificationCodeAwb.aspx"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Chinese airport name → IATA code
_AIRPORT: dict[str, str] = {
    "上海浦东": "PVG", "浦东": "PVG",
    "上海虹桥": "SHA", "虹桥": "SHA",
    "洛杉矶":   "LAX",
    "北京首都": "PEK", "首都": "PEK",
    "北京大兴": "PKX",
    "广州":     "CAN",
    "成都":     "CTU",
    "深圳":     "SZX",
    "香港":     "HKG",
    "东京成田": "NRT", "成田": "NRT",
    "大阪关西": "KIX", "大阪": "KIX",
    "纽约":     "JFK",
    "旧金山":   "SFO",
    "芝加哥":   "ORD",
    "达拉斯":   "DFW",
    "亚特兰大": "ATL",
    "迪拜":     "DXB",
    "法兰克福": "FRA",
    "阿姆斯特丹": "AMS",
    "伦敦希思罗": "LHR", "希思罗": "LHR",
    "首尔仁川": "ICN", "仁川": "ICN",
    "悉尼":     "SYD",
    "多伦多":   "YYZ",
    "巴黎戴高乐": "CDG", "戴高乐": "CDG",
    "新加坡":   "SIN",
    "曼谷素万那普": "BKK", "曼谷": "BKK",
    "台北桃园": "TPE", "桃园": "TPE",
    "名古屋":   "NGO",
    "福冈":     "FUK",
    "大连":     "DLC",
    "沈阳":     "SHE",
    "昆明":     "KMG",
    "武汉":     "WUH",
    "西安":     "XIY",
    "郑州":     "CGO",
    "天津":     "TSN",
    "重庆":     "CKG",
    "哈尔滨":   "HRB",
    "济南":     "TNA",
    "南京":     "NKG",
    "杭州":     "HGH",
    "厦门":     "XMN",
    "南宁":     "NNG",
    "乌鲁木齐": "URC",
}

_STATUS_MAP: dict[str, str] = {
    "订舱确认": "Booked",
    "收货":     "Received",
    "货物配载": "Manifested",
    "出发":     "Departed",
    "货物到达": "Arrived",
    "到达":     "Arrived",
    "已转运":   "Transferred",
    "货物提取": "Delivered",
    "运单交付": "Delivered",
}


def _iata(name: str) -> str:
    name = name.strip()
    for k, v in _AIRPORT.items():
        if k in name:
            return v
    return name


def _parse_dt(dt_str: str) -> tuple[str, str]:
    """'20260516 15:20' → ('15:20', '2026/05/16'), or '' for empty."""
    s = dt_str.strip()
    if len(s) < 13:
        return "", ""
    date_part = s[:8]
    time_part = s[9:14]
    return time_part, f"{date_part[:4]}/{date_part[4:6]}/{date_part[6:8]}"


async def _fetch_tracking(prefix: str, number: str) -> str:
    """Return decoded HTML from China Eastern, handling captcha retry (up to 3×)."""
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
    except ImportError:
        raise HTTPException(502, "ddddocr not installed; run: pip install ddddocr")

    headers = {"User-Agent": _UA}
    last_err = "unknown"

    async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
        attempt = 0
        for _ in range(10):
            # 1. GET form page → session cookie + hidden fields
            r = await client.get(_TRACK_URL)
            html = r.content.decode("gb2312", errors="replace")
            vs  = re.search(r'__VIEWSTATE[^>]*value="([^"]+)"', html)
            evl = re.search(r'__EVENTVALIDATION[^>]*value="([^"]+)"', html)
            vsv  = vs.group(1)  if vs  else ""
            evlv = evl.group(1) if evl else ""

            # 2. GET captcha image → OCR in thread (ddddocr is synchronous)
            cap_r = await client.get(_CAPTCHA_URL)
            loop = asyncio.get_event_loop()
            raw: str = await loop.run_in_executor(
                None, ocr.classification, cap_r.content
            )
            # Strip non-alphanumeric noise from OCR output
            code = re.sub(r'[^a-zA-Z0-9]', '', raw)
            if len(code) != 4:
                continue  # bad OCR, retry without counting as an attempt

            attempt += 1

            # 3. POST the form
            data = {
                "__VIEWSTATE":      vsv,
                "__EVENTVALIDATION": evlv,
                "txtstrAwbPfx0":    prefix,
                "txtstrbum0":       number,
                "strVCodeAWB":      code,
                "btnQry":           "查询",
                "rowid":            "1",
                "txtAwbs":          f"{prefix}-{number}",
            }
            r2 = await client.post(
                _TRACK_URL, data=data,
                headers={"Referer": _TRACK_URL},
            )
            html2 = r2.content.decode("gb2312", errors="replace")

            if "resultb" in html2:
                return html2

            last_err = f"attempt {attempt}: captcha={code!r}, no result in response"
            if attempt >= 6:
                break

    raise HTTPException(502, f"China Eastern: captcha failed after {attempt} attempts ({last_err})")


def _parse_html(awb: str, html: str) -> TrackingResult:
    soup = BeautifulSoup(html, "html.parser")
    result_div = soup.find(class_="resultb")
    if not result_div:
        raise HTTPException(404, f"AWB {awb} not found on China Eastern")

    # 4 inner tables, each with border="1"
    tables = result_div.find_all("table", attrs={"border": "1"})

    pieces:    Optional[int]   = None
    weight_kg: Optional[float] = None
    origin      = ""
    destination = ""

    # Table 0: AWB summary — row[2] = [pieces, weight, origin, dest, commodity]
    if tables:
        rows = tables[0].find_all("tr")
        if len(rows) >= 3:
            cells = [c.get_text(strip=True) for c in rows[2].find_all("td")]
            if len(cells) >= 4:
                try:
                    pieces = int(cells[0])
                except (ValueError, TypeError):
                    pass
                try:
                    weight_kg = float(cells[1])
                except (ValueError, TypeError):
                    pass
                origin      = _iata(cells[2])
                destination = _iata(cells[3])

    # Table 1: Segment info — row[2+] = [from, to, fn, date, type, ATD, ATA, pcs, wgt, status]
    flights: list[FlightLeg] = []
    if len(tables) >= 2:
        rows = tables[1].find_all("tr")
        for row in rows[2:]:
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cells) < 7:
                continue
            fn = cells[2].strip()
            if not fn:
                continue

            dep_time, dep_date = _parse_dt(cells[5])
            arr_time, arr_date = _parse_dt(cells[6])
            dep_status = "actual" if dep_time else "scheduled"
            arr_status = "actual" if arr_time else "scheduled"

            # Fall back scheduled date to col 3 if no actual departure
            if not dep_date and cells[3].strip():
                raw = cells[3].strip()
                if len(raw) == 8:
                    dep_date = f"{raw[:4]}/{raw[4:6]}/{raw[6:8]}"

            flights.append(FlightLeg(
                flight_no        = fn,
                from_airport     = _iata(cells[0]),
                to_airport       = _iata(cells[1]),
                departure_date   = dep_date,
                departure_time   = dep_time,
                departure_status = dep_status,
                arrival_date     = arr_date,
                arrival_time     = arr_time,
                arrival_status   = arr_status,
                flight_time      = "",
                flrs_id          = 0,   # China Eastern provides no ULD data
            ))

    # Status: Table 2 (simple/latest), first data row, col 0
    status = ""
    if len(tables) >= 3:
        rows = tables[2].find_all("tr")
        if len(rows) >= 3:
            cells = [c.get_text(strip=True) for c in rows[2].find_all("td")]
            if cells:
                status = _STATUS_MAP.get(cells[0], cells[0])

    return TrackingResult(
        awb=awb,
        from_airport=origin,
        from_name="",
        to_airport=destination,
        to_name="",
        status=status,
        status_code=status,
        flights=flights,
        total_pieces=pieces,
        total_weight_kg=weight_kg,
    )


# ── tracker ───────────────────────────────────────────────────────────────

class ChinaEasternTracker(AirlineTracker):
    prefixes = ["112"]
    name = "China Eastern Cargo"

    async def track(self, prefix: str, number: str) -> TrackingResult:
        awb = f"{prefix}-{number}"
        html = await _fetch_tracking(prefix, number)
        return _parse_html(awb, html)

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
