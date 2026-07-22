#!/usr/bin/env python3
"""corpus_audit.py — deterministic corpus-INTEGRITY audit: vocabulary drift, dead fields,
and prediction feedback-loop engagement.

The corpus is a test oracle the repo gates never consult: code can be individually correct
while the ACCUMULATED data drifts (agent-authored enum values wander, a consumed field is
never populated). This lane measures two such classes continuously, so they are dashboard
rows instead of rediscoveries in the next capability review (the graduation rule):

  1. ENUM DRIFT — for every field the governing schema binds to a vocabulary
     (``field_enums``), count corpus values OUTSIDE the allowed set, with an example page.
     Also audits the nested ``evidence[].direction`` vocabulary (the measured D1 class:
     ~18 drifted entries were silently mis-bucketed by the cockpit tally). If NO governing
     schema declares ``field_enums``, the section reports **undetectable** — never a
     vacuous pass (the missing-key = WARN rule).
  2. DEAD FIELDS — fields ENGINE lanes consume that only an OPTIONAL producer populates:
     zero population over a nonempty candidate namespace means the consumer is silently
     degraded (the D6 class: ``signal_class`` shipped with a consumer and no producer).
  3. PREDICTION LOOP — evidence coverage, terminal ungraded waste, and filing-time
     measurement-method coverage. These are ratios over the live corpus, not fixture behavior:
     a prediction lane can be implemented while barely touching its book.
  0. OFF-TAXONOMY TYPES — pages whose ``type`` is outside the governing schema (base ∪ pack).
     ``strict_types`` defaults OFF, so a pack that has not opted in cannot enforce its taxonomy at the
     write path; STIX-style names (``threat-actor_group``) slip in and fragment entities.
  0b. ENTITY FRAGMENTATION — entity pages that share a normalized name/alias, i.e. one entity
     split across near-duplicate pages that entity resolution never converged.
  4. BODY INTEGRITY — malformed ``## ##`` headings and reader-derived backlink/reference
     panels authored into canonical prose.
  5. FIELD COVERAGE — for each (type, field) the governing schema lists in ``coverage_fields``,
     the fraction of that type's pages populating the field. A sparse field (a KEV backlog whose
     ``cvss_base`` was never backfilled) becomes a standing row with an optional ``min`` alert
     floor, not a per-review rediscovery. Engine-agnostic: the pack names the fields. If NO
     governing schema declares ``coverage_fields``, the section reports **undetectable**.

Sub-domain aware (okengine#177/#178): pages validate against their GOVERNING schema
(walk-up), so a multipack vault audits each sub-domain against its own vocabularies.

Pure ``no_agent`` script — the numbers ARE the deliverable; emits ``wakeAgent=false``
always. Idempotent (rewrites the dashboard wholesale each run). Tolerates pages vanishing
mid-scan (glob-then-read race with mover lanes).

Env: WIKI_PATH (default /opt/vault) · CORPUS_AUDIT_MAX_EXAMPLES (3)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
DASH_DIR = WIKI / "dashboards"
MAX_EXAMPLES = int(os.environ.get("CORPUS_AUDIT_MAX_EXAMPLES", "3"))
# okengine#237: drift on a page created/updated within this window = an ACTIVE producer
# regression, not legacy data. Surfaced as the `recent` column + a headline alert.
RECENT_DAYS = int(os.environ.get("CORPUS_AUDIT_RECENT_DAYS", "7"))
MAX_ENTITY_SLUG_LEN = 80
# Alias-fragmentation detector (the Gentlemen / Storm-2697 repro: one actor split across six
# actor pages sharing the alias "The Gentlemen"). Identity tokens shorter than this are too
# generic to cluster on (avoids merging distinct actors on a shared short token like "apt").
MIN_IDENTITY_LEN = int(os.environ.get("CORPUS_AUDIT_MIN_IDENTITY_LEN", "5"))
MAX_CLUSTERS = int(os.environ.get("CORPUS_AUDIT_MAX_CLUSTERS", "20"))
_IDENTITY_NORM_RE = re.compile(r"[^a-z0-9]+")

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_MALFORMED_H2_RE = re.compile(r"^##[ \t]+##(?:[ \t]+|$)", re.MULTILINE)
_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_LEAKED_FRONTMATTER_RE = re.compile(r"\A\s*---[^\s-]")
DERIVED_PANEL_HEADINGS = {
    "incoming backlinks",
    "outbound references",
    "referenced by",
    "references",
}


def _body_integrity_counts(body: str) -> tuple[int, int]:
    """Count malformed/derived H2s outside fenced code examples."""
    malformed = derived = 0
    fence: tuple[str, int] | None = None
    for line in body.splitlines():
        marker = _FENCE_RE.match(line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        if _MALFORMED_H2_RE.match(line):
            malformed += 1
        heading = _H2_RE.match(line)
        if heading and heading.group(1).strip().casefold() in DERIVED_PANEL_HEADINGS:
            derived += 1
    return malformed, derived

def _norm_identity(s) -> str:
    """Normalize a name/alias to a comparable identity token: casefold, punctuation -> space,
    collapse, drop a leading 'the '. So 'The Gentlemen', 'Gentlemen', and 'the-gentlemen' all
    map to 'gentlemen' — the join key that reveals one actor fragmented across pages."""
    t = _IDENTITY_NORM_RE.sub(" ", str(s or "").casefold()).strip()
    if t.startswith("the "):
        t = t[4:].strip()
    return t


# Namespaces that are operational output, not corpus — never audited.
SKIP_PARTS = {"dashboards", "operational", "_archived", ".okengine", ".backlinks"}

# DEAD-FIELD registry (defect class D6, okengine#221): field -> (candidate namespace,
# consumer description). A field here is READ by an engine lane/UI but populated only by an
# OPTIONAL producer — zero population over a nonempty candidate namespace = the consumer is
# silently degraded. Keep this list in sync when adding engine consumers of optional fields.
CONSUMED_FIELDS: dict[str, tuple[str, str]] = {
    "signal_class": ("sources", "source_portfolio_watch (falls back to source_kind)"),
    "evidence": ("predictions", "cockpit trajectory sparkline + reinforces/contradicts tally"),
    # okengine#326 [21]: the reader's "Recent reporting" panel reads recent_news_refs off entity
    # pages (okengine-reader/app.py), but no lane produces it — a consumer with no producer. Register
    # it so the dead-field detector reports it if it's referenced without ever being populated.
    "recent_news_refs": ("entities", "reader Recent-reporting panel (okengine-reader/app.py)"),
}

# Sanctioned nested evidence[].direction vocabulary (matches the regrade digest in
# okengine.predictions/select_regrade_batch.py). HARDCODED until nested item contracts land
# at the write path (okengine#211/#217) — then read from the governing schema's item
# declaration and delete this constant.
EVIDENCE_DIRECTION_ENUM = {"reinforces", "contradicts", "partial", "neutral"}
EVIDENCE_DIRECTION_KEY = "evidence[].direction"
PREDICTION_TERMINAL = {"confirmed", "refuted", "partial", "expired-ungraded", "resolved", "expired"}
PREDICTION_UNGRADED = {"expired-ungraded", "expired"}


def _skip(rel: Path) -> bool:
    name = rel.name.lower()
    return (bool(SKIP_PARTS.intersection(rel.parts)) or name in {
        "bundle.md", "hot.md", "health.md", "index.md", "log.md", "readme.md", "agents.md"
    } or rel.name.upper().startswith("INDEX-") or rel.name.startswith(("_", ".")))


def _is_recent(fm: dict) -> bool:
    """Page created/updated within RECENT_DAYS (tolerant of str/date/datetime stamps)."""
    cutoff = date.today() - timedelta(days=RECENT_DAYS)
    for f in ("created", "last_updated", "updated"):
        v = fm.get(f)
        s = v.isoformat() if hasattr(v, "isoformat") else (v if isinstance(v, str) else "")
        if s[:10] >= cutoff.isoformat():
            return True if s else False
    return False


def _enum_rules(schema: dict) -> dict[str, tuple[set, bool]]:
    """field -> (allowed_values, extensible), resolved with the WRITE-PATH's semantics
    (tools/schema_validator._enum_reject_reason): a rule is ``{enum: <name>, extensible: bool}``
    referencing ``schema['enums'][<name>]`` (base ∪ pack, merged by schema_lib). A bare list is
    accepted as a direct allowed-list (strict). ``extensible: true`` fields are LEGAL to extend
    at write time — out-of-enum values there are reported as NOVEL vocabulary, not drift."""
    enums = schema.get("enums") or {}
    out: dict[str, tuple[set, bool]] = {}
    for field, rule in (schema.get("field_enums") or {}).items():
        if isinstance(rule, list):
            out[field] = ({str(v) for v in rule}, False)
        elif isinstance(rule, dict):
            allowed = enums.get(rule.get("enum"))
            if isinstance(allowed, list):
                out[field] = ({str(v) for v in allowed}, bool(rule.get("extensible")))
    return out


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


# A frontmatter scalar that LOOKS like a bare wiki-relative page path (namespace/…/slug): lowercase
# slug segments joined by '/', no spaces, no URL scheme (no ':'), not a [[wikilink]] (no '['). Used
# to flag references a move/reshard left dangling — the assessment-`subject:` class (#336).
_PATHREF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9._-]*)+$")


def _iter_pathrefs(fm: dict):
    """Yield (field, target) for every frontmatter scalar (or list item) that looks like a bare
    wiki-relative page path. Wikilinks carry '[' and URLs carry ':', so the regex excludes both."""
    for field, val in fm.items():
        for v in (val if isinstance(val, list) else [val]):
            if isinstance(v, str):
                target = v.strip().removesuffix(".md")
                if _PATHREF_RE.match(target):
                    yield str(field), target


def _frontmatter(path: Path) -> dict | None:
    """Parse frontmatter; None on read race, no frontmatter, or YAML error (counted upstream)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None  # vanished mid-scan (mover-lane race) — skip, don't crash
    m = _FM_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}  # parse error — distinct from "no frontmatter"
    return fm if isinstance(fm, dict) else {}


