#!/usr/bin/env python3
"""Compatibility alias for the packaged corpus transaction protocol."""

import sys

from okengine import corpus_transaction as _implementation

sys.modules[__name__] = _implementation
