"""Compatibility shim — finder lives in ``decision.engines.finder.discover``."""

import sys

from decision.engines.finder import discover as _impl

sys.modules[__name__] = _impl