def audit(vault: Path) -> dict:
    """Walk the corpus once; return the measured state (pure, testable)."""
    wiki = vault / "wiki"
    # drift/novel: [field][bad_value] -> {"count": n, "examples": [rel, ...]}
    _bucket = lambda: defaultdict(lambda: {"count": 0, "recent": 0, "examples": []})  # noqa: E731
    drift: dict = defaultdict(_bucket)   # strict enums — the write path would reject these now
    novel: dict = defaultdict(_bucket)   # extensible enums — legal, but silent growth = pre-drift
    populated: dict = {f: 0 for f in CONSUMED_FIELDS}
    candidates: dict = {f: 0 for f in CONSUMED_FIELDS}
    schema_cache: dict = {}
    rules_cache: dict = {}
    cov_cache: dict = {}     # govdir -> [(type, field, min_ratio), ...] from schema.coverage_fields
    # (type, field) -> {"total", "have", "min"} — schema-declared field-population coverage (#264)
    coverage: dict = defaultdict(lambda: {"total": 0, "have": 0, "min": None})
    coverage_declared = False
    types_cache: dict = {}   # govdir -> set of valid type names (base ∪ pack types + type_aliases)
    # type value -> occurrences of a page whose `type` is outside the governing taxonomy
    off_taxonomy: dict = defaultdict(lambda: {"count": 0, "recent": 0, "examples": []})
    # alias-fragmentation: normalized identity token -> set of entity rels claiming it, and
    # per-entity metadata for the cluster report.
    identity_index: dict[str, set] = defaultdict(set)
    entity_meta: dict[str, dict] = {}
    enums_declared = False
    pages = parse_errors = 0
    prediction_loop = {
        "total": 0,
        "with_evidence": 0,
        "terminal": 0,
        "terminal_ungraded": 0,
        "open_primary": 0,
        "open_primary_missing_measurement_method": 0,
    }
    source_signatures: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    review_ages: list[int] = []
    review_total = review_substantive = 0
    malformed_slugs: list[str] = []
    malformed_slug_count = 0
    body_integrity = {
        "malformed_heading_occurrences": 0,
        "malformed_heading_pages": 0,
        "malformed_heading_examples": [],
        "derived_panel_occurrences": 0,
        "derived_panel_pages": 0,
        "derived_panel_examples": [],
        "leaked_frontmatter_pages": 0,
        "leaked_frontmatter_examples": [],
    }

    existing_paths: set[str] = set()   # every real page (no .md), for dangling-ref resolution
    path_refs: list[tuple[str, str, str]] = []   # (field, target, source_rel) — bare path refs

    for p in sorted(wiki.rglob("*.md")):
        rel = p.relative_to(wiki)
        existing_paths.add(rel.as_posix()[:-3])   # BEFORE _skip: a skipped page is still a valid target
        if _skip(rel):
            continue
        fm = _frontmatter(p)
        if fm is None:
            continue
        pages += 1
        if fm == {}:
            parse_errors += 1
            continue
        for _field, _target in _iter_pathrefs(fm):
            path_refs.append((_field, _target, str(rel)))
        try:
            page_text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            page_text = ""
        body = _FM_RE.sub("", page_text, count=1)
        if _LEAKED_FRONTMATTER_RE.match(body):
            body_integrity["leaked_frontmatter_pages"] += 1
            if len(body_integrity["leaked_frontmatter_examples"]) < MAX_EXAMPLES:
                body_integrity["leaked_frontmatter_examples"].append(str(rel))
        malformed_n, derived_n = _body_integrity_counts(body)
        if malformed_n:
            body_integrity["malformed_heading_occurrences"] += malformed_n
            body_integrity["malformed_heading_pages"] += 1
            if len(body_integrity["malformed_heading_examples"]) < MAX_EXAMPLES:
                body_integrity["malformed_heading_examples"].append(str(rel))
        if derived_n:
            body_integrity["derived_panel_occurrences"] += derived_n
            body_integrity["derived_panel_pages"] += 1
            if len(body_integrity["derived_panel_examples"]) < MAX_EXAMPLES:
                body_integrity["derived_panel_examples"].append(str(rel))

        parts = rel.parts
        if parts and parts[0] == "sources" and len(parts) in (4, 5):
            title = str(fm.get("title") or fm.get("name") or "").strip().casefold()
            publisher = str(fm.get("publisher") or "").strip().casefold()
            published = str(fm.get("published") or "")[:10]
            if title and publisher and published:
                source_signatures[(title, publisher, published)].append(str(rel))

        if fm.get("needs_review") is True:
            review_total += 1
            if len(body.strip()) > 200:
                review_substantive += 1
            stamp = fm.get("last_updated") or fm.get("updated") or fm.get("created")
            s = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp or "")
            try:
                review_ages.append(max(0, (date.today() - date.fromisoformat(s[:10])).days))
            except (TypeError, ValueError):
                pass

        if rel.parts and rel.parts[0] == "entities" and (
            any(ch.isspace() for ch in p.stem) or len(p.stem) > MAX_ENTITY_SLUG_LEN
        ):
            malformed_slug_count += 1
            if len(malformed_slugs) < MAX_EXAMPLES:
                malformed_slugs.append(str(rel))

        ns = str(rel.parent) if rel.parent != Path(".") else ""
        govdir = schema_lib._governing_dir(vault, ns)
        if govdir not in schema_cache:
            schema_cache[govdir] = schema_lib.merged_schema(vault, ns)
            rules_cache[govdir] = _enum_rules(schema_cache[govdir])
            cov_cache[govdir] = _coverage_specs(schema_cache[govdir])
            sch = schema_cache[govdir]
            types_cache[govdir] = set(sch.get("types") or {}) | set(sch.get("type_aliases") or {})
        rules = rules_cache[govdir]
        if rules:
            enums_declared = True

        # PRODUCER-REGRESSION signal (okengine#237): a drifted value on a RECENTLY created/
        # updated page means a producer is minting drift NOW (importers bypass the write path)
        # — not legacy data awaiting backfill. Rendered as its own column + a headline alert.
        page_recent = _is_recent(fm)

        # A tombstoned page is intentionally superseded (a dedup/merge loser pointing at its
        # canonical) — it is the RESOLUTION of these two defects, not an instance. Counting it
        # would mean tombstoning never clears the signal (a merged dup keeps its old alias/type).
        tombstoned = str(fm.get("status") or "").strip().lower() == "tombstoned"

        # 0. type OUTSIDE the governing taxonomy. strict_types defaults OFF, so a permissive pack
        # does not enforce its own type taxonomy at the write path — STIX-style names (e.g.
        # `threat-actor_group`, `threat_actor_family`) slip in and fragment an entity across
        # near-duplicate types. Base ∪ pack types (+ type_aliases) are the sanctioned set.
        ptype = str(fm.get("type") or "").strip()
        valid_types = types_cache.get(govdir) or set()
        if not tombstoned and ptype and valid_types and ptype not in valid_types:
            rec = off_taxonomy[ptype]
            rec["count"] += 1
            rec["recent"] += 1 if page_recent else 0
            if len(rec["examples"]) < MAX_EXAMPLES:
                rec["examples"].append(str(rel))

        # 0c. schema-declared field coverage (okengine#264): the population ratio of each (type,
        # field) the governing schema lists in `coverage_fields`. A sparsely-populated field (a KEV
        # backlog whose cvss_base never got backfilled) is a standing dashboard row here instead of a
        # per-review rediscovery. Engine-agnostic — the pack names the fields; tombstones excluded.
        cov_specs = cov_cache.get(govdir) or []
        if cov_specs:
            coverage_declared = True
            if not tombstoned and ptype:
                for ct, cf, mn in cov_specs:
                    if ct != ptype:
                        continue
                    rec = coverage[(ct, cf)]
                    rec["total"] += 1
                    rec["min"] = mn
                    if fm.get(cf) not in (None, "", [], {}):
                        rec["have"] += 1

        # 0b. entity identity tokens (name/title/aliases) -> the alias-fragmentation index. An
        # exact normalized alias shared by >1 entity page is the strong signal that entity
        # resolution / canonical-assemble failed to converge them (the Gentlemen repro).
        if not tombstoned and rel.parts and rel.parts[0] == "entities":
            keys = set()
            for src in (fm.get("name"), fm.get("title")):
                k = _norm_identity(src)
                if len(k) >= MIN_IDENTITY_LEN:
                    keys.add(k)
            aliases = fm.get("aliases")
            if isinstance(aliases, list):
                for a in aliases:
                    k = _norm_identity(a)
                    if len(k) >= MIN_IDENTITY_LEN:
                        keys.add(k)
            if keys:
                entity_meta[str(rel)] = {"type": ptype, "keys": keys}
                for k in keys:
                    identity_index[k].add(str(rel))

        # 1a. top-level vocabulary check against the governing schema's field_enums
        for field, (allowed, extensible) in rules.items():
            val = fm.get(field)
            if not isinstance(val, str) or val in allowed:
                continue
            rec = (novel if extensible else drift)[field][val]
            rec["count"] += 1
            rec["recent"] += 1 if page_recent else 0
            if len(rec["examples"]) < MAX_EXAMPLES:
                rec["examples"].append(str(rel))

        # 1b. nested evidence[].direction (hardcoded until #211 — see constant docstring)
        ev = fm.get("evidence")
        if isinstance(ev, list):
            for item in ev:
                if not isinstance(item, dict):
                    continue
                d = item.get("direction")
                if isinstance(d, str) and d not in EVIDENCE_DIRECTION_ENUM:
                    rec = drift[EVIDENCE_DIRECTION_KEY][d]
                    rec["count"] += 1
                    rec["recent"] += 1 if page_recent else 0
                    if len(rec["examples"]) < MAX_EXAMPLES:
                        rec["examples"].append(str(rel))

        # 1c. prediction feedback-loop engagement. Keep the detector generic: it reads the
        # engine's core prediction envelope and treats measurement_method as an optional maturity
        # signal, never a conformance requirement.
        if str(fm.get("type") or "").strip() == "prediction":
            prediction_loop["total"] += 1
            if isinstance(ev, list) and ev:
                prediction_loop["with_evidence"] += 1
            status = str(fm.get("status") or "").strip().lower()
            if status in PREDICTION_TERMINAL:
                prediction_loop["terminal"] += 1
                if status in PREDICTION_UNGRADED:
                    prediction_loop["terminal_ungraded"] += 1
            primary = any(k in fm for k in ("made_on", "horizon", "resolves_by"))
            if status == "open" and primary:
                prediction_loop["open_primary"] += 1
                if not str(fm.get("measurement_method") or "").strip():
                    prediction_loop["open_primary_missing_measurement_method"] += 1

        # 2. dead-field population over candidate namespaces
        top = rel.parts[0] if rel.parts else ""
        sub = rel.parts[1] if len(rel.parts) > 2 else ""  # walk-up subdomain: <sub>/<ns>/page
        for field, (cand_ns, _consumer) in CONSUMED_FIELDS.items():
            if top == cand_ns or sub == cand_ns:
                candidates[field] += 1
                v = fm.get(field)
                if v not in (None, "", [], {}):
                    populated[field] += 1

    # Per-shared-key clustering (NOT transitive union-find): each normalized alias/name claimed by
    # >1 entity page is one cluster. Transitive merging over-connects — a single page listing many
    # aliases bridges genuinely distinct actors into a blob (OilRig+APT41+Kimsuky) and destroys the
    # signal. An EXACT shared normalized alias is the high-precision "same entity" join. Clusters
    # with the identical member set (page shares both name and an alias) are merged, keys unioned.
    by_members: dict[tuple, dict] = {}
    fragmentation = []
    for k, rels in sorted(identity_index.items()):
        if len(rels) < 2:
            continue
        members = tuple(sorted(rels))
        if members in by_members:
            by_members[members]["shared"].append(k)
            continue
        entry = {
            "members": list(members),
            "shared": [k],
            "types": sorted({entity_meta[m]["type"] for m in members if entity_meta[m]["type"]}),
        }
        by_members[members] = entry
        fragmentation.append(entry)
    for e in fragmentation:
        e["shared"] = sorted(e["shared"])
    fragmentation.sort(key=lambda c: (-len(c["members"]), c["members"][0]))

    # DANGLING PATH REFERENCES (#336) — bare frontmatter paths whose target no longer exists: a
    # move/reshard that never rewrote the reference (the assessment `subject:` join break). Scoped to
    # real top-level namespaces so an arbitrary slashed string isn't misread as a page reference; a
    # target that names a shard/dir (a prefix of some page) resolves too.
    namespaces = {pp.split("/", 1)[0] for pp in existing_paths}
    existing_dirs: set[str] = set()
    for pp in existing_paths:
        parts = pp.split("/")
        for i in range(1, len(parts)):
            existing_dirs.add("/".join(parts[:i]))
    dangling: dict = defaultdict(lambda: {"count": 0, "examples": []})
    for field, target, src in path_refs:
        if target.split("/", 1)[0] not in namespaces:
            continue                                   # not a wiki page namespace
        if target in existing_paths or target in existing_dirs:
            continue                                   # resolves to a page or a shard/dir
        rec = dangling[field]
        rec["count"] += 1
        if len(rec["examples"]) < MAX_EXAMPLES:
            rec["examples"].append(f"{src} → {target}")

    return {
        "pages": pages,
        "parse_errors": parse_errors,
        "dangling_refs": {f: dict(r) for f, r in dangling.items()},
        "off_taxonomy": {t: dict(rec) for t, rec in off_taxonomy.items()},
        "fragmentation": fragmentation,
        "drift": {f: dict(vals) for f, vals in drift.items()},
        "novel": {f: dict(vals) for f, vals in novel.items()},
        "populated": populated,
        "candidates": candidates,
        "enums_declared": enums_declared,
        "coverage_declared": coverage_declared,
        "coverage": {f"{t}.{f}": {"total": rec["total"], "have": rec["have"], "min": rec["min"],
                                  "ratio": (rec["have"] / rec["total"] if rec["total"] else 0.0)}
                     for (t, f), rec in sorted(coverage.items())},
        "prediction_loop": prediction_loop,
        "source_partition_collisions": [
            sorted(paths, key=lambda path: (len(Path(path).parts), path))
            for paths in source_signatures.values()
            if len(paths) > 1
            and any(len(Path(path).parts) == 4 for path in paths)
            and any(len(Path(path).parts) == 5 for path in paths)
        ],
        "review_queue": {
            "total": review_total,
            "substantive": review_substantive,
            "fraction": (review_total / pages) if pages else 0.0,
            "median_age_days": median(review_ages) if review_ages else None,
        },
        "malformed_slugs": {
            "count": malformed_slug_count,
            "examples": malformed_slugs,
        },
        "body_integrity": body_integrity,
    }


