#!/usr/bin/env python3
"""Compatibility alias for :mod:`okengine.operations.run`."""
import sys

from okengine.operations import run as _implementation

sys.modules[__name__] = _implementation
