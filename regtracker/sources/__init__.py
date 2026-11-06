from .boc import BoCSource
from .eba import EBASource
from .ecb import ECBSource
from .fca import FCASource
from .finma import FINMASource
from .fintrac import FINTRACSource
from .pra import PRASource

ALL_SOURCES = {s.name: s for s in (FCASource(), PRASource(), EBASource(), ECBSource(), FINMASource(),
                                     FINTRACSource(), BoCSource())}
