#!/usr/bin/env python3
"""Entry point for Cosmic Ray's concurrent HTTP distributor.

Two adaptations, both at the one boundary every parallel mutation campaign crosses:

1. Cosmic Ray 8 calls ``asyncio.get_event_loop()`` without creating a loop. Python 3.14 correctly
   raises there, while older CI interpreters create one implicitly. Install the loop explicitly so
   local and CI mutation evidence use the same concurrent execution path.

2. Cosmic Ray's ``send_request`` calls ``aiohttp.request`` without a timeout, so aiohttp's default
   applies: 300 seconds total. The per-mutant bound the gate configures (the ``timeout`` in the
   session's TOML, enforced worker-side by ci/mutation_timeout.py and Cosmic Ray) is routinely
   longer. A mutant whose test run outlasted 300 s was still running on the worker when the client
   gave up; Cosmic Ray recorded the ``asyncio.TimeoutError`` (whose text is empty) as
   ``worker='abnormal'`` with no output, and the gate rightly refused it as an infrastructure
   error. Nightly 13260 lost three surviving convergence.py mutants that way: selection runs of
   359 s, 301 s and 331 s, the only three of 610 over 300 s (okengine#1047). The client now waits
   for the configured mutant timeout plus a margin, so the WORKER's watchdog, not an unrelated HTTP
   default, decides how long a mutant may run.
"""
from __future__ import annotations

import asyncio
import functools
import sys
import tomllib
from pathlib import Path

import aiohttp

# The worker answers only after its own watchdog fires (ci/mutation_timeout.py, then Cosmic Ray's
# timeout, which the gate sets 10 s wider). This margin covers that gap, mutation and restore, and
# the response; it is not a second budget.
RESPONSE_MARGIN_SECONDS = 120.0
CONNECT_SECONDS = 30.0


def client_timeout(config_path: Path) -> aiohttp.ClientTimeout:
    """The HTTP client timeout for a session: the configured per-mutant timeout plus a margin."""
    with Path(config_path).open("rb") as handle:
        config = tomllib.load(handle)
    seconds = (config.get("cosmic-ray") or {}).get("timeout")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ValueError(f"{config_path}: [cosmic-ray] timeout must be a positive number, "
                         f"got {seconds!r}; without it the client would fall back to aiohttp's "
                         f"300 s default and report slow mutants as abnormal")
    return aiohttp.ClientTimeout(total=float(seconds) + RESPONSE_MARGIN_SECONDS,
                                 sock_connect=CONNECT_SECONDS)


class _TimedAiohttp:
    """``aiohttp`` as Cosmic Ray's HTTP distributor sees it, with ``request`` given a timeout."""

    def __init__(self, timeout: aiohttp.ClientTimeout):
        self.request = functools.partial(aiohttp.request, timeout=timeout)

    def __getattr__(self, name):
        return getattr(aiohttp, name)


def install_client_timeout(timeout: aiohttp.ClientTimeout) -> None:
    """Make Cosmic Ray's distributor send every mutate-and-test request with ``timeout``."""
    from cosmic_ray.distribution import http

    http.aiohttp = _TimedAiohttp(timeout)


def main(argv: list[str]) -> int:
    """``cosmic-ray exec <config> <session>`` with the event loop and client timeout installed."""
    from cosmic_ray.cli import main as cosmic_main

    if len(argv) < 2:
        raise SystemExit("usage: cosmic_http_exec.py <config.toml> <session.sqlite>")
    install_client_timeout(client_timeout(Path(argv[0])))
    asyncio.set_event_loop(asyncio.new_event_loop())
    return cosmic_main(["exec", *argv])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
