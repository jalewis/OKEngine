#!/usr/bin/env python3
"""actor-risk-rank — deterministic target-relative actor ranking (okengine#170 v1).

Scores every actor page against each operator-configured target from the #168
backlink artifact (wiki/.backlinks.json) + source dates, and writes explainable
dashboards under dashboards/actor-risk/. Design: docs/design/actor-risk-ranking.md.

Hard rules encoded here:
  - no config -> LOUD no-op (exit 0, one line);
  - person targets -> REFUSED at parse (exit 2);
  - artifact absent/stale -> LOUD skip (exit 1) — never rank a stale graph;
  - confidence counts DISTINCT ORIGIN DOMAINS, never items — a single syndicated
    report cannot lift a band;
  - a band above 'moderate' needs >=2 non-zero drivers AND >= min_origin_domains;
  - unknowns (zero-evidence drivers, unresolved alias candidates) are printed on
    the dashboard and cap the band at 'elevated'.

Deterministic, stdlib+yaml only, no network, no model.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import yaml

FM_RE = re.compile(r"^---\n(.*?)\n(?:---|\.\.\.)", re.S)
HEAD_BYTES = 4096

DEFAULT_ACTOR_TYPES = {"threat-actor", "intrusion-set"}
DEFAULT_CAPABILITY_TYPES = {"malware", "tool", "attack-pattern", "software"}
DEFAULT_VULN_TYPES = {"vulnerability"}
DEFAULT_SECTOR_TYPES = {"segment", "sector", "concept"}

BANDS = ["low", "moderate", "elevated", "high"]
WEIGHTS = {"direct": 30, "opportunity": 25, "capability": 15, "intent": 20, "recency": 10}


def _fm(path: Path) -> dict:
    try:
        head = path.open("rb").read(HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {}
    m = FM_RE.match(head)
    if not m:
        return {}
    try:
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_config(vault: Path) -> dict | None:
    rel = os.environ.get("ACTOR_RISK_TARGETS", "config/actor-risk-targets.yaml")
    p = vault / rel
    if not p.is_file():
        print(f"actor-risk-rank: no-op — no target config at {rel} "
              "(operator-owned; see the extension README)")
        return None
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    targets = cfg.get("targets") or {}
    for name, t in targets.items():
        if str((t or {}).get("type", "")).strip().lower() == "person":
            print(f"ERROR: target '{name}' is type: person — v1 refuses person targets "
                  "(deterministic ranking of people is out of scope by design; see "
                  "docs/design/actor-risk-ranking.md §3)", file=sys.stderr)
            sys.exit(2)
    if not targets:
        print("actor-risk-rank: no-op — config present but declares no targets")
        return None
    return cfg


def load_artifact(vault: Path) -> dict:
    p = vault / "wiki" / ".backlinks.json"
    max_age_h = int(os.environ.get("ACTOR_RISK_ARTIFACT_MAX_AGE_HOURS", "48"))
    if not p.is_file():
        print("ERROR: wiki/.backlinks.json missing — enable the backlinks-refresh "
              "engine cron (okengine#168) before this lane", file=sys.stderr)
        sys.exit(1)
    age_h = (time.time() - p.stat().st_mtime) / 3600
    if age_h > max_age_h:
        print(f"ERROR: backlink artifact is {age_h:.0f}h old (ceiling {max_age_h}h) — "
              "skipping rather than ranking a stale graph", file=sys.stderr)
        sys.exit(1)
    return json.loads(p.read_text(encoding="utf-8"))


def build_graph(artifact: dict) -> tuple[dict, dict]:
    """Undirected co-edge sets per page + the raw referrer map."""
    bl = artifact.get("backlinks") or {}
    edges: dict[str, set] = defaultdict(set)
    for tgt, refs in bl.items():
        for r in refs:
            src = r.get("key")
            if not src:
                continue
            edges[tgt].add(src)
            edges[src].add(tgt)
    return edges, bl


def classify_pages(vault: Path, cfg: dict) -> dict[str, dict]:
    """slug -> {type, title, aliases, published, url, publisher} for wiki pages
    (entities + sources are what scoring needs; heads only)."""
    wiki = vault / "wiki"
    tmap = (cfg.get("scoring") or {}).get("type_map") or {}
    out: dict[str, dict] = {}
    for top in ("entities", "concepts", "segments", "sources"):
        base = wiki / top
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            n = p.name
            if n.startswith(("_", ".", "INDEX", "index")) or ".bak." in n:
                continue
            fm = _fm(p)
            if not fm:
                continue
            key = str(p.relative_to(wiki))[:-3]
            t = str(fm.get("type") or "").strip().strip('"')
            t = tmap.get(t, t)
            out[key] = {
                "type": t,
                "title": str(fm.get("title") or fm.get("name") or key.split("/")[-1]),
                "aliases": [str(a) for a in (fm.get("aliases") or []) if a],
                "published": str(fm.get("published") or fm.get("date") or ""),
                "url": str(fm.get("url") or ""),
                "publisher": str(fm.get("publisher") or ""),
            }
    return out


def fold_aliases(actors: list[str], pages: dict) -> tuple[dict, list]:
    """canonical -> [folded slugs]; an actor whose page basename matches another
    actor's declared alias folds into it. Returns (fold_map, unresolved_report)."""
    slug_of = {a.split("/")[-1].lower(): a for a in actors}
    alias_to_canon = {}
    for a in actors:
        for al in pages[a]["aliases"]:
            alias_to_canon[al.strip().lower().replace(" ", "-")] = a
    fold: dict[str, list] = defaultdict(list)
    for a in actors:
        base = a.split("/")[-1].lower()
        canon = alias_to_canon.get(base)
        if canon and canon != a:
            fold[canon].append(a)
    # unresolved near-duplicates: actor slugs where one is a prefix of the other
    # and neither declares the alias — reported as unknowns, never auto-merged.
    unresolved = []
    folded_away = {x for v in fold.values() for x in v}
    names = sorted(s for s in slug_of if slug_of[s] not in folded_away)
    for i, n1 in enumerate(names):
        for n2 in names[i + 1:]:
            if n2.startswith(n1 + "-") or n1.startswith(n2 + "-"):
                unresolved.append((slug_of[n1], slug_of[n2]))
    return fold, unresolved


