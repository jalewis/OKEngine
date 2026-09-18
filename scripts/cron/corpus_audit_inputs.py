"""Input parsing and raw-capture analysis for the corpus integrity audit."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

import id_lib


def _slug_identity(stem: str) -> str:
    """A slug reduced to what a HUMAN reads as the same name: letters and digits only.

    `agent-tesla` and `agenttesla` are one actor written two ways, and the corpus held both
    (with `inc-ransom`/`incransom`, `apt-29-cozy-bear`/`apt29-cozy-bear`, `winnti-group`/
    `winnti_group`, and 20 more). Nothing detected it, because every partition check compares
    slugs for EQUALITY -- and these are not equal, so each half is correctly filed at its own
    canonical seat. It is not a misfiling; it is two subjects where there is one.

    An all-digit result is discarded: separators are the whole meaning in a numeric identifier,
    so `110-37-3-251` and `110-37-32-51` are different addresses, not one written two ways.
    """
    return id_lib.slug_identity(stem)


def _skip(rel: Path, skip_parts: set[str]) -> bool:
    name = rel.name.lower()
    return (bool(skip_parts.intersection(rel.parts)) or name in {
        "bundle.md", "hot.md", "health.md", "index.md", "log.md", "readme.md", "agents.md"
    } or rel.name.upper().startswith("INDEX-") or rel.name.startswith(("_", ".")))


def _is_recent(fm: dict, recent_days: int) -> bool:
    """Page created/updated within recent_days (tolerant of str/date/datetime stamps)."""
    cutoff = date.today() - timedelta(days=recent_days)
    for f in ("created", "last_updated", "updated"):
        v = fm.get(f)
        s = v.isoformat() if hasattr(v, "isoformat") else (v if isinstance(v, str) else "")
        if s[:10] >= cutoff.isoformat():
            return True if s else False
    return False


def _coverage_specs(schema: dict) -> list[tuple[str, str, float | None]]:
    """(type, field, min_ratio|None) from the governing schema's optional ``coverage_fields`` — a
    pack declares which (type, field) POPULATION ratios to track continuously (okengine#264). The
    engine stays domain-agnostic: it measures the ratio; the pack names the fields (e.g. a vuln pack
    tracks cve.cvss_base coverage so the KEV-backlog sparsity that band-aided the CVSS column is a
    standing dashboard row, not a rediscovery). ``min`` (0..1) is an optional alert floor."""
    out: list[tuple[str, str, float | None]] = []
    for spec in (schema.get("coverage_fields") or []):
        if not isinstance(spec, dict):
            continue
        t, f = str(spec.get("type") or "").strip(), str(spec.get("field") or "").strip()
        if not t or not f:
            continue
        try:
            mn = float(spec["min"]) if spec.get("min") is not None else None
        except (TypeError, ValueError):
            mn = None
        out.append((t, f, mn))
    return out


def _typed_shape_rules(schema: dict, shape_checks: dict) -> dict:
    """{(type, field): shape} from the by_type form of `field_shapes`. The legacy plain-string form
    (`aliases: list`) is global and stays with its existing consumers, so this is additive."""
    out = {}
    for field, rule in ((schema or {}).get("field_shapes") or {}).items():
        by = rule.get("by_type") if isinstance(rule, dict) else None
        for typ, shape in (by or {}).items():
            if str(shape) in shape_checks:
                out[(str(typ), str(field))] = str(shape)
    return out


def _iter_pathrefs(fm: dict, non_ref_fields: frozenset[str], pathref_re: Any):
    """Yield (field, target) for every frontmatter scalar (or list item) that looks like a bare
    wiki-relative page path. Wikilinks carry '[' and URLs carry ':', so the regex excludes both.
    Identity/storage fields are skipped — see non_ref_fields."""
    for field, val in fm.items():
        if str(field) in non_ref_fields:
            continue
        for v in (val if isinstance(val, list) else [val]):
            if isinstance(v, str):
                target = v.strip().removesuffix(".md")
                if pathref_re.match(target):
                    yield str(field), target


def _frontmatter(path: Path, fm_re: Any) -> dict | None:
    """Parse frontmatter; None on read race, no frontmatter, or YAML error (counted upstream)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None  # vanished mid-scan (mover-lane race) — skip, don't crash
    m = fm_re.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}  # parse error — distinct from "no frontmatter"
    return fm if isinstance(fm, dict) else {}


def _sources_enum_rules(vault: Path, enum_rules: Callable[..., Any], schema_module: Any) -> dict | None:
    """The `sources` enum contract, or None when this vault cannot resolve one."""
    try:
        return enum_rules(schema_module.merged_schema(vault, "sources")) or None
    except Exception:
        return None


