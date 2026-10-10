"""Compatibility shim — arb engine lives in ``decision.engines.arb.engine``."""

import sys

from decision.engines.arb import engine as _impl

sys.modules[__name__] = _impl