def render(state: dict, today: str) -> str:
    out = [
        "# Corpus integrity audit",
        "",
        f"Generated {today} by `corpus_audit.py` (no_agent). "
        f"{state['pages']} pages scanned, {state['parse_errors']} frontmatter parse errors.",
        "",
        "## Vocabulary drift (values outside the governing schema's enums)",
        "",
    ]
    drift = state["drift"]
    if not state["enums_declared"]:
        out += [
            "**UNDETECTABLE** — no governing schema declares resolvable `field_enums`, so "
            "top-level vocabulary drift cannot be measured on this vault (this is a WARN, "
            "not a pass). The nested `evidence[].direction` check still ran.",
            "",
        ]
    elif not drift:
        out += ["None — every audited value is inside its declared vocabulary.", ""]
    if drift:
        hot = sum(rec.get("recent", 0) for vals in drift.values() for rec in vals.values())
        if hot:
            out += [f"**⚠ ACTIVE PRODUCER REGRESSION: {hot} drifted value(s) on pages "
                    f"created/updated within {RECENT_DAYS}d** — a lane is minting drift now "
                    f"(importers bypass the write path, okengine#237); find and fix the "
                    f"producer before backfilling.", ""]
        out += ["| Field | Out-of-enum value | Count | Recent(≤" + str(RECENT_DAYS) + "d) | Example pages |",
                "|---|---|---|---|---|"]
        for field in sorted(drift):
            for val, rec in sorted(drift[field].items(), key=lambda kv: -kv[1]["count"]):
                ex = ", ".join(f"`{e}`" for e in rec["examples"])
                out.append(f"| `{field}` | `{val}` | {rec['count']} | {rec.get('recent', 0)} | {ex} |")
        out.append("")
    novel = state["novel"]
    if novel:
        out += [
            "## Novel values on extensible vocabularies (legal — but silent growth is pre-drift)",
            "",
            "| Field | Novel value | Count | Example pages |",
            "|---|---|---|---|",
        ]
        for field in sorted(novel):
            for val, rec in sorted(novel[field].items(), key=lambda kv: -kv[1]["count"]):
                ex = ", ".join(f"`{e}`" for e in rec["examples"])
                out.append(f"| `{field}` | `{val}` | {rec['count']} | {ex} |")
        out.append("")
    out += ["## Dead fields (engine consumers of optional producers — okengine#221 class)", ""]
    out += ["| Field | Consumer | Candidates | Populated | Verdict |", "|---|---|---|---|---|"]
    for field, (cand_ns, consumer) in CONSUMED_FIELDS.items():
        cand, pop = state["candidates"][field], state["populated"][field]
        if cand == 0:
            verdict = "n/a (no candidate pages)"
        elif pop == 0:
            verdict = "**DEGRADED — consumer runs on fallback; producer missing?**"
        else:
            verdict = "OK"
        out.append(f"| `{field}` | {consumer} | {cand} (`{cand_ns}/`) | {pop} | {verdict} |")
    out += ["", "## Field coverage (schema-declared population ratios — okengine#264)", ""]
    cov = state.get("coverage") or {}
    if not state.get("coverage_declared"):
        out += ["**UNDETECTABLE** — no governing schema declares `coverage_fields`, so "
                "field-population coverage isn't tracked on this vault (a WARN, not a pass).", ""]
    elif not cov:
        out += ["Declared, but no pages of the declared type(s) exist yet.", ""]
    else:
        out += ["| Type.field | Populated | Total | Coverage | Floor | Verdict |",
                "|---|---:|---:|---:|---:|---|"]
        for name, r in cov.items():
            total, have = int(r.get("total") or 0), int(r.get("have") or 0)
            ratio, mn = float(r.get("ratio") or 0.0), r.get("min")
            floor = f"{mn:.0%}" if isinstance(mn, (int, float)) else "—"
            if isinstance(mn, (int, float)) and ratio < mn:
                verdict = f"**BELOW FLOOR — {total - have} page(s) missing the field**"
            else:
                verdict = "OK (complete)" if have == total else "OK"
            out.append(f"| `{name}` | {have} | {total} | {ratio:.1%} | {floor} | {verdict} |")
        out.append("")
    loop = state.get("prediction_loop") or {}
    total = int(loop.get("total") or 0)
    with_evidence = int(loop.get("with_evidence") or 0)
    terminal = int(loop.get("terminal") or 0)
    ungraded = int(loop.get("terminal_ungraded") or 0)
    open_primary = int(loop.get("open_primary") or 0)
    missing_method = int(loop.get("open_primary_missing_measurement_method") or 0)
    pct = lambda n, d: f"{100.0 * n / d:.1f}%" if d else "n/a"  # noqa: E731
    out += [
        "",
        "## Flat-vs-sharded source collisions",
        "",
    ]
    collisions = state.get("source_partition_collisions") or []
    out.append(
        f"**{len(collisions)}** exact article identity collision(s) across monthly and daily paths."
    )
    for paths in collisions[:MAX_EXAMPLES]:
        out.append("- " + " ↔ ".join(f"`{path}`" for path in paths))
    out += [
        "",
        "## Human-review queue health",
        "",
    ]
    review = state.get("review_queue") or {}
    rq_total = int(review.get("total") or 0)
    rq_substantive = int(review.get("substantive") or 0)
    rq_fraction = float(review.get("fraction") or 0.0)
    rq_age = review.get("median_age_days")
    out += [
        "| Flagged | Corpus fraction | Substantive (>200 chars) | Median age |",
        "|---:|---:|---:|---:|",
        f"| {rq_total} | {rq_fraction:.1%} | {rq_substantive} | "
        f"{str(rq_age) + 'd' if rq_age is not None else 'n/a'} |",
        "",
    ]
    malformed = state.get("malformed_slugs") or {}
    out += [
        "## Malformed page basenames",
        "",
        f"**{int(malformed.get('count') or 0)}** entity page(s) have whitespace or exceed "
        f"{MAX_ENTITY_SLUG_LEN} characters.",
    ]
    examples = malformed.get("examples") or []
    if examples:
        out += ["", *[f"- `{example}`" for example in examples]]
    integrity = state.get("body_integrity") or {}
    malformed_pages = int(integrity.get("malformed_heading_pages") or 0)
    malformed_occurrences = int(integrity.get("malformed_heading_occurrences") or 0)
    panel_pages = int(integrity.get("derived_panel_pages") or 0)
    panel_occurrences = int(integrity.get("derived_panel_occurrences") or 0)
    leaked_pages = int(integrity.get("leaked_frontmatter_pages") or 0)
    out += [
        "",
        "## Body integrity",
        "",
        "| Defect | Pages | Occurrences | Example pages |",
        "|---|---:|---:|---|",
        f"| Malformed `## ##` headings | {malformed_pages} | {malformed_occurrences} | "
        + ", ".join(f"`{p}`" for p in integrity.get("malformed_heading_examples", [])) + " |",
        f"| Reader-derived panels authored as body H2 | {panel_pages} | {panel_occurrences} | "
        + ", ".join(f"`{p}`" for p in integrity.get("derived_panel_examples", [])) + " |",
        f"| Front-matter fragment leaked into body | {leaked_pages} | {leaked_pages} | "
        + ", ".join(f"`{p}`" for p in integrity.get("leaked_frontmatter_examples", [])) + " |",
    ]
    out += [
        "",
        "## Prediction feedback-loop engagement",
        "",
        "| Metric | Numerator | Denominator | Rate |",
        "|---|---:|---:|---:|",
        f"| Predictions carrying evidence | {with_evidence} | {total} | {pct(with_evidence, total)} |",
        f"| Terminal predictions left ungraded | {ungraded} | {terminal} | {pct(ungraded, terminal)} |",
        f"| Open primary predictions missing `measurement_method` | {missing_method} | "
        f"{open_primary} | {pct(missing_method, open_primary)} |",
    ]
    out += [
        "",
        "## Entity types outside the schema taxonomy",
        "",
    ]
    off = state.get("off_taxonomy") or {}
    if not off:
        out.append("**0** — every page's `type` is within its governing schema taxonomy.")
    else:
        n = sum(int(r.get("count") or 0) for r in off.values())
        out.append(
            f"**{n}** page(s) across **{len(off)}** type value(s) declare a `type` NOT in the "
            "governing schema (base ∪ pack + type_aliases). `strict_types` is OFF for this "
            "governing pack, so these bypass the validator and fragment entities across near-duplicate types:")
        out += ["", "| Type value | Pages | Recent | Examples |", "|---|---:|---:|---|"]
        rows = sorted(off.items(), key=lambda kv: -int(kv[1].get("count") or 0))
        for t, r in rows[:MAX_CLUSTERS]:
            # a run-on `type` (whole frontmatter collapsed into it) is itself a defect; truncate
            # for the table so one broken page can't blow up the report width.
            disp = t if len(t) <= 60 else t[:57] + "…"
            out.append(
                f"| `{disp}` | {int(r.get('count') or 0)} | {int(r.get('recent') or 0)} | "
                + ", ".join(f"`{e}`" for e in r.get("examples", [])) + " |")
        if len(rows) > MAX_CLUSTERS:
            out.append(f"| … {len(rows) - MAX_CLUSTERS} more type value(s) | | | |")
    out += [
        "",
        "## Same-entity fragmentation (shared-alias clusters)",
        "",
    ]
    frag = state.get("fragmentation") or []
    if not frag:
        out.append("**0** — no two entity pages share a normalized name/alias token.")
    else:
        out.append(
            f"**{len(frag)}** cluster(s) of entity pages sharing a normalized name/alias — likely "
            "one entity split across pages (entity resolution / canonical-assemble did not "
            "converge them). Consolidate onto a single canonical page:")
        for c in frag[:MAX_CLUSTERS]:
            aliases = ", ".join(f"`{k}`" for k in c.get("shared", [])) or "—"
            types = ", ".join(f"`{t}`" for t in c.get("types", [])) or "—"
            out += ["", f"- **{len(c['members'])} pages** sharing {aliases} (types: {types}):"]
            out += [f"    - `{m}`" for m in c["members"]]
    out += ["", "## Dangling path references (frontmatter path → a page that no longer exists)", ""]
    dangling = state.get("dangling_refs") or {}
    if not dangling:
        out += ["None — every bare `field: namespace/…` frontmatter path resolves to a page or "
                "shard.", ""]
    else:
        tot = sum(r["count"] for r in dangling.values())
        out += [
            f"**⚠ {tot} dangling reference(s)** — a page was moved/resharded without rewriting "
            "inbound BARE-path references (the assessment `subject:` join break, okengine#336). The "
            "consumer that joins on this field silently drops the row. Repoint the field, and ensure "
            "the mover ran the frontmatter-aware rewriter (`okf_migrate.make_path_rewriter`).",
            "",
            "| Field | Count | Examples (source → missing target) |",
            "|---|---|---|",
        ]
        for field in sorted(dangling, key=lambda f: -dangling[f]["count"]):
            r = dangling[field]
            out.append(f"| `{field}` | {r['count']} | {'; '.join(r['examples']) or '—'} |")
        out.append("")
    out += [
        "",
        "---",
        "*Drift here means agent-authored values wandered outside the sanctioned vocabulary; "
        "consumers may silently mis-bucket them. Enforcement at the write path is the fix "
        "(okengine#211/#217); this dashboard is the standing detector.*",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    if not WIKI.is_dir():
        print(f"corpus-audit | no wiki/ under {VAULT} — nothing to audit")
        print(json.dumps({"wakeAgent": False}))   # JSON sentinel, not bare string (audit HIGH #8)
        return 0
    state = audit(VAULT)
    DASH_DIR.mkdir(parents=True, exist_ok=True)
    (DASH_DIR / "corpus-audit.md").write_text(
        render(state, date.today().isoformat()), encoding="utf-8"
    )
    n_drift = sum(rec["count"] for vals in state["drift"].values() for rec in vals.values())
    n_novel = sum(rec["count"] for vals in state["novel"].values() for rec in vals.values())
    dead = [
        f for f, (ns, _) in CONSUMED_FIELDS.items()
        if state["candidates"][f] > 0 and state["populated"][f] == 0
    ]
    off_pages = sum(int(r.get("count") or 0) for r in (state.get("off_taxonomy") or {}).values())
    frag = state.get("fragmentation") or []
    print(
        f"corpus-audit | {state['pages']} pages | {n_drift} drifted values across "
        f"{len(state['drift'])} fields | {n_novel} novel extensible values | "
        f"dead fields: {', '.join(dead) or 'none'} | "
        f"off-taxonomy: {off_pages} page(s)/{len(state.get('off_taxonomy') or {})} type(s) | "
        f"fragmentation: {len(frag)} cluster(s) | "
        f"body defects: {state['body_integrity']['malformed_heading_pages']} malformed-heading "
        f"page(s), {state['body_integrity']['derived_panel_pages']} derived-panel page(s), "
        f"{state['body_integrity']['leaked_frontmatter_pages']} leaked-frontmatter page(s) | "
        f"{state['parse_errors']} parse errors"
    )
    print(json.dumps({"wakeAgent": False}))   # JSON sentinel, not bare string (audit HIGH #8)
    return 0


if __name__ == "__main__":
    sys.exit(main())
