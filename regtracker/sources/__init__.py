from .boc import BoCSource
from .eba import EBASource
from .ecb import ECBSource
from .fca import FCASource
from .finma import FINMASource
from .fincen import FinCENSource
from .fintrac import FINTRACSource
from .mica import MiCASource
from .southafrica import ZA_SOURCES
from .swiss_sro import CHSROSource
from .pra import PRASource

ALL_SOURCES = {s.name: s for s in (FCASource(), PRASource(), EBASource(), ECBSource(), FINMASource(),
                                     FINTRACSource(), BoCSource(), FinCENSource(), MiCASource(), CHSROSource(),
                                     *ZA_SOURCES)}
