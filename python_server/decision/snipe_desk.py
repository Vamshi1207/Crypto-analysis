"""Compatibility shim — sniper desk lives in ``decision.engines.sniper.desk``."""

import sys

from decision.engines.sniper import desk as _impl

sys.modules[__name__] = _impl