def raw_capture_health(
    vault: Path,
    rules: dict | None = None,
    *,
    frontmatter: Callable[[Path], dict | None],
    provenance: Any,
    novel: str,
    max_examples: int,
) -> dict:
    """Duplicate raw captures, captured URLs never promoted, and the vocabulary the ingest minted.

    The raw tree is the ONE place a count can inflate without anything noticing: it is append-only,
    nothing dedupes it, and every downstream number derived from "raw files" inherits the error.
    Observed on one deployment: 14,425 captures of 3,996 distinct URLs — 10,429 redundant files, with
    a single URL captured 555 times. A "12,358 unpromoted files" backlog computed from file counts
    was almost entirely re-captures; the honest backlog was 1,516 distinct URLs.

    So both halves are reported: `duplicate_captures` is wasted work and a corrupted denominator,
    and `unpromoted_urls` is the real ingest backlog. Counting FILES conflates them, which is
    precisely how the backlog was misread.

    THE VOCABULARY HALF (okengine#594). The wiki-side drift check above is the LAST place an
    undeclared value can be caught, and by then it may not be catchable at all: provenance is
    carried from raw to wiki, so an ingest lane minting its own vocabulary either lands a value
    the write path rejects, or — the case that actually happened — the compile model, handed a
    value it cannot use, invents its own and the raw record's truth never reaches the page.
    Three ingest lanes on two deployments had minted `cyber-news` 4,520 times against schemas
    declaring `news`, and nothing looked at `raw/` to say so.

    An ingest lane does not go through the write path, so the schema's vocabulary is not
    enforced on it anywhere. This is the check that notices, at the boundary where the value is
    minted rather than three hops downstream. `rules` is the resolved enum contract for the
    `sources` namespace (raw captures are source records); passing None skips the check rather
    than reporting a vacuous clean.
    """
    def _nurl(value) -> str:
        u = str(value or "").strip().rstrip("/").casefold()
        return re.sub(r"^https?://(www\.)?", "", u)

    page_urls: set[str] = set()
    base = vault / "wiki" / "sources"
    for path in base.rglob("*.md") if base.is_dir() else []:
        fm = frontmatter(path)
        if isinstance(fm, dict) and fm.get("url"):
            page_urls.add(_nurl(fm["url"]))

    counts: dict[str, int] = defaultdict(int)
    raw_base = vault / "raw"
    unparseable = 0
    # No `extensible` default: it is assigned from the verdict on the same pass that creates the
    # record, so a default here is dead — it could only ever be read after being overwritten.
    minted: dict = defaultdict(lambda: defaultdict(lambda: {"count": 0, "lanes": set()}))
    for path in raw_base.rglob("*.md") if raw_base.is_dir() else []:
        fm = frontmatter(path)
        if not isinstance(fm, dict) or not fm:
            unparseable += 1
            continue
        if fm.get("url"):
            counts[_nurl(fm["url"])] += 1
        for field in rules or {}:
            verdict = provenance.classify_value(field, fm.get(field), rules)
            if verdict is None:
                continue
            rec = minted[field][fm[field]]
            rec["count"] += 1
            rec["extensible"] = verdict == novel
            # WHICH lane minted it — the only detail that makes the finding actionable. A value
            # named without its writer sends the reader looking through every ingest lane.
            lane = fm.get("watch_lane") or fm.get("source_channel")
            if isinstance(lane, str) and lane and len(rec["lanes"]) < max_examples:
                rec["lanes"].add(lane)

    unpromoted = sorted(u for u in counts if u not in page_urls)
    worst = [{"url": u, "captures": n}
             for u, n in sorted(counts.items(), key=lambda kv: -kv[1])[:5] if n > 1]
    return {
        "captures": sum(counts.values()),
        "distinct_urls": len(counts),
        "duplicate_captures": sum(counts.values()) - len(counts),
        "urls_captured_more_than_once": sum(1 for n in counts.values() if n > 1),
        "worst_duplicates": worst,
        "unpromoted_urls": len(unpromoted),
        "unpromoted_examples": unpromoted[:max_examples],
        "unparseable_captures": unparseable,
        "vocabulary_checked": rules is not None,
        "minted_vocabulary": {
            field: {value: {"count": rec["count"], "extensible": rec["extensible"],
                            "lanes": sorted(rec["lanes"])}
                    for value, rec in sorted(values.items(), key=lambda kv: -kv[1]["count"])}
            for field, values in sorted(minted.items())
        },
    }
