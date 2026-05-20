from .base import AirlineTracker, TrackingResult, FlightLeg
from .br_cargo import BRCargoTracker
from .cargolux import CargoluxTracker
from .cathay_cargo import CathayCargoTracker
from .china_airlines import ChinaAirlinesTracker
from .korean_air import KoreanAirCargoTracker
from .atlas_air import AtlasAirTracker
from .china_eastern import ChinaEasternTracker
from .kalitta_air import KalittaAirTracker
from .aa_cargo import AACargoTracker
from .ana_cargo import ANACargoTracker
from .china_southern import ChinaSouthernTracker
from .turkish_cargo import TurkishCargoTracker
from .iag_cargo import IAGCargoTracker
from .nca_cargo import NCACargoTracker
from .asiana_cargo import AsianaCargoTracker
from .air_zeta_cargo import AirZetaCargoTracker
from .hna_cargo import HNACargoTracker
from .sf_airlines import SFAirlinesTracker

REGISTRY: dict[str, AirlineTracker] = {}

def _register(*trackers: AirlineTracker):
    for t in trackers:
        for p in t.prefixes:
            REGISTRY[p] = t

_register(
    BRCargoTracker(),
    CargoluxTracker(),
    CathayCargoTracker(),
    ChinaAirlinesTracker(),
    KoreanAirCargoTracker(),
    AtlasAirTracker(),
    ChinaEasternTracker(),
    KalittaAirTracker(),
    AACargoTracker(),
    ANACargoTracker(),
    ChinaSouthernTracker(),
    TurkishCargoTracker(),
    IAGCargoTracker(),
    NCACargoTracker(),
    AsianaCargoTracker(),
    AirZetaCargoTracker(),
    HNACargoTracker(),
    SFAirlinesTracker(),
)
