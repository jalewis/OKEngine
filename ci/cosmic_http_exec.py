#!/usr/bin/env python3
"""Compatibility entry point for Cosmic Ray's concurrent HTTP distributor.

Cosmic Ray 8 calls ``asyncio.get_event_loop()`` without creating a loop.  Python 3.14 correctly
raises there, while older CI interpreters create one implicitly.  Install the loop explicitly so
local and CI mutation evidence use the same concurrent execution path.
"""
import asyncio
import sys

from cosmic_ray.cli import main


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    sys.exit(main(["exec", *sys.argv[1:]]))
