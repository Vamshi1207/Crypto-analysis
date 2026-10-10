"""Compatibility shim — AMM helper lives in ``decision.engines.arb.amm``."""

import sys

from decision.engines.arb import amm as _impl

sys.modules[__name__] = _impl
