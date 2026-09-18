#!/usr/bin/env python3
"""Compatibility alias for :mod:`okengine.projection.projector`."""
import sys

import engine_package  # noqa: E402  — makes `okengine` importable when this file is
engine_package.ensure()  # loaded BY PATH by an external consumer (okpacks-library#89)
from okengine.projection import projector as _implementation

sys.modules[__name__] = _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
