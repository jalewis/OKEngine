#!/usr/bin/env python3
"""Generic re-shelving drain — re-file flat pages into the OKF hierarchy for EVERY
partitioned namespace across ALL domains, driven by each domain-pack's schema.yaml
`partitioning` config.

Replaces the per-namespace reshelve_{entities,sources,concepts} drains: instead of
hardcoding three namespaces, it asks okf_migrate for every non-flat namespace — the vault
root's EFFECTIVE schema (the composed artifact where there is one) plus every sub-domain
schema (e.g. wiki/<subdomain>/schema.yaml → <subdomain>/concepts, <subdomain>/entities,
<subdomain>/sources) — and re-shelves each via okf_migrate's link-preserving bulk pass.
A new domain just drops a schema.yaml.

Pure script / no_agent. Idempotent. Env: WIKI_PATH (default /opt/vault).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okf_migrate  # noqa: E402


def main() -> int:
    root = os.environ.get("WIKI_PATH", "/opt/vault")
    # okf_migrate owns which schema governs a namespace; asking it keeps the drain's idea of
    # what EXISTS identical to the mover's. This used to read root/schema.yaml itself, and on
    # a composed pack that file omits namespaces the composition declares — so the drain never
    # passed `sources` to the mover and still exited 0, reporting the namespaces it did do
    # (okengine#519).
    nss = okf_migrate.partitioned_namespaces(Path(root))
    print(f"reshelve: partitioned namespaces = {nss}")
    for ns in nss:
        okf_migrate.main(["--namespace", ns, "--apply", "--root", root])
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
