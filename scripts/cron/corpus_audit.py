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
import okf_migrate  # noqa: E402  — the SINGLE source of canonical placement (okengine#54)
import provenance_lib  # noqa: E402  — the SINGLE source of the declared value vocabulary
import corpus_audit_inputs  # noqa: E402
import corpus_audit_domain  # noqa: E402
import corpus_audit_render  # noqa: E402
import engine_package  # noqa: E402  — makes `okengine` importable when this file is
engine_package.ensure()  # loaded BY PATH by an external consumer (okpacks-library#89)
from okengine.actor_identity import actor_identity_error  # noqa: E402
from provenance_lib import DRIFT, NOVEL, enum_rules as _enum_rules  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
DASH_DIR = WIKI / "dashboards"
MAX_EXAMPLES = int(os.environ.get("CORPUS_AUDIT_MAX_EXAMPLES", "3"))
# okengine#237: drift on a page created/updated within this window = an ACTIVE producer
# regression, not legacy data. Surfaced as the `recent` column + a headline alert.
RECENT_DAYS = int(os.environ.get("CORPUS_AUDIT_RECENT_DAYS", "7"))
MAX_ENTITY_SLUG_LEN = 80
# OVER-BROAD ALIAS (okengine#589). An alias is a MATCH TERM: importers and resolvers tag a page by
# finding it in prose, so a short common token tags everything. `AI` and `LLM` were minted as
# threat-actor aliases on this corpus, and `AI` matched 10.6% of all source pages.
#
# The floor is 4, not the 6 an existing consumer uses, because 6 is far too blunt here: 179 of 1,342
# actor pages carry an alias under 6 characters and nearly all are legitimate (`ALPHV`, `ZINC`,
# `Qilin`, `Turla`). A detector that reports 179 pages gets switched off, and then guards nothing.
# At 4 it reports 19, every one a real over-broad term (`AMD`, `SEA`, `UPS`, `CS`), and it still
# catches both `AI` and `LLM`.
MIN_ALIAS_LEN = int(os.environ.get("CORPUS_AUDIT_MIN_ALIAS", "4"))
# okengine#591. Ratio of unvalidated news matches to CITED sources above which an identity is
# matching far more than it is evidenced for. Measured on a live corpus: every genuine actor below
# 1.0, every generic noun at or above 2.0, the worst at 18.0. MIN_NEWS keeps a 1-news/0-source page
# out of it -- a brand-new page is thin, not over-matching, and reporting it would bury the signal.
OVERMATCH_RATIO = float(os.environ.get("CORPUS_AUDIT_OVERMATCH_RATIO", "3.0"))
OVERMATCH_MIN_NEWS = int(os.environ.get("CORPUS_AUDIT_OVERMATCH_MIN_NEWS", "3"))
# A catalogue identifier: a short prefix then a number (APT31, TA412, G0035, BE2). Short by nature
# and never an ordinary word, so the floor must not touch it. Deliberately GENERIC -- the engine
# ships no vendor's numbering scheme, only the SHAPE of "a prefix and a number".
_STRUCTURED_ID = re.compile(r"^[A-Za-z]{1,6}[-_ ]?\d{1,5}$")
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
# "identified in the <Name> dataset" and friends. Matched generically here; the captured <Name> is
# only reported if the corpus itself declares it as a `retrieved_via` CARRIER, so no vendor or
# repository name is ever hardcoded in the engine.
# "2 A-grade sources: …; 1 B-grade source: …" — the grades review_autoverify stamps as its basis.
_BASIS_GRADE_RE = re.compile(r"(\d+)\s+([A-F])-grade")
# A frontmatter KEY is an identifier, not prose. Two shapes are always wrong and neither depends on
# the schema being complete -- which matters, because the schema declares 223 field names while the
# corpus uses 1169, so "undeclared" alone is ~1000 findings that are mostly schema gaps (`url` on
# 25k pages). These two catch agent invention precisely: a key that is not an identifier at all
# (`slug"`, `-category`, `title State of API Exposure 2024 y`), and a key long enough to be a
# sentence (`confidence_numeric_approximately_zero_point_seven_five`).
_FIELD_KEY_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_-]*\Z")
MAX_FIELD_KEY_LEN = int(os.environ.get("CORPUS_AUDIT_MAX_FIELD_KEY_LEN", "48"))
# The frontmatter field naming a judgment's KIND. Pack vocabulary -- the engine ships no default
# name (guessing one would be domain knowledge in the engine layer). Unset => silence is measured
# per (subject, type) and the audit SAYS so, rather than reporting a coarse number as complete.
JUDGMENT_KIND_FIELD = os.environ.get("CORPUS_AUDIT_JUDGMENT_KIND_FIELD", "").strip()
_CARRIER_PROSE_RE = re.compile(
    r"(?:identified|described|reported|found|listed|catalogued)\s+in\s+the\s+"
    r"([A-Za-z][A-Za-z0-9 ._-]{1,40}?)\s*(?:dataset|database|data\s?set|feed)\b", re.I)
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

