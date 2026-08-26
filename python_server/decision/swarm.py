"""Compatibility shim — scalp swarm lives in ``decision.engines.scalp.swarm``."""

import sys

from decision.engines.scalp import swarm as _impl

sys.modules[__name__] = _impl
