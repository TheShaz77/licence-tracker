from .boc import BoCSource
from .eba import EBASource
from .ecb import ECBSource
from .fca import FCASource
from .finma import FINMASource
from .fincen import FinCENSource
from .fintrac import FINTRACSource
from .mica import MiCASource
from .opensanctions_msb import OpenSanctionsMSBSource
from .southafrica import ZA_SOURCES
from .swiss_sro import CHSROSource
from .za_wiki import ZABanksWikiSource
from .pra import PRASource

ALL_SOURCES = {s.name: s for s in (FCASource(), PRASource(), EBASource(), ECBSource(), FINMASource(),
                                     FINTRACSource(), BoCSource(), FinCENSource(), OpenSanctionsMSBSource(), MiCASource(), CHSROSource(), ZABanksWikiSource(),
                                     *ZA_SOURCES)}