def _alias_list(value) -> list[str]:
    """Normalize `aliases` to a list of non-empty strings, however it was spelled.

    `aliases` is DECLARED `list` in the base schema, but a SCALAR lands on pages written outside the
    enforced write path -- 4 on the live vault. Both naive readings of a scalar are wrong, and this
    exists so neither happens again:

      - iterating it yields its CHARACTERS, turning `aliases: raw-alias` into seven one-letter
        aliases (which is exactly what a probe written for this issue did before it was caught);
      - skipping anything that is not a list -- what the fragmentation index below did -- makes
        those pages invisible to every alias check, so the page with the malformed field is the one
        least likely to be audited.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [s for s in (str(v).strip() for v in items if v is not None) if s]


def _overbroad_aliases(value) -> list[str]:
    """Aliases too short to be safe MATCH TERMS. Structured catalogue IDs and non-ASCII are exempt.

    Character count is a proxy for specificity only within one script: three CJK characters are a
    full name, not an acronym, so `狼毒草` must not be flagged for being "short".
    """
    return [a for a in _alias_list(value)
            if len(a) < MIN_ALIAS_LEN and a.isascii() and not _STRUCTURED_ID.match(a)]


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
    "local_only": ("sources", "local-evidence authority-record attestation"),
    "export_policy": ("sources", "local-evidence authority-record attestation"),
    "record_checksum": ("sources", "local-evidence authority-record attestation"),
    "bounded_auto_accept": ("sources", "local-evidence bounded auto-accept policy"),
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

# A frontmatter scalar that LOOKS like a bare wiki-relative page path (namespace/…/slug): lowercase
# slug segments joined by '/', no spaces, no URL scheme (no ':'), not a [[wikilink]] (no '['). Used
# to flag references a move/reshard left dangling — the assessment-`subject:` class (#336).
_PATHREF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9._-]*)+$")


# Fields whose values are path-SHAPED but are NOT graph edges, so a "dangling ref" finding on them
# is always false. `id` is an identity (path-style ids are common and resolve through the id-index,
# not the filesystem), `raw`/`path`/`raw_path` are storage locations, and `field_mapped`/`slug`-style
# keys are mapping metadata. Counting them buried the real signal: 496 of 568 reported dangling refs
# on one live vault were these — 87% noise, and `id` alone was 397 (okengine#563).
# Per-type SHAPE declarations: `field_shapes: {<field>: {by_type: {<type>: <shape>}}}`.
# A field can mean different things on different types -- `confidence` is a numeric probability on an
# assessment (804/804 on one live vault) and a qualitative band elsewhere -- so one global shape
# cannot fit. Nothing enforces this at the write path yet: the corpus is only clean for some types,
# and enforcing what the data violates is how a schema binding takes down 500 pages. Reported here
# first, so a type can be declared once its data actually complies (okengine#563).
_SHAPE_CHECKS = {
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "str": lambda v: isinstance(v, str),
    "list": lambda v: isinstance(v, list),
}



NON_REF_FIELDS = frozenset({
    "id", "raw", "path", "raw_path", "field_mapped", "canonical_key", "slug", "url_slug",
    "watch_lane",
})






_slug_identity = corpus_audit_inputs._slug_identity


def _skip(rel: Path) -> bool:
    return corpus_audit_inputs._skip(rel, SKIP_PARTS)


def _is_recent(fm: dict) -> bool:
    return corpus_audit_inputs._is_recent(fm, RECENT_DAYS)


_coverage_specs = corpus_audit_inputs._coverage_specs


def _typed_shape_rules(schema: dict) -> dict:
    return corpus_audit_inputs._typed_shape_rules(schema, _SHAPE_CHECKS)


def _iter_pathrefs(fm: dict):
    return corpus_audit_inputs._iter_pathrefs(fm, NON_REF_FIELDS, _PATHREF_RE)


def _frontmatter(path: Path) -> dict | None:
    return corpus_audit_inputs._frontmatter(path, _FM_RE)


def _sources_enum_rules(vault: Path) -> dict | None:
    return corpus_audit_inputs._sources_enum_rules(vault, _enum_rules, schema_lib)


def raw_capture_health(vault: Path, rules: dict | None = None) -> dict:
    return corpus_audit_inputs.raw_capture_health(
        vault,
        rules,
        frontmatter=_frontmatter,
        provenance=provenance_lib,
        novel=NOVEL,
        max_examples=MAX_EXAMPLES,
    )


def audit(vault: Path) -> dict:
    return corpus_audit_domain.audit(vault, corpus_audit_domain.AuditContext(
        consumed_fields=CONSUMED_FIELDS,
        evidence_direction_enum=EVIDENCE_DIRECTION_ENUM,
        evidence_direction_key=EVIDENCE_DIRECTION_KEY,
        judgment_kind_field=JUDGMENT_KIND_FIELD,
        max_entity_slug_len=MAX_ENTITY_SLUG_LEN,
        max_examples=MAX_EXAMPLES,
        max_field_key_len=MAX_FIELD_KEY_LEN,
        min_identity_len=MIN_IDENTITY_LEN,
        novel=NOVEL,
        overmatch_min_news=OVERMATCH_MIN_NEWS,
        overmatch_ratio=OVERMATCH_RATIO,
        prediction_terminal=PREDICTION_TERMINAL,
        prediction_ungraded=PREDICTION_UNGRADED,
        basis_grade_re=_BASIS_GRADE_RE,
        carrier_prose_re=_CARRIER_PROSE_RE,
        field_key_re=_FIELD_KEY_RE,
        frontmatter_re=_FM_RE,
        leaked_frontmatter_re=_LEAKED_FRONTMATTER_RE,
        shape_checks=_SHAPE_CHECKS,
        alias_list=_alias_list,
        actor_identity_error=actor_identity_error,
        body_integrity_counts=_body_integrity_counts,
        coverage_specs=_coverage_specs,
        enum_rules=_enum_rules,
        frontmatter=_frontmatter,
        is_recent=_is_recent,
        iter_pathrefs=_iter_pathrefs,
        norm_identity=_norm_identity,
        overbroad_aliases=_overbroad_aliases,
        skip=_skip,
        slug_identity=_slug_identity,
        sources_enum_rules=_sources_enum_rules,
        typed_shape_rules=_typed_shape_rules,
        okf_migrate=okf_migrate,
        provenance_lib=provenance_lib,
        raw_capture_health=raw_capture_health,
        schema_lib=schema_lib,
    ))

def render(state: dict, today: str) -> str:
    return corpus_audit_render.render(
        state,
        today,
        max_examples=MAX_EXAMPLES,
        recent_days=RECENT_DAYS,
        consumed_fields=CONSUMED_FIELDS,
        max_entity_slug_len=MAX_ENTITY_SLUG_LEN,
        min_alias_len=MIN_ALIAS_LEN,
        max_clusters=MAX_CLUSTERS,
    )

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
    _raw = state.get("raw_capture_health") or {}
    n_minted = sum(rec["count"] for vals in (_raw.get("minted_vocabulary") or {}).values()
                   for rec in vals.values())
    minted_note = (f"{n_minted} undeclared ingest value(s)" if _raw.get("vocabulary_checked")
                   else "ingest vocabulary UNDETECTABLE")
    print(
        f"corpus-audit | {state['pages']} pages | {n_drift} drifted values across "
        f"{len(state['drift'])} fields | {n_novel} novel extensible values | "
        f"{minted_note} | "
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
