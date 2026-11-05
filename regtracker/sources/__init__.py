from .eba import EBASource
from .ecb import ECBSource
from .fca import FCASource
from .finma import FINMASource
from .pra import PRASource

ALL_SOURCES = {s.name: s for s in (FCASource(), PRASource(), EBASource(), ECBSource(), FINMASource())}
