#!/usr/bin/env python3
"""discover_competitors.py — propose competitor CANDIDATES from the ingested graph (no_agent).

The watchlist is curated by the operator; this surfaces companies the vault ALREADY knows about that
aren't on the watchlist yet, ranked by evidence, so the operator can promote them. Honest by
construction: it writes a review dashboard (dashboards/competitive/discovery.md) — it NEVER fabricates
a quadrant position and NEVER auto-edits the watchlist. Turns "I list my rivals" into "I name myself
and the system surfaces the field."

Deterministic signals, all from the vault:
  - co-occurrence  : a company cited in the same SOURCE pages as your home company / watched competitors
  - segment match  : a company whose `segment` matches a watched segment but isn't listed
  - prominence     : how many sources reference the company (salience in your feed)
  - alternatives-language : companies named in COMPETITIVE LANGUAGE near your home/tracked names in
    source bodies ("alternatives to X", "X vs Y", "competitors to X", "switch from X") — catches
    rivals that have NO entity yet, which the entity-graph signals can't see.

Config (watchlist): optional `home: <entity-slug>` (your company — anchors co-occurrence + language) +
`segments`. Env: WIKI_PATH · WATCHLIST_PATH · DISCOVERY_TYPES
(competitor,company,vendor,organization,identity) · DISCOVERY_TOP (25) · DISCOVERY_MIN_SCORE (1) ·
DISCOVERY_ALT (1=on) · DISCOVERY_ALT_MIN (1)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comp_lib  # noqa: E402

WIKI = comp_lib.WIKI
DASH = WIKI / "dashboards" / "competitive" / "discovery.md"
TYPES = {t.strip().lower() for t in os.environ.get(
    "DISCOVERY_TYPES", "competitor,company,vendor,organization,identity").split(",") if t.strip()}
TOP = int(os.environ.get("DISCOVERY_TOP", "25"))
MIN_SCORE = int(os.environ.get("DISCOVERY_MIN_SCORE", "1"))
ALT_ON = os.environ.get("DISCOVERY_ALT", "1").strip() not in ("0", "false", "no", "")
ALT_MIN = int(os.environ.get("DISCOVERY_ALT_MIN", "1"))
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)

# --- "alternatives to X" language mining ---
_TRIGGERS = re.compile(
    r"alternatives?|competitors?|rivals?|substitutes?|\bvs\.?\b|versus|compared (?:to|with)|"
    r"switch(?:ing)? (?:from|to)|migrat\w* (?:from|to)|instead of", re.I)
# a Title-Case / branded name: 1-4 capitalized tokens. Tokens are joined only by SPACES/TABS (never a
# newline — a name can't cross lines), and the name is cut at the first ". " in extraction (a sentence
# boundary), so "NovaForge AI. Many" -> "NovaForge AI".
_NAME = re.compile(r"\b[A-Z][A-Za-z0-9.&'+-]*(?:[ \t]+(?:&[ \t]+)?[A-Z][A-Za-z0-9.&'+-]*){0,3}\b")
_SUFFIX = re.compile(r"(Inc|LLC|Ltd|Corp|Co|AI|Labs?|Systems?|Software|Technolog\w+|Group|Networks?|"
                     r"Security|Cloud|Data|Platform|\.io|\.com|\.ai|\.dev)$")
# common Title-Case words that aren't companies (sentence starters, triggers, filler)
_STOP = {w.lower() for w in (
    "The A An This That These Those We Our You Your It They Looking Consider Read More See Learn "
    "Best Top Why How What When Where Who Which If But And Or So Then Also Here There Now New "
    "Alternatives Alternative Competitors Competitor Rivals Rival Versus Compared Switch Switching "
    "Migrate Instead Over Replace Substitute Substitutes For To Of From With Like Such As Other "
    "Unlike Both Either Neither Pricing Pros Cons Review Reviews Guide Comparison Vs January February "
    "March April May June July August September October November December Monday Tuesday Wednesday "
    "Thursday Friday Saturday Sunday").split()}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def _looks_company(name: str) -> bool:
    return len(name.split()) >= 2 or bool(_SUFFIX.search(name))


def _stem(ref) -> str:
    return str(ref).strip().strip("[]").strip("/").split("/")[-1].lower().removesuffix(".md")


def _load_entities():
    out = {}
    edir = WIKI / "entities"
    if not edir.is_dir():
        return out
    for p in edir.rglob("*.md"):
        if p.name.startswith(("_", ".")) or p.name.startswith("INDEX"):
            continue
        txt = p.read_text(errors="replace")[:8000]
        m = _FM.search(txt)
        if not m:
            continue
        try:
            import yaml
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        if not isinstance(fm, dict):
            continue
        slug = p.relative_to(edir).with_suffix("").as_posix()
        srcs = fm.get("sources")
        srcs = srcs if isinstance(srcs, list) else ([srcs] if srcs else [])
        out[slug] = {"slug": slug, "name": fm.get("title") or fm.get("name") or _stem(slug),
                     "type": str(fm.get("type") or "").lower(),
                     "segment": str(fm.get("segment") or "").strip().lower(),
                     "sources": {_stem(s) for s in srcs if s}}
    return out


def _mine_alternatives(anchor_names, exclude_norms):
    """Scan source BODIES for competitive language near an anchor (home/tracked) name and harvest the
    OTHER company names mentioned. Returns {norm: {name, count, evidence:[(src, snippet)]}}. Excludes
    anchors, known entity names, and obvious non-companies. Catches rivals with no entity yet."""
    found = {}
    sdir = WIKI / "sources"
    longs = [a for a in anchor_names if len(a) >= 4]
    if not sdir.is_dir() or not longs:
        return found
    arx = re.compile("|".join(re.escape(a) for a in longs), re.I)
    anchor_norms = {_norm(a) for a in anchor_names}
    for p in sdir.rglob("*.md"):
        if p.name.startswith(("_", ".")) or p.name.startswith("INDEX"):
            continue
        raw = p.read_text(errors="replace")[:50000]
        body = _FM.sub("", raw, count=1)                    # BODY only — skip frontmatter (publishers, titles)
        for am in arx.finditer(body):
            ctx = body[max(0, am.start() - 60): am.end() + 60]
            if not _TRIGGERS.search(ctx):
                continue                                    # competitive language near the anchor?
            win = body[max(0, am.start() - 40): am.end() + 200]
            for nm in _NAME.finditer(win):
                name = re.split(r"\.\s", nm.group(0))[0].strip(" .,&")   # cut at the first sentence boundary
                norm = _norm(name)
                if (not name or norm in anchor_norms or norm in exclude_norms
                        or norm in _STOP or not _looks_company(name)):
                    continue
                if any(norm in a or a in norm for a in anchor_norms):
                    continue
                e = found.setdefault(norm, {"name": name, "count": 0, "evidence": []})
                e["count"] += 1
                if len(e["evidence"]) < 2:
                    snip = re.sub(r"\s+", " ", win).strip()[:110]
                    e["evidence"].append((p.relative_to(WIKI).with_suffix("").as_posix(), snip))
    return {k: v for k, v in found.items() if v["count"] >= ALT_MIN}


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    wl = comp_lib.read_watchlist()
    segments = wl.get("segments") or {}
    home = (wl.get("home") or "").strip()
    known, watched = set(), set()
    if home:
        known.add(_stem(home))
    for key, seg in segments.items():
        watched.add(str(key).lower())
        watched.add(str((seg or {}).get("label") or "").lower())
        for c in (seg or {}).get("competitors") or []:
            known.add(_stem(c))
    watched.discard("")

    ents = _load_entities()
    anchor_sources = set()
    anchor_names = []
    all_entity_norms = set()
    for slug, e in ents.items():
        all_entity_norms.add(_norm(e["name"]))
        all_entity_norms.add(_stem(slug))
        if _stem(slug) in known:
            anchor_sources |= e["sources"]
            anchor_names.append(e["name"])
    if home and not anchor_names:
        anchor_names.append(home.replace("-", " ").title())

    # --- entity-graph candidates (co-occurrence / segment / prominence) ---
    cands = []
    for slug, e in ents.items():
        if _stem(slug) in known or (TYPES and e["type"] and e["type"] not in TYPES):
            continue
        shared = e["sources"] & anchor_sources
        prominence = len(e["sources"])
        seg_match = bool(e["segment"]) and e["segment"] in watched
        score = len(shared) * 2 + prominence + (3 if seg_match else 0)
        if score < MIN_SCORE:
            continue
        why = []
        if shared:
            why.append(f"co-cited with tracked cos in {len(shared)} source(s)")
        if seg_match:
            why.append(f"in watched segment '{e['segment']}'")
        if prominence:
            why.append(f"{prominence} source(s)")
        cands.append({"slug": slug, "segment": e["segment"], "score": score, "why": "; ".join(why)})
    cands.sort(key=lambda c: (-c["score"], c["slug"]))
    cands = cands[:TOP]

    # --- language-mined candidates (no entity yet) ---
    tracked_norms = {_norm(n) for n in anchor_names} | {_stem(s) for s in known}
    mined = _mine_alternatives(anchor_names, all_entity_norms | tracked_norms) if ALT_ON else {}
    mined_list = sorted(mined.values(), key=lambda m: (-m["count"], m["name"]))[:TOP]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Competitor discovery"', f"updated: {now}", "---", "",
         f"# Competitor discovery — {now}", "",
         f"_Candidates only — promote the real ones into `config/competitive-watchlist.yaml`; nothing "
         f"here is auto-added or positioned. {len(known)} tracked, {len(ents)} entities scanned"
         f"{', anchored on home `' + home + '`' if home else ''}._", "",
         f"**{len(cands)} entity candidate(s)** · **{len(mined_list)} language-mined name(s)**", ""]
    if cands:
        L += ["## Known to the vault (have entity pages)", "",
              "| # | Candidate | Segment | Score | Why |", "|--:|---|---|--:|---|"]
        for i, c in enumerate(cands, 1):
            L.append(f"| {i} | [[entities/{c['slug']}]] | {c['segment'] or '—'} | {c['score']} | {c['why']} |")
        L += ["", "Promote into a segment's `competitors:` list, e.g.:", "", "```yaml",
              "segments:", "  <your-segment>:",
              "    competitors: [" + ", ".join(_stem(c["slug"]) for c in cands[:6]) + "]", "```", ""]
    if mined_list:
        L += ["## Named as alternatives (no entity yet)", "",
              "_Harvested from competitive language in source bodies ('alternatives to …', 'X vs …'). "
              "Lower confidence — review, then create entities for the real ones._", "",
              "| Name | Mentions | Example context |", "|---|--:|---|"]
        for m in mined_list:
            ev = m["evidence"][0] if m["evidence"] else ("", "")
            L.append(f"| {m['name']} | {m['count']} | [{ev[0]}]({ev[0]}.md): …{ev[1]}… |")
        L.append("")
    if not cands and not mined_list:
        L += ["_No off-watchlist candidates yet — add broader sources to `feeds/feeds.opml` so the "
              "ingest pipeline creates more company entities + source coverage, then re-run._", ""]
    DASH.parent.mkdir(parents=True, exist_ok=True)
    DASH.write_text("\n".join(L), encoding="utf-8")
    print(f"discover-competitors: {len(cands)} entity candidate(s) + {len(mined_list)} language-mined "
          f"from {len(ents)} entities ({len(known)} tracked) -> wiki/dashboards/competitive/discovery.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
