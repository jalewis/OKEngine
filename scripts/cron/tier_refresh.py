#!/usr/bin/env python3
"""tier-refresh — derived hot/warm/cold distribution dashboard (OKF G4, no_agent).

Tier is DERIVED, not stored (see tier_lib): a page self-promotes/demotes as it
ages, so there is nothing to write onto pages. This cron just reports the live
distribution — counts per namespace per tier — so the operator can see the
working-set shape and watch promotion/demotion movement over time. The actual
"tier filter" lives in kb_search / okengine-mcp (the `--tier` retrieval filter),
which call tier_lib at query time.

Efficiency: by-date namespaces (sources) are counted from the path (no file
reads — 46k files); the rest read frontmatter (~6k each).

Writes wiki/operational/tier-distribution.md (excluded from the schema gate) and
a gitignored sidecar wiki/operational/.tier-counts.json for run-over-run deltas.
Pure script / no_agent.

Env: WIKI_PATH (default /opt/vault)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier_lib  # noqa: E402
import tz_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
OPDIR = WIKI / "operational"
DASH = OPDIR / "tier-distribution.md"
SIDECAR = OPDIR / ".tier-counts.json"
_TIERS = ("hot", "warm", "cold")


def _namespace_bases(ns: str) -> list:
    """Every dir holding `ns` pages: the root wiki/<ns>, PLUS wiki/<subdomain>/<ns> for each
    walk-up sub-domain (a dir under wiki/ carrying its own schema.yaml). Without the sub-domain
    bases a co-installed (multipack) vault under-counts every namespace, so the operator sees a
    vault smaller than it is (okengine#178)."""
    bases = [WIKI / ns]
    if WIKI.is_dir():
        for sub in sorted(WIKI.iterdir()):
            if sub.is_dir() and (sub / "schema.yaml").is_file():
                bases.append(sub / ns)
    return bases


def _count_namespace(ns: str, nscfg: dict, cfg: dict, today) -> dict:
    counts = {t: 0 for t in _TIERS}
    untiered = 0
    from_path = nscfg.get("from_path") and not nscfg.get("status_field")
    for base in _namespace_bases(ns):
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            n = p.name
            if n == "INDEX.md" or n.startswith("INDEX-p") or n.startswith("_"):
                continue
            # rel must start with the NAMESPACE for both root and sub-domain bases — tier_of infers
            # the namespace from rel.split('/')[0]. From WIKI, a walk-up page reads as
            # '<subdomain>/<ns>/…', so tier_of got the sub-domain name instead of the namespace and
            # the namespace's date/status tiering config never applied (multipack under-count).
            rel = f"{ns}/{p.relative_to(base).as_posix()}"
            fm = {} if from_path else tier_lib.fm_of(p)
            t = tier_lib.tier_of(rel, fm, cfg, today)
            if t in counts:
                counts[t] += 1
            else:
                untiered += 1
    if untiered:
        counts["_untiered"] = untiered
    return counts


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    cfg = tier_lib.load_cfg(VAULT)
    today = tz_lib.deployment_today()                                  # okengine#301: deployment TZ
    now = tz_lib.deployment_now().strftime("%Y-%m-%d %H:%M %Z")        # %Z = the deployment zone, not "UTC"

    nss = cfg.get("namespaces") or {}
    dist = {ns: _count_namespace(ns, nscfg, cfg, today) for ns, nscfg in nss.items()}

    prior = {}
    if SIDECAR.is_file():
        try:
            prior = json.loads(SIDECAR.read_text(encoding="utf-8")).get("dist", {})
        except Exception:
            prior = {}

    def delta(ns: str, t: str) -> str:
        d = dist[ns].get(t, 0) - (prior.get(ns, {}).get(t, 0) if prior else 0)
        if not prior or d == 0:
            return ""
        return f" ({'+' if d > 0 else ''}{d})"

    hd = int(cfg.get("hot_days", 30))
    wd = int(cfg.get("warm_days", 365))
    L = ["---", "type: dashboard", 'title: "Tier distribution — hot/warm/cold"', "---", "",
         f"# Tier distribution — {now}", "",
         f"_Derived (not stored): **hot** ≤ {hd}d · **warm** ≤ {wd}d · **cold** older. "
         "A page self-promotes/demotes as it ages — this is the live shape, with "
         "movement since the last run in parens. Filter at query time with "
         "`kb_search --tier` / okengine-mcp `search(tier=...)`._", "",
         "| Namespace | Hot | Warm | Cold | Total |",
         "|---|---|---|---|---|"]
    tot = {t: 0 for t in _TIERS}
    for ns in nss:
        c = dist[ns]
        row_tot = sum(c.get(t, 0) for t in _TIERS)
        for t in _TIERS:
            tot[t] += c.get(t, 0)
        L.append(f"| {ns} | {c.get('hot',0)}{delta(ns,'hot')} | {c.get('warm',0)}{delta(ns,'warm')} "
                 f"| {c.get('cold',0)}{delta(ns,'cold')} | {row_tot} |")
    grand = sum(tot.values())
    L.append(f"| **all** | **{tot['hot']}** | **{tot['warm']}** | **{tot['cold']}** | **{grand}** |")
    L.append("")
    OPDIR.mkdir(parents=True, exist_ok=True)
    DASH.write_text("\n".join(L), encoding="utf-8")
    SIDECAR.write_text(json.dumps({"as_of": now, "dist": dist}, indent=2), encoding="utf-8")

    summary = " · ".join(f"{ns}: {dist[ns].get('hot',0)}h/{dist[ns].get('warm',0)}w/{dist[ns].get('cold',0)}c"
                         for ns in nss)
    print(f"tier-refresh: {summary} -> wiki/operational/tier-distribution.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
