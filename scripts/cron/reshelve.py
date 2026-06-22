#!/usr/bin/env python3
"""Generic re-shelving drain — re-file flat pages into the OKF hierarchy for EVERY
partitioned namespace across ALL domains, driven by each domain-pack's schema.yaml
`partitioning` config.

Replaces the per-namespace reshelve_{entities,sources,concepts} drains: instead of
hardcoding three namespaces, it reads the root schema (entities/sources/concepts/…)
and every sub-domain schema (e.g. wiki/<subdomain>/schema.yaml → <subdomain>/concepts,
<subdomain>/entities, <subdomain>/sources) and re-shelves each non-flat namespace via
okf_migrate's link-preserving bulk pass. A new domain just drops a schema.yaml.

Pure script / no_agent. Idempotent. Env: WIKI_PATH (default /opt/vault).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okf_migrate  # noqa: E402


def _nonflat_namespaces(root: Path) -> list[str]:
    out: list[str] = []

    def add(schema_path: Path, prefix: str) -> None:
        try:
            sch = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        for leaf, cfg in ((sch.get("partitioning") or {}).get("namespaces") or {}).items():
            if (cfg or {}).get("strategy", "flat") != "flat":
                out.append(f"{prefix}{leaf}")

    if (root / "schema.yaml").is_file():
        add(root / "schema.yaml", "")
    for sd in sorted((root / "wiki").iterdir()):
        if sd.is_dir() and (sd / "schema.yaml").is_file():
            add(sd / "schema.yaml", f"{sd.name}/")
    return out


def main() -> int:
    root = os.environ.get("WIKI_PATH", "/opt/vault")
    nss = _nonflat_namespaces(Path(root))
    print(f"reshelve: partitioned namespaces = {nss}")
    for ns in nss:
        okf_migrate.main(["--namespace", ns, "--apply", "--root", root])
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
