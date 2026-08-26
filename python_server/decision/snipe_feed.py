"""Compatibility shim — sniper feed lives in ``decision.engines.sniper.feed``."""

import sys

from decision.engines.sniper import feed as _impl

sys.modules[__name__] = _impl
