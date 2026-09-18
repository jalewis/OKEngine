"""Validator result model and command-line rendering."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []  # (severity, check, detail)

    def add(self, sev: str, check: str, detail: str = ""):
        self.rows.append((sev, check, detail))

    def ok(self, c, d=""):
        self.add("OK", c, d)

    def info(self, c, d=""):
        self.add("INFO", c, d)

    def warn(self, c, d=""):
        self.add("WARN", c, d)

    def fail(self, c, d=""):
        self.add("FAIL", c, d)

    @property
    def n_fail(self):
        return sum(1 for s, _, _ in self.rows if s == "FAIL")

    @property
    def n_warn(self):
        return sum(1 for s, _, _ in self.rows if s == "WARN")




GLYPH = {"OK": "✓", "INFO": "·", "WARN": "⚠", "FAIL": "✗"}


def main(argv: list[str], validate, *, stable_corpus) -> int:
    ap = argparse.ArgumentParser(description="Validate an OKF domain pack before deploy.")
    ap.add_argument("pack", help="path to the domain pack directory")
    ap.add_argument("--probe-feeds", action="store_true", help="HTTP-probe feed URLs (network)")
    ap.add_argument("--quiet", action="store_true", help="only print WARN/FAIL + summary")
    args = ap.parse_args(argv)
    pack = Path(args.pack).expanduser()
    if not pack.is_dir():
        print(f"ERROR: pack dir not found: {pack}", file=sys.stderr)
        return 2
    with stable_corpus(pack) as corpus_epoch:
        r = validate(pack, probe=args.probe_feeds)
    print(f"framework validate — {pack} (corpus epoch {corpus_epoch})\n")
    for sev, check, detail in r.rows:
        if args.quiet and sev in ("OK", "INFO"):
            continue
        line = f"  {GLYPH[sev]} [{sev}] {check}"
        print(f"{line}: {detail}" if detail else line)
    verdict = "FAIL" if r.n_fail else ("PASS-with-warnings" if r.n_warn else "PASS")
    print(f"\n{verdict} — {r.n_fail} fail, {r.n_warn} warn, {len(r.rows)} checks")
    return 1 if r.n_fail else 0
