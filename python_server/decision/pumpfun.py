"""Compatibility shim — Pump helpers live in ``decision.engines.sniper.pumpfun``."""

import sys

from decision.engines.sniper import pumpfun as _impl

sys.modules[__name__] = _impl
