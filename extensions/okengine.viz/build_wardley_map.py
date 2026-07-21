#!/usr/bin/env python3
"""build_wardley_map.py — okengine.viz: a Wardley/strategic map over the vault's concept graph
(okengine#156). Deterministic, no_agent. Domain-agnostic: it lays concepts on an
evolution × value-chain plane; the axes + node set come from the vault, no domain knowledge.

Axes (the maturity-axis design):
  - x = EVOLUTION/maturity. A concept's `evolution` field (config: VIZ_EVOLUTION_FIELD), mapped via
    the Wardley scale (genesis < custom < product < commodity), when a pack enriches concepts.
    FALLBACK when absent: ubiquity — how widely the concept is referenced (in-degree percentile);
    a heavily-referenced concept reads as more settled/commodity.
  - y = VALUE-CHAIN/foundational. A concept's `value_chain` field (config: VIZ_VALUE_FIELD) when
    present. FALLBACK: entity-coupling — how embedded it is in the entity graph (distinct entity
    referrers, percentile); deeply-coupled concepts read as more load-bearing.

Heuristic fallbacks are clearly labelled on the dashboard — enrich concepts with the two fields for
a true map. Writes wiki/dashboards/wardley.md (type: dashboard) carrying a self-declared
`panel: {kind: two-axis}` + the node coordinates (the reader's two-axis kind renders it, okengine#160
P2) AND a readable quadrant breakdown so it's useful before that lands.

Scope (okengine: readability): a whole-vault map surfaces global hub concepts, not the field the
operator cares about. `VIZ_ANCHOR` (comma-separated wiki-relative page paths, e.g. a watchlist
page) scopes the map to the anchors' neighborhood: concepts the anchor pages link directly, plus
concepts linked from any entity the anchors link (1 hop). Percentiles stay computed over the FULL
concept population so scoped nodes keep their true global positions. Unset -> whole-vault map.

Env: WIKI_PATH (default /opt/vault) · VIZ_EVOLUTION_FIELD (evolution) · VIZ_VALUE_FIELD (value_chain)
     VIZ_ANCHOR (unset) · WARDLEY_MAX_NODES (35 anchored / 75 global)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel_svg    # noqa: E402  (sibling: server-side SVG — the chart lives IN the body)

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
EVO_FIELD = os.environ.get(
    "VIZ_EVOLUTION_FIELD", os.environ.get("OKENGINE_VIZ_EVOLUTION_FIELD", "evolution")
)
VAL_FIELD = os.environ.get(
    "VIZ_VALUE_FIELD", os.environ.get("OKENGINE_VIZ_VALUE_FIELD", "value_chain")
)
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
# final path segment — concepts may be letter-sharded ([[concepts/t/slug]]) or flat ([[concepts/slug]])
_CLINK = re.compile(r"\[\[\s*concepts/(?:[a-z0-9._-]+/)*([a-z0-9][a-z0-9-]*)\s*(?:[|#\]])")
# any wikilink's final path segment (handles [[entities/vendor/d/diligent]], [[../x/y]], [[bare-slug]])
_ANYLINK = re.compile(r"\[\[\s*(?:[A-Za-z0-9._-]+/)*([a-z0-9][a-z0-9-]*)\s*(?:[|#\]])")
# Wardley evolution scale -> x in [0,1].
_EVO = {"genesis": 0.12, "custom": 0.37, "custom-built": 0.37, "product": 0.62,
        "rental": 0.62, "commodity": 0.87, "utility": 0.87}


def _fm(p: Path) -> dict:
    try:
        import yaml
        m = _FM.match(p.read_text(encoding="utf-8", errors="replace")[:4000])
        return (yaml.safe_load(m.group(1)) or {}) if m else {}
    except Exception:
        return {}


def _num(v):
    try:
        f = float(str(v).rstrip("%"))
        return max(0.0, min(1.0, f / 100.0 if f > 1.0 else f))
    except (TypeError, ValueError):
        return None


def _pctile(vals: dict) -> dict:
    """Rank -> [0,1] percentile per key (ties share the min rank). Empty/one -> 0.5."""
    items = sorted(vals.items(), key=lambda kv: kv[1])
    n = len(items)
    if n <= 1:
        return {k: 0.5 for k in vals}
    return {k: i / (n - 1) for i, (k, _) in enumerate(items)}


def main() -> int:
    cdir = WIKI / "concepts"
    if not cdir.is_dir():
        print("build-wardley-map: no concepts/ — nothing to map")
        print(json.dumps({"wakeAgent": False}))
        return 0
    concepts = {}   # slug -> {title, fm}
    for p in cdir.rglob("*.md"):
        if p.name.startswith(("_", "INDEX")):
            continue
        fm = _fm(p)
        concepts[p.stem.lower()] = {"title": str(fm.get("title") or p.stem), "fm": fm}

    # anchor scope (VIZ_ANCHOR): stems the anchor pages link. A stem that resolves to a concept is
    # directly in scope; a stem that resolves to an entity marks that entity as an anchor entity,
    # whose own concept links join the scope during the graph scan below (1 hop).
    anchors = [a.strip() for a in os.environ.get(
        "VIZ_ANCHOR", os.environ.get("OKENGINE_VIZ_ANCHOR", "")
    ).split(",") if a.strip()]
    anchor_stems: set = set()
    for a in anchors:
        ap = WIKI / a
        if not ap.is_file():
            print(f"build-wardley-map: WARNING anchor not found, ignoring: {a}")
            continue
        anchor_stems |= {m.lower() for m in _ANYLINK.findall(ap.read_text(encoding="utf-8", errors="replace"))}
    anchored = bool(anchor_stems)
    anchor_self = {Path(a).stem.lower() for a in anchors}
    scope = {s for s in anchor_stems if s in concepts} - anchor_self

    # reference graph: who links [[concepts/<slug>]] — total in-degree + distinct-entity coupling
    # + concept→concept edges (the value-chain dependency lines on the map)
    indeg = {s: 0 for s in concepts}
    ecoup = {s: 0 for s in concepts}
    c2c: set = set()
    for p in WIKI.rglob("*.md"):
        if p.name.startswith(("_", "INDEX")):
            continue
        rel = p.relative_to(WIKI).as_posix()
        is_entity = rel.startswith("entities/")
        src_concept = p.stem.lower() if rel.startswith("concepts/") else None
        anchor_hop = is_entity and anchored and p.stem.lower() in anchor_stems
        seen = set()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # page moved/deleted by a concurrent lane mid-scan
        for slug in _CLINK.findall(text):
            s = slug.lower()
            if s in indeg:
                indeg[s] += 1
                if anchor_hop:
                    scope.add(s)
                if src_concept and src_concept != s and src_concept in indeg:
                    c2c.add((src_concept, s))
                if is_entity and s not in seen:
                    ecoup[s] += 1
                    seen.add(s)
    scope -= anchor_self

    ubiq, coup = _pctile(indeg), _pctile(ecoup)   # percentiles over the FULL population
    if anchored:
        concepts = {s: c for s, c in concepts.items() if s in scope}
        print(f"build-wardley-map: anchored to {','.join(anchors)} — {len(concepts)} concept(s) in scope")
    nodes, heuristic_x = [], 0
    for s, c in concepts.items():
        ev = c["fm"].get(EVO_FIELD)
        x = _EVO.get(str(ev).strip().lower()) if ev is not None else None
        if x is None:
            x = _num(ev)
        if x is None:
            x = ubiq[s]
            heuristic_x += 1
        vy = c["fm"].get(VAL_FIELD)
        y = _num(vy) if vy is not None else coup[s]
        nodes.append({"slug": s, "title": c["title"], "x": round(x, 3), "y": round(y, 3),
                      "refs": indeg[s]})
    nodes.sort(key=lambda d: (-d["y"], -d["x"]))
    # A map of EVERY concept is a starfield, not a map (a live vault rendered 2,445 nodes).
    # Cap to the most-referenced concepts; percentiles stay computed over the FULL population so
    # the shown nodes keep their true global positions.
    cap = int(os.environ.get("WARDLEY_MAX_NODES", "35" if anchored else "75"))
    if cap > 0 and len(nodes) > cap:
        nodes.sort(key=lambda d: -indeg.get(d["slug"], 0))
        dropped = len(nodes) - cap
        nodes = nodes[:cap]
        nodes.sort(key=lambda d: (-d["y"], -d["x"]))
        print(f"build-wardley-map: capped to top {cap} by in-degree ({dropped} low-signal concepts not shown — WARDLEY_MAX_NODES)")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    anchor_links = ", ".join(f"[[{a[:-3] if a.endswith('.md') else a}]]" for a in anchors)
    shown = {d["slug"] for d in nodes}
    edges = sorted([a, b] for a, b in c2c if a in shown and b in shown)
    panel = {"kind": "two-axis", "x_label": "Evolution →", "y_label": "Value chain ↑",
             # the Wardley evolution stages — rendered as labeled bands with dividers
             "x_bands": [{"label": "Genesis", "from": 0, "to": 0.25},
                         {"label": "Custom", "from": 0.25, "to": 0.5},
                         {"label": "Product", "from": 0.5, "to": 0.75},
                         {"label": "Commodity", "from": 0.75, "to": 1}],
             "edges": edges,   # concept→concept links = the value-chain lines
             "nodes": [{"label": d["title"], "slug": d["slug"], "x": d["x"], "y": d["y"]}
                       for d in nodes]}
    L = ["---", "type: dashboard", 'title: "Wardley map"', f"updated: {now}",
         f"panel: {json.dumps(panel)}", "---", "",
         f"# Wardley map — {now}", "",
         panel_svg.svg_block(panel), "",
         (f"_Scoped to the neighborhood of: {anchor_links} (VIZ_ANCHOR)._\n" if anchored else ""),
         f"_{len(nodes)} concept(s) on an evolution × value-chain plane (okengine#156). "
         + (f"{heuristic_x} positioned by the graph-ubiquity HEURISTIC (no `{EVO_FIELD}` field yet — "
            f"enrich concepts with `{EVO_FIELD}`/`{VAL_FIELD}` for a true map)._"
            if heuristic_x else "Positioned from concept `evolution`/`value_chain` fields._"), ""]
    # quadrant breakdown (readable before the two-axis render lands)
    quad = {"emerging (novel, foundational)": [], "established (settled, foundational)": [],
            "experimental (novel, surface)": [], "commodity (settled, surface)": []}
    for d in nodes:
        k = ("established" if d["x"] >= 0.5 else "emerging") if d["y"] >= 0.5 else \
            ("commodity" if d["x"] >= 0.5 else "experimental")
        key = next(q for q in quad if q.startswith(k))
        quad[key].append(d)
    for q, ds in quad.items():
        if ds:
            L += [f"## {q}", ""] + [f"- [[concepts/{d['slug']}|{d['title']}]] "
                                    f"(x={d['x']}, y={d['y']}, {d['refs']} refs)" for d in ds] + [""]
    L += ["## Coordinates", "", "| Concept | Evolution (x) | Value chain (y) | Refs |", "|---|---|---|---|"]
    for d in nodes:
        L.append(f"| [[concepts/{d['slug']}|{d['title']}]] | {d['x']} | {d['y']} | {d['refs']} |")
    L.append("")
    out = WIKI / "dashboards" / "wardley.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"build-wardley-map: {len(nodes)} concept(s), {heuristic_x} heuristic-positioned -> "
          "wiki/dashboards/wardley.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
