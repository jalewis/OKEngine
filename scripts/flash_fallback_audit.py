#!/usr/bin/env python3
"""Audit post-cutoff DeepSeek Flash calls and fallback activations across pack logs.

This is intentionally independent of the daily usage ledger: a P0 cost-control
check needs an exact deployment cutoff and a non-zero exit when any new Flash
traffic appears.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+")
_LOG = re.compile(r"^(.+)-\d{8}-\d{6}\.log$")
_FLASH_CALL = re.compile(
    r"\bAPI call #\d+:\s+model=(?:deepseek-flash|deepseek-v4-flash)\s+provider=(deepseek|custom)\b",
    re.I,
)
_FLASH_FAILED = re.compile(
    r"\bAPI call failed .*provider=deepseek\b.*model=(?:deepseek-flash|deepseek-v4-flash)\b",
    re.I,
)
_FLASH_FALLBACK = re.compile(
    r"Fallback activated:.*(?:→|->)\s*(?:deepseek-flash|deepseek-v4-flash)\b", re.I
)


def parse_since(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


def audit(
    packs: list[Path],
    since: datetime,
    allowed_paid: set[tuple[str, str]] | None = None,
) -> dict:
    allowed_paid = allowed_paid or set()
    calls: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    paid_calls: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    expected_paid_calls: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unexpected_paid_calls: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    local_calls: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    paid_failures: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    activations: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    logs_scanned = 0
    for pack in packs:
        logdir = pack / ".hermes-data" / "logs" / "cron-plus"
        if not logdir.is_dir():
            continue
        for path in logdir.glob("*.log"):  # glob-ok: cron-plus run logs are a deliberately flat directory
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            logs_scanned += 1
            match = _LOG.match(path.name)
            lane = match.group(1) if match else path.stem
            for line in lines:
                stamp = _STAMP.match(line)
                if not stamp or datetime.fromisoformat(stamp.group(1)) < since:
                    continue
                call = _FLASH_CALL.search(line)
                if call:
                    calls[pack.name][lane] += 1
                    target = paid_calls if call.group(1).lower() == "deepseek" else local_calls
                    target[pack.name][lane] += 1
                    if call.group(1).lower() == "deepseek":
                        classification = (
                            expected_paid_calls
                            if (pack.name, lane) in allowed_paid
                            else unexpected_paid_calls
                        )
                        classification[pack.name][lane] += 1
                if _FLASH_FAILED.search(line):
                    paid_failures[pack.name][lane] += 1
                if _FLASH_FALLBACK.search(line):
                    activations[pack.name][lane] += 1
    return {
        "since": since.isoformat(timespec="seconds"),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "logs_scanned": logs_scanned,
        "flash_calls": sum(sum(v.values()) for v in calls.values()),
        "paid_flash_calls": sum(sum(v.values()) for v in paid_calls.values()),
        "expected_paid_flash_calls": sum(
            sum(v.values()) for v in expected_paid_calls.values()
        ),
        "unexpected_paid_flash_calls": sum(
            sum(v.values()) for v in unexpected_paid_calls.values()
        ),
        "local_flash_calls": sum(sum(v.values()) for v in local_calls.values()),
        "paid_flash_failures": sum(sum(v.values()) for v in paid_failures.values()),
        "flash_fallback_activations": sum(sum(v.values()) for v in activations.values()),
        "calls_by_pack_lane": {p: dict(sorted(v.items())) for p, v in sorted(calls.items())},
        "paid_calls_by_pack_lane": {
            p: dict(sorted(v.items())) for p, v in sorted(paid_calls.items())
        },
        "expected_paid_calls_by_pack_lane": {
            p: dict(sorted(v.items())) for p, v in sorted(expected_paid_calls.items())
        },
        "unexpected_paid_calls_by_pack_lane": {
            p: dict(sorted(v.items())) for p, v in sorted(unexpected_paid_calls.items())
        },
        "local_calls_by_pack_lane": {
            p: dict(sorted(v.items())) for p, v in sorted(local_calls.items())
        },
        "paid_failures_by_pack_lane": {
            p: dict(sorted(v.items())) for p, v in sorted(paid_failures.items())
        },
        "fallbacks_by_pack_lane": {
            p: dict(sorted(v.items())) for p, v in sorted(activations.items())
        },
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True,
                        help="local log timestamp cutoff, ISO format")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-paid",
        action="append",
        default=[],
        metavar="PACK/LANE",
        help=(
            "classify an explicitly routed paid Flash lane as expected; repeatable. "
            "All paid traffic remains visible in paid_flash_calls."
        ),
    )
    parser.add_argument("packs", nargs="+", type=Path)
    args = parser.parse_args(argv)
    allowed_paid = set()
    for value in args.allow_paid:
        pack, separator, lane = value.partition("/")
        if not separator or not pack or not lane:
            parser.error(f"--allow-paid must be PACK/LANE, got {value!r}")
        allowed_paid.add((pack, lane))
    result = audit(args.packs, parse_since(args.since), allowed_paid)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        atomic_write(args.output, rendered)
    print(rendered, end="")
    return 1 if (
        result["unexpected_paid_flash_calls"]
        or result["paid_flash_failures"]
        or result["flash_fallback_activations"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
