import sys
import traceback
import logging
from dataclasses import asdict
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from trackers import REGISTRY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Air Cargo Tracker")


@app.get("/api/track/{awb}")
async def track(awb: str):
    awb = awb.strip().replace(" ", "")

    if "-" in awb:
        prefix, number = awb.split("-", 1)
    elif len(awb) >= 4:
        prefix, number = awb[:3], awb[3:]
    else:
        raise HTTPException(400, "Invalid AWB format. Use: 695-59554773")

    tracker = REGISTRY.get(prefix)
    if not tracker:
        raise HTTPException(
            404,
            f"Airline prefix '{prefix}' not supported yet. "
            f"Supported: {sorted(REGISTRY.keys())}",
        )

    try:
        result = await tracker.track(prefix, number)
    except HTTPException:
        raise
    except Exception as exc:
        msg = str(exc) or repr(exc)
        logger.error("Track error for %s: %s\n%s", awb, msg, traceback.format_exc())
        raise HTTPException(500, msg or f"{type(exc).__name__} (no message)") from exc
    return asdict(result)


@app.get("/api/uld")
async def uld_detail(
    prefix: str,
    awb: str,
    flight_no: str,
    dep: str,
    arr: str,
    dep_date: str,
    flrs_id: int,
):
    tracker = REGISTRY.get(prefix)
    if not tracker:
        raise HTTPException(404, f"Prefix '{prefix}' not supported")
    try:
        result = await tracker.fetch_uld(prefix, awb, flight_no, dep, arr, dep_date, flrs_id)
    except NotImplementedError:
        from trackers.base import ULDResult
        result = ULDResult(
            flight_no=flight_no,
            departure_date=dep_date,
            departure=dep,
            arrival=arr,
            ulds=[],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ULD error for %s %s: %s\n%s", prefix, awb, str(exc), traceback.format_exc())
        raise HTTPException(500, str(exc) or repr(exc)) from exc
    return asdict(result)


@app.get("/api/debug/17track/{awb}")
async def debug_17track(awb: str):
    """Return raw 17track REST API response for debugging."""
    import os, httpx
    api_key = os.environ.get("SEVENTEEN_TRACK_API_KEY", "")
    if not api_key:
        return {"error": "SEVENTEEN_TRACK_API_KEY not set"}

    api_base = "https://api.17track.net/track/v2.2"
    headers = {"17token": api_key, "Content-Type": "application/json"}
    payload = [{"number": awb}]

    results = {}
    async with httpx.AsyncClient(timeout=30) as client:
        reg = await client.post(f"{api_base}/register", json=payload, headers=headers)
        results["register_status"] = reg.status_code
        try:
            results["register_body"] = reg.json()
        except Exception:
            results["register_body"] = reg.text

        resp = await client.post(f"{api_base}/getRealTimeTrackInfo", json=payload, headers=headers)
        results["realtime_status"] = resp.status_code
        try:
            results["realtime_body"] = resp.json()
        except Exception:
            results["realtime_body"] = resp.text

    return results


@app.get("/api/airlines")
async def airlines():
    seen = set()
    out = []
    for prefix, tracker in sorted(REGISTRY.items()):
        if tracker.name not in seen:
            seen.add(tracker.name)
            out.append({"prefixes": tracker.prefixes, "name": tracker.name})
    return out


# Serve built frontend in production
try:
    app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="static")
except Exception:
    pass
