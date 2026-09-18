"""Compatibility alias for :mod:`okengine.mcp.projection`."""
import sys

from okengine.mcp import projection as _implementation

sys.modules[__name__] = _implementation
