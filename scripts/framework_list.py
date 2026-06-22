#!/usr/bin/env python3
"""framework list — browse the pack catalog.

Reads catalog.json (the okpacks-library index by default) and prints the packs you
can `framework pull`. Override the source with --catalog URL|PATH or OKENGINE_CATALOG.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent


def _pull_mod():
    spec = importlib.util.spec_from_file_location(
        "framework_pull", ENGINE_ROOT / "scripts" / "framework_pull.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(argv: list[str]) -> int:
    fp = _pull_mod()
    ap = argparse.ArgumentParser(prog="framework list", description=__doc__)
    ap.add_argument("--catalog", default=fp.DEFAULT_CATALOG)
    ap.add_argument("--json", action="store_true", help="machine-readable catalog JSON")
    args = ap.parse_args(argv)

    catalog, err = fp.read_catalog(args.catalog)
    if not catalog:
        print(f"ERROR: could not read the catalog.\n  {err}\n{fp.CATALOG_HELP}", file=sys.stderr)
        return 1
    packs = catalog.get("packs") or []

    if args.json:
        import json
        print(json.dumps(catalog, indent=2, ensure_ascii=False))
        return 0

    # Fixed columns with DOMAIN last, so a long domain can run on without pushing
    # the version/trust/status columns out of alignment (#9). Domain truncated.
    print(f"  {'NAME':<22} {'ENGINE':<8} {'TRUST':<8} {'STATUS':<11} DOMAIN")
    print(f"  {'-' * 22} {'-' * 8} {'-' * 8} {'-' * 11} {'-' * 6}")
    for p in packs:
        dom = str(p.get("domain", ""))
        if len(dom) > 64:
            dom = dom[:63] + "…"
        print(f"  {str(p.get('name','')):<22} {str(p.get('engine_version','')):<8} "
              f"{str(p.get('trust','')):<8} {str(p.get('status','')):<11} {dom}")
    print(f"\n  {len(packs)} pack(s) · catalog: {catalog.get('catalog','?')}")
    print("  pull one:  framework pull <name> [dest]   ·   --json for tooling")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
