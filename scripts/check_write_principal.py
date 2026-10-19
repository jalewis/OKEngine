#!/usr/bin/env python3
"""Refuse a deploy whose write servers would refuse to start (okengine#1027).

The enforced write server exits at startup without one pack principal: <vault>/pack.yaml
must name the pack (or, where no pack.yaml declares one, the server's env: map must carry
OKENGINE_PACK). The `write-principal` deployment check judges exactly that, but it ran only
after the gateway was recreated: the refusal it describes had already become an outage.
deploy.sh runs this between ensure-runtime (which renders the writers) and the recreate, with
the deployment check's own rule, so a refusal stops the deploy while the old gateway still
serves.

Usage: check_write_principal.py PACK_DIR      exit 0 = every writer starts, 1 = refused.
"""
from __future__ import annotations

import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
# This checkout's own code wins over any installed or editable `okengine` (deploy.sh runs this with
# the host python, whose venv may carry another worktree's engine), exactly as framework.py does.
for _root in (ENGINE, ENGINE / "src", ENGINE / "scripts" / "cron"):
    while str(_root) in sys.path:
        sys.path.remove(str(_root))
    sys.path.insert(0, str(_root))
import deployment_checks as checks  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_write_principal.py PACK_DIR", file=sys.stderr)
        return 2
    pack = Path(argv[0])
    checks.F.clear()
    checks.configure(pack, data=pack / ".hermes-data", hermes=pack / ".no-baked-image", env={})
    checks.check_write_principal()
    refused = [msg for level, area, msg in checks.F if level != "INFO"]
    for msg in refused:
        print(f"ERROR: {msg}", file=sys.stderr)
    if refused:
        print("       Nothing was recreated: the running gateway keeps serving.", file=sys.stderr)
        return 1
    for _level, _area, msg in checks.F:
        print(f"  ok: {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