def origin_domain(meta: dict) -> str:
    if meta.get("url"):
        try:
            host = urlparse(meta["url"]).netloc.lower()
            return host[4:] if host.startswith("www.") else host
        except Exception:
            pass
    return meta.get("publisher", "").strip().lower()


def _within_horizon(published: str, horizon_days: int, now: float) -> bool:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", published or "")
    if not m:
        return False
    try:
        ts = time.mktime(time.strptime(m.group(0), "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return False
    return (now - ts) <= horizon_days * 86400


def score_actor(actor: str, folded: list, target: dict, edges: dict, pages: dict,
                scoring: dict, now: float) -> dict:
    keys = [actor] + folded
    nbrs = set()
    for k in keys:
        nbrs |= edges.get(k, set())
    nbrs -= set(keys)

    def typed(kinds):
        return {n for n in nbrs if pages.get(n, {}).get("type") in kinds}

    # driver type sets are config so the scorer stays ontology-free (okengine#174:
    # the vendor variant ranks `vendor` pages with capability = product/component
    # footprint — same arithmetic, different nouns)
    cap_types = set(scoring.get("capability_types") or DEFAULT_CAPABILITY_TYPES)
    vuln_types = set(scoring.get("vulnerability_types") or DEFAULT_VULN_TYPES)
    sector_types = set(scoring.get("sector_types") or DEFAULT_SECTOR_TYPES)
    horizon = int(scoring.get("horizon_days", 180))
    techs = {str(t).strip() for t in (target.get("technologies") or [])}
    sectors = {str(s).strip().lower() for s in (target.get("sectors") or [])}
    tgt_entity = str(target.get("entity") or "").strip()

    drivers: dict[str, dict] = {}
    drivers["direct"] = {"evidence": sorted(nbrs & {tgt_entity}) if tgt_entity else []}
    vulns = typed(vuln_types)
    opp = {v for v in vulns if edges.get(v, set()) & techs}
    drivers["opportunity"] = {"evidence": sorted(opp)}
    drivers["capability"] = {"evidence": sorted(typed(cap_types))}
    intent = {n for n in typed(sector_types)
              if n.split("/")[-1].lower() in sectors}
    drivers["intent"] = {"evidence": sorted(intent)}

    srcs = {n for n in nbrs if n.startswith("sources/")}
    recent = {s for s in srcs
              if _within_horizon(pages.get(s, {}).get("published", ""), horizon, now)}
    drivers["recency"] = {"evidence": sorted(recent)}
    domains = {d for d in (origin_domain(pages.get(s, {})) for s in recent) if d}

    # normalized 0..1 per driver (diminishing returns), then weighted 0..100 sort key
    def sat(n, k=5):
        return min(1.0, n / k)

    parts = {
        "direct": sat(len(drivers["direct"]["evidence"]), 1),
        "opportunity": sat(len(opp), 3),
        "capability": sat(len(drivers["capability"]["evidence"]), 8),
        "intent": sat(len(intent), 2),
        "recency": sat(len(recent), 6),
    }
    score = round(sum(WEIGHTS[d] * parts[d] for d in WEIGHTS))

    nonzero = [d for d in WEIGHTS if parts[d] > 0]
    unknowns = [f"no {d} evidence" for d in WEIGHTS if parts[d] == 0]
    band = BANDS[0]
    if score >= 15:
        band = "moderate"
    min_dom = int(scoring.get("min_origin_domains", 2))
    if score >= 40 and len(nonzero) >= 2 and len(domains) >= min_dom:
        band = "elevated"
        if score >= 65 and not unknowns:
            band = "high"
    if len(domains) < min_dom and band in ("elevated", "high"):
        band = "moderate"   # syndication gate (belt and braces)

    return {"actor": actor, "folded": folded, "score": score, "band": band,
            "drivers": drivers, "parts": parts, "domains": sorted(domains),
            "unknowns": unknowns}


def render(vault: Path, cfg: dict, results: dict, unresolved: list,
           artifact: dict, now: float) -> list[Path]:
    outdir = vault / "wiki" / "dashboards" / "actor-risk"
    outdir.mkdir(parents=True, exist_ok=True)
    scoring = cfg.get("scoring") or {}
    top_n = int(scoring.get("top_n", 25))
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
    built = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(artifact.get("built_at", 0)))
    written = []

    def link(k):
        return f"[[{k}]]"

    lines = ["---", "type: dashboard", f"updated: {time.strftime('%Y-%m-%d', time.gmtime(now))}",
             "---", "# Actor risk rankings", "",
             f"Generated {stamp} · graph artifact built {built} · horizon "
             f"{scoring.get('horizon_days', 180)}d. Scores are horizon-bound sort keys; "
             "read the BAND + drivers. Bands above moderate require >= 2 independent "
             "drivers and >= "
             f"{scoring.get('min_origin_domains', 2)} distinct origin domains.", ""]
    for tname, rows in results.items():
        lines += [f"## Target: {tname}", "",
                  "| # | actor | band | score | top drivers | origin domains |",
                  "|---|---|---|---|---|---|"]
        for i, r in enumerate(rows[:top_n], 1):
            tops = ", ".join(d for d in WEIGHTS if r["parts"][d] > 0) or "—"
            lines.append(f"| {i} | {link(r['actor'])} | **{r['band']}** | {r['score']} "
                         f"| {tops} | {len(r['domains'])} |")
        lines.append("")
    if unresolved:
        lines += ["## Unresolved alias candidates (entity-resolution gap — not merged)", ""]
        lines += [f"- {link(a)} vs {link(b)}" for a, b in unresolved[:20]]
        lines.append("")
    p = outdir / "rankings.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written.append(p)

    for tname, rows in results.items():
        tl = ["---", "type: dashboard",
              f"updated: {time.strftime('%Y-%m-%d', time.gmtime(now))}", "---",
              f"# Actor risk — target: {tname}", "",
              f"Generated {stamp} · graph built {built}.", ""]
        for r in rows[:top_n]:
            tl += [f"## {link(r['actor'])} — **{r['band']}** ({r['score']})", ""]
            if r["folded"]:
                tl.append(f"- folded aliases: {', '.join(link(f) for f in r['folded'])}")
            for d in WEIGHTS:
                ev = r["drivers"][d]["evidence"]
                if ev:
                    shown = ", ".join(link(e) for e in ev[:8])
                    more = f" (+{len(ev) - 8} more)" if len(ev) > 8 else ""
                    tl.append(f"- **{d}**: {len(ev)} — {shown}{more}")
            if r["domains"]:
                tl.append(f"- **origin domains** ({len(r['domains'])}): "
                          + ", ".join(r["domains"][:10]))
            if r["unknowns"]:
                tl.append(f"- **unknowns**: {'; '.join(r['unknowns'])}")
            tl.append("")
        p = outdir / f"{re.sub(r'[^a-z0-9-]+', '-', tname.lower()).strip('-')}.md"
        p.write_text("\n".join(tl) + "\n", encoding="utf-8")
        written.append(p)
    return written


def _resolve_vault() -> Path:
    """Vault root from WIKI_PATH — the engine-wide standard (fleet_health, lacuna, …), never cwd.
    Falling back to os.getcwd() made this lane silently score whatever tree the cron happened to
    run from (the backlinks_refresh cwd-resolution regression). VAULT_DIR kept as a legacy override,
    but WIKI_PATH wins and the default is /opt/vault — cwd is never consulted."""
    return Path(os.environ.get("WIKI_PATH") or os.environ.get("VAULT_DIR") or "/opt/vault").resolve()


def main() -> int:
    vault = _resolve_vault()
    if not (vault / "wiki").is_dir():
        print(f"ERROR: no wiki/ under {vault}", file=sys.stderr)
        return 2
    cfg = load_config(vault)
    if cfg is None:
        return 0
    artifact = load_artifact(vault)
    now = time.time()
    edges, _ = build_graph(artifact)
    pages = classify_pages(vault, cfg)

    scoring = cfg.get("scoring") or {}
    actor_types = set((scoring.get("actor_types") or [])) or DEFAULT_ACTOR_TYPES
    exclude = set(scoring.get("exclude_actors") or [])
    actors = [k for k, m in pages.items()
              if m["type"] in actor_types and k.split("/")[-1] not in exclude]
    if not actors:
        print("actor-risk-rank: no-op — no actor-typed pages found "
              f"(looked for types {sorted(actor_types)})")
        return 0
    fold, unresolved = fold_aliases(actors, pages)
    folded_away = {x for v in fold.values() for x in v}

    results = {}
    for tname, target in (cfg.get("targets") or {}).items():
        rows = [score_actor(a, fold.get(a, []), target or {}, edges, pages, scoring, now)
                for a in actors if a not in folded_away]
        rows.sort(key=lambda r: (-r["score"], r["actor"]))
        results[tname] = rows
    written = render(vault, cfg, results, unresolved, artifact, now)
    print(f"actor-risk-rank: {len(actors) - len(folded_away)} actors × "
          f"{len(results)} target(s) -> {len(written)} dashboard(s); "
          f"{len(folded_away)} alias page(s) folded, {len(unresolved)} unresolved pair(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
