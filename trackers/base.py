import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

# Playwright launch args required for Docker/container environments
PW_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

# Max concurrent Playwright browsers to avoid OOM in container (1 GB RAM)
PW_SEMAPHORE = asyncio.Semaphore(2)


@dataclass
class ULDItem:
    uld: str
    pieces: int


@dataclass
class ULDResult:
    flight_no: str
    departure_date: str
    departure: str
    arrival: str
    ulds: list[ULDItem]


@dataclass
class FlightLeg:
    flight_no: str
    from_airport: str
    to_airport: str
    departure_date: str
    departure_time: str
    departure_status: str       # "actual" | "estimated" | "scheduled"
    arrival_date: str
    arrival_time: str
    arrival_status: str
    flight_time: str
    pieces: Optional[int] = None
    weight_kg: Optional[float] = None
    flrs_id: int = 0            # non-zero after departure; enables ULD lookup


@dataclass
class TrackingResult:
    awb: str
    from_airport: str
    from_name: str
    to_airport: str
    to_name: str
    status: str
    status_code: str
    flights: list[FlightLeg] = field(default_factory=list)
    total_pieces: Optional[int] = None
    total_weight_kg: Optional[float] = None


class AirlineTracker(ABC):
    prefixes: list[str]
    name: str

    @abstractmethod
    async def track(self, prefix: str, number: str) -> TrackingResult:
        ...

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
        raise NotImplementedError(f"{self.name} does not support ULD lookup")

    # ── shared helpers ────────────────────────────────────────────────

    @staticmethod
    def classify(time_str: str) -> str:
        t = time_str.lower()
        if "actual" in t:    return "actual"
        if "estimated" in t: return "estimated"
        return "scheduled"

    @staticmethod
    def clean(time_str: str) -> str:
        for tag in ("(Estimated)", "(Actual)", "(Scheduled)", "(estimated)", "(actual)"):
            time_str = time_str.replace(tag, "")
        return time_str.strip()
