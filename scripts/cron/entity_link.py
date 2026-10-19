#!/usr/bin/env python3
"""entity_link.py — link sources to the EXISTING entities they name (okengine#888). No model.

Entity lanes mint an entity from the one source they were handed and never revisit it, so an
entity's `sources:` stays at one citation however often the corpus mentions it again. Measured on
one live vault: 871 refs across 831 entities; 131 of ~3,000 recent sources cited by any entity;
0 entities with >= 3 recent citations. Everything that reads the entity graph as a frequency
signal (trends-refresh's movers, lacuna's field density) starved on it.

This lane appends every live source that names an entity — by the entity's `name`, `title` or
`aliases`, matched as whole normalized tokens — to that entity's `sources:`. It never creates an
entity and never guesses:
  - a name shared by two entities is AMBIGUOUS and links nothing (reported);
  - a name shorter than OKENGINE_ENTITY_LINK_MIN_CHARS links nothing (reported);
  - a name found in more than OKENGINE_ENTITY_LINK_MAX_SHARE of the scanned sources is too
    generic to be evidence and links nothing (reported);
  - OKENGINE_ENTITY_LINK_EXCLUDE is the PACK's regex for names it knows to be generic;
  - tombstoned entities and sources never link.
Normalization is id_lib's: NFKD ascii-fold, lowercase, non-alphanumeric runs as separators.

Stale refs: a `sources/<path>` ref whose page moved is repaired to the source's current seat when
exactly one live source has that stem; otherwise it is reported and left alone.

It bypasses the SERVER, never the INVARIANTS: every write is the write path's own `_update` under
the authenticated `cron:entity-link` actor, held to this lane's output_contract and the policy
plane. Each run appends one row to wiki/operational/entity-link-snapshots.md (the share of recent
sources any entity cites), which kb-health renders — no alert.

Modes: default scans sources published in the last OKENGINE_ENTITY_LINK_WINDOW_DAYS and writes up
to OKENGINE_ENTITY_LINK_LIMIT entity updates. `--backfill` scans the whole corpus and is a DRY RUN
unless `--apply`. `--dry-run` classifies without writing in either mode.
Env: WIKI_PATH (/opt/vault) · OKENGINE_MCP_DIR (/opt/hermes/okengine-mcp).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from yaml import YAMLError

sys.path.insert(0, str(Path(__file__).resolve().parent))  # staged siblings
import generated_page  # noqa: E402 — keeps a regenerated page's id (okengine#933)

ACTOR = "cron:entity-link"
VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
LIMIT = int(os.environ.get("OKENGINE_ENTITY_LINK_LIMIT", "200"))
WINDOW_DAYS = int(os.environ.get("OKENGINE_ENTITY_LINK_WINDOW_DAYS", "14"))
RATIO_WINDOW_DAYS = int(os.environ.get("OKENGINE_ENTITY_LINK_RATIO_DAYS", "42"))
MIN_CHARS = int(os.environ.get("OKENGINE_ENTITY_LINK_MIN_CHARS", "4"))
MAX_SHARE = float(os.environ.get("OKENGINE_ENTITY_LINK_MAX_SHARE", "0.05"))
MIN_SOURCES_FOR_SHARE = 20      # below this many scanned sources, share is not evidence
EXCLUDE = os.environ.get("OKENGINE_ENTITY_LINK_EXCLUDE", "").strip()
MAX_NGRAM = 8
SNAPSHOT = WIKI / "operational" / "entity-link-snapshots.md"
_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_SPLIT_ANY = re.compile(r"[^A-Za-z0-9]+")
_REF = re.compile(r"^\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]\Z")


def fold(text: str, lower: bool = True) -> list[str]:
    """id_lib.normalize_key's folding, kept as a token list (not truncated or hashed)."""
    ascii_s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return [t for t in _SPLIT_ANY.split(ascii_s.lower() if lower else ascii_s) if t]


def _lossy(name: str) -> bool:
    """Folding would drop an alphanumeric character (`τ²-bench` folds to the word `bench`)."""
    decomposed = unicodedata.normalize("NFKD", name)
    return any(ch.isalnum() and not ch.isascii() and unicodedata.category(ch)[0] != "M"
               for ch in decomposed)


