"""Compatibility alias for :mod:`okengine.mcp.scope`."""
import sys

from okengine.mcp import scope as _implementation

globals().update(
    (name, value) for name, value in vars(_implementation).items()
    if not (name.startswith("__") and name.endswith("__"))
)
sys.modules[__name__] = _implementation
