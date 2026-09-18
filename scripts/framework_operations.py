#!/usr/bin/env python3
"""Compatibility alias for :mod:`okengine.operations.framework`."""
import sys

from okengine.operations import framework as _implementation

globals().update(
    (name, value) for name, value in vars(_implementation).items()
    if not (name.startswith("__") and name.endswith("__"))
)
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