def _acronym(name: str) -> bool:
    """An all-caps short name (`CLEAR`, `MAPS`) is an acronym: it must match in its exact case,
    or every lowercase English use of the word would link."""
    letters = [ch for ch in name if ch.isalpha()]
    return bool(letters) and all(ch.isupper() for ch in letters) and len(letters) <= 8


def _read(page: Path) -> tuple[dict, str] | None:
    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FM.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except YAMLError:
        return None
    if not isinstance(fm, dict) or str(fm.get("status") or "").lower() == "tombstoned":
        return None
    return fm, text[m.end():]


def _date(fm: dict, seat: str) -> date | None:
    raw = fm.get("published")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return datetime.strptime(str(raw or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    m = re.search(r"(\d{4})/(\d{2})/", seat)
    return date(int(m.group(1)), int(m.group(2)), 15) if m and 1 <= int(m.group(2)) <= 12 else None


def _pages(namespace: str):
    base = WIKI / namespace
    for page in sorted(base.rglob("*.md")) if base.is_dir() else ():
        if page.name.startswith(("_", "INDEX")):
            continue
        yield page


def load_sources(since: date | None) -> list[dict]:
    """Live source pages (newest first): seat `sources/<path>`, id, date, token list."""
    out = []
    for page in _pages("sources"):
        parsed = _read(page)
        if parsed is None:
            continue
        fm, body = parsed
        seat = page.relative_to(WIKI).with_suffix("").as_posix()
        when = _date(fm, seat)
        if since is not None and (when is None or when < since):
            continue
        out.append({"seat": seat, "id": str(fm.get("id") or ""), "date": when,
                    "tokens": fold(f"{fm.get('title') or ''}\n{body}", lower=False)})
    out.sort(key=lambda s: (s["date"] or date.min, s["seat"]), reverse=True)
    return out


def _as_list(value) -> list:
    """A list-valued field as a list: None -> [], a list/tuple as-is, ANY other value (a bare str,
    int, float, bool, date or mapping) -> a one-item list (okengine#994 [20]). Iterating a bare int
    crashed the lane; iterating a mapping silently flattened it to its keys."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _names(fm: dict) -> set[str]:
    names = {str(fm.get(k)).strip() for k in ("name", "title") if str(fm.get(k) or "").strip()}
    for alias in _as_list(fm.get("aliases")):
        # a YAML boolean (`yes`, `true`) is never a name: "True" would link every source saying it
        if (isinstance(alias, (str, int, float)) and not isinstance(alias, bool)
                and str(alias).strip()):
            names.add(str(alias).strip())
    return names


def _norm_ref(ref) -> str:
    text = str(ref or "").strip()
    m = _REF.match(text)
    return (m.group(1) if m else text).strip().removesuffix(".md")


def load_entities() -> dict[str, dict]:
    out = {}
    for page in _pages("entities"):
        parsed = _read(page)
        if parsed is None:
            continue
        fm, _body = parsed
        out[page.relative_to(WIKI).as_posix()] = {"names": _names(fm),
                                                   "refs": _as_list(fm.get("sources"))}
    return out


def name_index(entities: dict[str, dict]) -> tuple[dict[tuple, tuple], Counter]:
    """normalized tokens -> (the ONE entity owning the name, exact-case tokens if an acronym);
    plus the reasons names were dropped."""
    exclude = re.compile(EXCLUDE, re.I) if EXCLUDE else None
    owners: dict[tuple, set[str]] = defaultdict(set)
    cased: dict[tuple, tuple] = {}
    dropped: Counter[str] = Counter()
    for rel, ent in entities.items():
        for name in ent["names"]:
            toks = tuple(fold(name))
            if _lossy(name):
                dropped["name-lossy-fold"] += 1
            elif not toks or len("".join(toks)) < MIN_CHARS:
                dropped["name-too-short"] += 1
            elif len(toks) > MAX_NGRAM:
                dropped["name-too-long"] += 1
            elif exclude and exclude.search(name):
                dropped["name-excluded-by-pack"] += 1
            else:
                owners[toks].add(rel)
                if _acronym(name):
                    cased[toks] = tuple(fold(name, lower=False))
    index = {}
    for toks, rels in owners.items():
        if len(rels) > 1:
            dropped["name-ambiguous"] += 1
        else:
            index[toks] = (next(iter(rels)), cased.get(toks))
    return index, dropped


def _names_in(cased: list[str], index: dict[tuple, tuple]) -> set[tuple]:
    """Leftmost-longest: at each position take the LONGEST known name and skip past it, so
    `GPT-5.5` counts as `gpt 5 5`, never also as its prefix `gpt 5`."""
    lower = [t.lower() for t in cased]
    found, i = set(), 0
    while i < len(lower):
        for n in range(min(MAX_NGRAM, len(lower) - i), 0, -1):
            key = tuple(lower[i:i + n])
            hit = index.get(key)
            if hit and (hit[1] is None or tuple(cased[i:i + n]) == hit[1]):
                found.add(key)
                i += n
                break
        else:
            i += 1
    return found


def match(sources: list[dict], index: dict[tuple, tuple]) -> tuple[dict[str, list[dict]], Counter]:
    """entity rel -> sources naming it (newest first); drops names too common to be evidence."""
    by_name: dict[tuple, list[dict]] = defaultdict(list)
    for src in sources:
        for key in _names_in(src["tokens"], index):
            by_name[key].append(src)
    dropped: Counter[str] = Counter()
    hits: dict[str, dict[str, dict]] = defaultdict(dict)
    for key, srcs in by_name.items():
        if len(sources) >= MIN_SOURCES_FOR_SHARE and len(srcs) > MAX_SHARE * len(sources):
            dropped["name-too-common"] += 1
            continue
        for src in srcs:
            hits[index[key][0]][src["seat"]] = src
    ordered = {rel: sorted(s.values(), key=lambda x: (x["date"] or date.min, x["seat"]),
                           reverse=True) for rel, s in hits.items()}
    return ordered, dropped


def top_names(index: dict[tuple, tuple], hits: dict[str, list[dict]], n: int = 15) -> list[str]:
    """The entities linking the most sources, with their names — the list an operator reviews
    for generic names (for OKENGINE_ENTITY_LINK_EXCLUDE) before a backfill is applied."""
    names: dict[str, list[str]] = defaultdict(list)
    for toks, (rel, _cased) in index.items():
        names[rel].append(" ".join(toks))
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:n]
    return [f"{len(srcs)} {rel} ({', '.join(sorted(names[rel]))})" for rel, srcs in ranked]


def plan(entity: dict, found: list[dict], live: set[str], by_stem: dict[str, list[str]]):
    """(new refs list or None, links added, repairs, unrepairable stale refs)."""
    have = {_norm_ref(r) for r in entity["refs"]}
    refs, repaired, stale = [], 0, 0
    for ref in entity["refs"]:
        target = _norm_ref(ref)
        if target.startswith("sources/") and target not in live:
            seats = by_stem.get(target.rsplit("/", 1)[-1], [])
            if len(seats) == 1:
                refs.append(seats[0])
                have.add(seats[0])
                repaired += 1
                continue
            stale += 1
        refs.append(ref)
    added = [s["seat"] for s in found
             if s["seat"] not in have and not (s["id"] and s["id"] in have)]
    refs.extend(added)
    return (refs if added or repaired else None), len(added), repaired, stale


def _write_server():
    """The write path, bound to this lane's identity (as raw_compile binds its own)."""
    os.environ["OKENGINE_WRITE_ACTOR"] = ACTOR
    os.environ.setdefault("OKENGINE_CRON_JOBS", "/opt/data/cron-plus/jobs.json")
    os.environ.setdefault("OKENGINE_OUTPUT_CONTRACT_MODE", "enforce")
    mcp_dir = os.environ.get("OKENGINE_MCP_DIR", "/opt/hermes/okengine-mcp")
    sys.path[:0] = [mcp_dir, str(Path(mcp_dir).parent / "scripts" / "cron")]
    import write_server as W
    return W


def link_ratio(entities: dict[str, dict], live: list[dict], today: date) -> tuple[int, int]:
    """(recent live sources any entity cites, recent live sources) over RATIO_WINDOW_DAYS."""
    since = today - timedelta(days=RATIO_WINDOW_DAYS)
    recent = [s for s in live if s["date"] is not None and s["date"] >= since]
    cited = {_norm_ref(r) for ent in entities.values() for r in ent["refs"]}
    return sum(1 for s in recent if s["seat"] in cited or (s["id"] and s["id"] in cited)), \
        len(recent)


def append_snapshot(today: date, cited: int, recent: int) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    if not SNAPSHOT.exists():
        generated_page.write(SNAPSHOT, "---\ntype: dashboard\ntitle: Entity link snapshots\n---\n\n"
                             "| date | recent sources cited by an entity | recent sources | % |\n"
                             "|---|---|---|---|\n")
    pct = 100.0 * cited / recent if recent else 0.0
    with SNAPSHOT.open("a", encoding="utf-8") as fh:
        fh.write(f"| {today.isoformat()} | {cited} | {recent} | {pct:.1f} |\n")


def _today() -> date:
    """The deployment's LOCAL calendar day: the snapshot row is a human ledger key, and kb-health
    aggregates it with the other nightly ledgers (invariant-audit B6.2 — never UTC)."""
    override = os.environ.get("ENTITY_LINK_NOW", "").strip()
    if override:
        return datetime.strptime(override, "%Y-%m-%d").date()
    today = datetime.now()
    return today.date()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backfill", action="store_true", help="scan the whole corpus (dry run)")
    ap.add_argument("--apply", action="store_true", help="with --backfill: actually write")
    ap.add_argument("--dry-run", action="store_true", help="classify, write nothing")
    ap.add_argument("--limit", type=int, default=LIMIT)
    args = ap.parse_args(argv)
    write = not args.dry_run and (args.apply or not args.backfill)
    if not WIKI.is_dir():
        print('{"wakeAgent": false}')
        return 0
    today = _today()
    entities = load_entities()
    index, outcomes = name_index(entities)
    sources = load_sources(None if args.backfill else today - timedelta(days=WINDOW_DAYS))
    hits, common = match(sources, index)
    outcomes.update(common)
    all_live = load_sources(None) if not args.backfill else sources
    live = {s["seat"] for s in all_live}
    by_stem: dict[str, list[str]] = defaultdict(list)
    for seat in sorted(live):
        by_stem[seat.rsplit("/", 1)[-1]].append(seat)
    W = _write_server() if write else None
    written = 0
    for rel in sorted(entities):
        refs, added, repaired, stale = plan(entities[rel], hits.get(rel, []), live, by_stem)
        outcomes["stale-ref-unrepairable"] += stale
        if refs is None:
            continue
        if written >= args.limit:
            outcomes["not-examined:run-limit"] += 1
            continue
        if W is None:
            outcomes["would-link"] += added
            outcomes["would-repair"] += repaired
            outcomes["entities-would-update"] += 1
            entities[rel]["refs"] = refs
            continue
        written += 1
        result = W._update(rel, {"sources": refs}).split("\n", 1)[0]
        if result.startswith(("rejected", "refused", "error")):
            outcomes["rejected:" + result[:90]] += 1
            continue
        entities[rel]["refs"] = refs
        outcomes["linked"] += added
        outcomes["repaired"] += repaired
        outcomes["entities-updated"] += 1
    cited, recent = link_ratio(entities, all_live, today)
    if W is not None:
        append_snapshot(today, cited, recent)
    mode = "backfill" if args.backfill else f"last {WINDOW_DAYS}d"
    print(f"# entity-link — {len(sources)} sources ({mode}), {len(entities)} entities, "
          f"{len(index)} usable names{' (dry run)' if W is None else ''}\n")
    for outcome, n in sorted(outcomes.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"- {outcome}: {n}")
    if W is None:
        print("\n## most-linked entities (review for generic names before --apply)")
        for line in top_names(index, hits):
            print(f"- {line}")
    pct = 100.0 * cited / recent if recent else 0.0
    print(f"- recent sources cited by an entity ({RATIO_WINDOW_DAYS}d): {cited}/{recent} "
          f"({pct:.1f}%){' — projected' if W is None else ''}")
    print(json.dumps({"wakeAgent": False, "sources": len(sources), **dict(outcomes),
                      "recent_cited": cited, "recent": recent}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
