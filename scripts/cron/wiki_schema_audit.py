#!/usr/bin/env python3
"""Schema drift audit for the wiki — counts type: distribution, flags drift.

Two modes of drift:

  1. Schema emergence (bottom-up) — pages using a `type:` value that is not
     canonical. Cluster of N>=5 with the same unsanctioned value is a
     canonization candidate (vault is signaling a missing schema slot).
     Singleton/low-count drift is typically a typo or one-off.

  2. Structural conformance gap — pages with a canonical type that lack the
     required frontmatter fields for that type (e.g. a `type: alpha` page
     missing a field the pack's schema marks `required:` for `alpha`). These
     are migration queue items: schema was extended, existing pages weren't
     updated.

Outputs a markdown report on stdout for the wiki-health-audit agent to
include in `lint-YYYY-MM-DD-vN.md`.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))


def load_canonical_types() -> dict[str, list[str]]:
    """Canonical types + their required-field lists, read from the pack's
    governing schema.yaml `types:` (the engine ships NO taxonomy of its own).

    Maps canonical type -> list of frontmatter keys that MUST be present
    (the type's `required:` list; empty list = allowed, no type-specific
    required fields). Returns {} if the pack declared no `types:` — callers
    treat that as 'every type is acceptable' (no drift / no conformance gaps),
    never as a built-in domain taxonomy."""
    schema = schema_lib.merged_schema(VAULT)
    types = schema.get("types")
    if not isinstance(types, dict):
        return {}
    out: dict[str, list[str]] = {}
    for name, spec in types.items():
        req = (spec or {}).get("required") if isinstance(spec, dict) else None
        out[str(name)] = [str(f) for f in req] if isinstance(req, (list, tuple)) else []
    return out


def load_operational_types() -> set[str]:
    """Operational types — frontmatter present, but not knowledge pages
    (dashboards, lint reports, overviews, generated artifacts). Read from the
    schema's `operational_types:` list if the pack declares one; otherwise
    empty (no operational suppression). Not flagged as drift."""
    schema = schema_lib.merged_schema(VAULT)
    ops = schema.get("operational_types")
    return {str(x) for x in ops} if isinstance(ops, (list, tuple, set)) else set()


# Resolved once at import time from the MERGED schema (engine base ⊕ pack ⊕ enabled extensions,
# okengine#133) — NOT the raw pack schema. Reading the pack-only schema flagged engine-owned base
# types (dashboard/prediction/source/concept…) as DRIFT on every norm-following vault (the skeleton
# tells packs NOT to re-declare base types), advising the operator to canonize a type the engine
# already owns. Empty only when nothing declares a taxonomy → distribution-only, no drift judgments.
CANONICAL_TYPES: dict[str, list[str]] = load_canonical_types()
OPERATIONAL_TYPES: set[str] = load_operational_types()
# alias -> canonical (e.g. threat-actor -> actor); resolve before the drift check so a STIX/legacy
# alias name isn't miscounted as an unsanctioned type.
TYPE_ALIASES: dict[str, str] = schema_lib.type_aliases(schema_lib.merged_schema(VAULT))

# Threshold: an unsanctioned type appearing on >= this many pages is a
# canonization candidate (vault is telling us the schema needs this slot).
CANONIZATION_THRESHOLD = 5

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.S)
TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.M)
# Cat-n line-number prefix: e.g. "     1|---" — happens when someone pastes
# `cat -n file.md` output into a Write call. Repaired by stripping the prefix
# per line; the audit just needs to detect and surface it.
CAT_N_PREFIX_RE = re.compile(r"^\s*\d+\|")


def parse_frontmatter(text: str) -> str | None:
    m = FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def categorize_fm_failure(text: str) -> str:
    """Categorize WHY parse_frontmatter returned None for this text.
    Categories: empty | cat-n-prefix | unclosed-fm | no-fm-block | other.
    Order matters — most specific first."""
    if not text.strip():
        return "empty"
    # Cat-n damage: at least one line has the line-number prefix
    if any(CAT_N_PREFIX_RE.match(line) for line in text.split("\n")):
        return "cat-n-prefix"
    # If the file doesn't start with `---`, there's no FM block intended at all
    # — this catches lint reports, logs, drafts, etc. that legitimately have no
    # FM but might use `---` as a horizontal rule mid-body.
    if not text.lstrip().startswith("---"):
        return "no-fm-block"
    # Starts with `---` but only one delimiter line found — FM opened, never closed
    delim_count = sum(1 for line in text.split("\n") if line.rstrip() == "---")
    if delim_count < 2:
        return "unclosed-fm"
    return "other"


def yaml_validity(fm_text: str) -> str | None:
    """Attempt yaml.safe_load on a FM block already extracted by the regex.
    Returns a one-line error string on failure, or None on success.

    Why this matters: the regex parse_frontmatter only checks `---`
    delimiters. A FM block can pass the regex (correct delimiters) but
    still fail real YAML parsing — typically when the `sources:` field
    has a JSON-style array with unquoted [[wikilinks]] or mid-line
    truncation. Such pages are silently invisible to every downstream
    that uses yaml.safe_load (the static dataview renderer, dashboard
    wake-gates, etc.) — Obsidian's Dataview parser has the same blind
    spot, so a page can be silently dropped from a derived view because
    of a single unquoted wikilink in its sources array."""
    try:
        import yaml
    except ImportError:
        return None  # Skip silently if PyYAML missing in caller env
    try:
        yaml.safe_load(fm_text)
        return None
    except yaml.YAMLError as e:
        # First line of the error is the most actionable summary
        return str(e).split("\n")[0][:120]


# Top-level wiki/ files that legitimately don't have frontmatter — operational
# pages / outputs from other cron jobs. Suppressed from "no-fm-block" tally.
# Match against PosixPath.name and PosixPath.parts (relative to wiki/).
_OPERATIONAL_NO_FM = {
    "index.md", "log.md", "README.md",
    # OKF-reserved / generated structural files (frontmatter-free by design).
    "AGENTS.md", "INDEX.md", "BUNDLE.md", "HEALTH.md",
}
_OPERATIONAL_NO_FM_PREFIXES = ("lint-", "log-")


def is_operational_no_fm(path: Path, wiki_dir: Path) -> bool:
    """Return True for operational files that are expected to lack FM
    (index, log, lint reports, log archives)."""
    rel = path.relative_to(wiki_dir)
    # Only top-level files qualify — entity/concept/source/prediction pages
    # under subdirs always need FM.
    if len(rel.parts) > 1:
        return False
    name = rel.name
    if name in _OPERATIONAL_NO_FM:
        return True
    if any(name.startswith(p) for p in _OPERATIONAL_NO_FM_PREFIXES):
        return True
    return False


def has_field(fm: str, field: str) -> bool:
    return re.search(rf"^{re.escape(field)}:", fm, re.M) is not None


def scan_wiki(wiki_dir: Path):
    """Returns (type_counts, type_files, conformance_gaps, fm_failures).

    conformance_gaps: dict[type] -> list[(path, [missing_fields])] for canonical
    types only.
    fm_failures: dict[category] -> list[Path] for pages whose frontmatter
    couldn't be parsed (empty | cat-n-prefix | unclosed-fm | no-fm-block | other).
    These pages are invisible to the schema audit — surface them prominently.
    """
    type_counts: Counter[str] = Counter()
    type_files: dict[str, list[Path]] = defaultdict(list)
    conformance_gaps: dict[str, list[tuple[Path, list[str]]]] = defaultdict(list)
    fm_failures: dict[str, list[Path]] = defaultdict(list)

    for path in sorted(wiki_dir.rglob("*.md")):
        if not path.is_file():
            continue
        # Skip backup / archive directories (e.g. wiki/entities.bak.YYYYMMDD-HHMMSS/)
        # so an in-tree snapshot doesn't pollute the live audit.
        if any(".bak." in part or part.startswith(".") for part in path.parts):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        if fm is None:
            category = categorize_fm_failure(text)
            # Suppress "no-fm-block" reports for known operational files
            # (index.md, log.md, lint-*.md, log-*.md). Real corruption
            # (cat-n-prefix, unclosed-fm) is still reported on those paths.
            if category == "no-fm-block" and is_operational_no_fm(path, wiki_dir):
                continue
            fm_failures[category].append(path)
            continue
        # Regex parse succeeded — but does it parse as real YAML?
        # If not, the page is invisible to every downstream tool that
        # uses yaml.safe_load (Dataview renderer, dashboard wake-gates,
        # the page catalog, etc.). Surface as its own category.
        yaml_err = yaml_validity(fm)
        if yaml_err is not None:
            fm_failures["yaml-invalid"].append(path)
            # Continue — without valid YAML we can't extract `type:` reliably,
            # so this page also won't contribute to the type distribution.
            continue
        m = TYPE_RE.search(fm)
        if not m:
            continue
        t_raw = m.group(1).strip().strip("\"'")
        t = TYPE_ALIASES.get(t_raw, t_raw)     # resolve a STIX/legacy alias to its canonical type
        type_counts[t] += 1
        type_files[t].append(path)

        if t in CANONICAL_TYPES:
            missing = [f for f in CANONICAL_TYPES[t] if not has_field(fm, f)]
            if missing:
                conformance_gaps[t].append((path, missing))

    return type_counts, type_files, conformance_gaps, fm_failures


_FM_FAILURE_NOTES = {
    "empty": "0-byte file. Either delete (check inbound `[[wikilinks]]` first) or stub with minimal `type:` + 1-line description.",
    "cat-n-prefix": "Cat-n line-number prefix (`     1|---`) baked into file content. Cause: a prior session pasted `cat -n` output into a Write call. Repair: strip `^\\s*\\d+\\|` per line — recursive if the prefix was applied twice.",
    "unclosed-fm": "Frontmatter opened with `---` but never closed. Insert closing `---` before the body H1 (or at EOF if no body).",
    "no-fm-block": "No `---` delimiters at all. Likely a stray draft or incomplete page — add full frontmatter or remove.",
    "yaml-invalid": "Frontmatter delimiters present but YAML body fails `yaml.safe_load`. Page is invisible to every downstream that parses YAML (dashboards, wake-gates, the page catalog). Most common cause: `sources:` field with unquoted `[[wikilinks]]` in a JSON-style array, or mid-line truncation. Repair: rewrite the offending field as a multi-line YAML list (`sources:\\n  - \"[[sources/...]]\"`), or quote each wikilink in the inline array.",
    "other": "Frontmatter present but malformed in some other way. Inspect manually.",
}


def render_report(type_counts: Counter, type_files: dict, conformance_gaps: dict, fm_failures: dict) -> str:
    out: list[str] = []

    # --- Section 0: frontmatter health (before schema drift, since broken FM
    # pages are invisible to every other section of this audit)
    out.append("## Frontmatter health\n")
    total_failures = sum(len(v) for v in fm_failures.values())
    if total_failures == 0:
        out.append("No parse failures. Every wiki page has parseable YAML frontmatter.\n")
    else:
        out.append(
            f"**{total_failures} pages with unparseable frontmatter — invisible to "
            f"the schema audit and to wake-gates that filter by `type:` "
            f"(various ingest crons).**\n"
        )
        for category in ("empty", "cat-n-prefix", "unclosed-fm", "yaml-invalid", "no-fm-block", "other"):
            paths = fm_failures.get(category, [])
            if not paths:
                continue
            out.append(f"\n### {category} ({len(paths)})")
            out.append(_FM_FAILURE_NOTES.get(category, ""))
            out.append("")
            for p in paths:
                out.append(f"- `{p.relative_to(VAULT)}`")
        out.append("")

    out.append("## Schema drift audit\n")

    total_typed = sum(type_counts.values())
    out.append(f"Pages with `type:` frontmatter: **{total_typed}**\n")

    # No declared taxonomy (pack has no schema.yaml `types:`) means there is no
    # canonical set to measure drift / conformance against — degrade to a plain
    # distribution and skip the drift/conformance judgments rather than flag
    # every page as drift.
    have_taxonomy = bool(CANONICAL_TYPES)
    if not have_taxonomy:
        out.append(
            "_No `types:` declared in the governing schema.yaml — reporting the "
            "type distribution only; drift and conformance checks are skipped._\n"
        )

    # --- Section 1: type distribution
    out.append("### Type distribution\n")
    out.append("| Type | Count | Status |")
    out.append("|---|---:|---|")
    for t, n in sorted(type_counts.items(), key=lambda x: -x[1]):
        if not have_taxonomy:
            status = "present"
        elif t in CANONICAL_TYPES:
            status = "canonical"
        elif t in OPERATIONAL_TYPES:
            status = "operational"
        else:
            status = "**DRIFT**"
        out.append(f"| `{t}` | {n} | {status} |")
    out.append("")

    # --- Section 2: emergence (unsanctioned types)
    drift_types = {
        t: n
        for t, n in type_counts.items()
        if have_taxonomy and t not in CANONICAL_TYPES and t not in OPERATIONAL_TYPES
    }
    canonization_candidates = {t: n for t, n in drift_types.items() if n >= CANONIZATION_THRESHOLD}
    one_off_drift = {t: n for t, n in drift_types.items() if n < CANONIZATION_THRESHOLD}

    out.append("### Schema emergence (unsanctioned `type:` values)\n")
    if not drift_types:
        out.append("None.\n")
    else:
        if canonization_candidates:
            out.append(
                f"**Canonization candidates** (>= {CANONIZATION_THRESHOLD} pages — "
                "the vault is signaling a missing schema slot; decide: canonize, "
                "rename to existing canonical type, or migrate pages off):\n"
            )
            for t, n in sorted(canonization_candidates.items(), key=lambda x: -x[1]):
                out.append(f"- `type: {t}` — {n} pages")
                for p in type_files[t][:3]:
                    out.append(f"  - `{p.relative_to(VAULT)}`")
                if len(type_files[t]) > 3:
                    out.append(f"  - ... and {len(type_files[t]) - 3} more")
            out.append("")
        if one_off_drift:
            out.append(
                f"**One-off / low-count drift** (< {CANONIZATION_THRESHOLD} pages — "
                "likely typos or experimental tags; review and either fix the page "
                "or promote the type if intentional):\n"
            )
            for t, n in sorted(one_off_drift.items(), key=lambda x: -x[1]):
                paths = ", ".join(f"`{p.relative_to(VAULT)}`" for p in type_files[t])
                out.append(f"- `type: {t}` — {n} pages: {paths}")
            out.append("")

    # --- Section 3: structural conformance gaps
    out.append("### Structural conformance gaps (canonical type, missing required fields)\n")
    if not conformance_gaps:
        out.append("None.\n")
    else:
        out.append(
            "Migration queue: pages with a canonical `type:` that lack required "
            "type-specific frontmatter fields. Pages should be updated to satisfy "
            "the schema or have their type changed.\n"
        )
        for t, gaps in sorted(conformance_gaps.items(), key=lambda x: -len(x[1])):
            required = CANONICAL_TYPES[t]
            out.append(
                f"- **`type: {t}`** — {len(gaps)}/{type_counts[t]} pages missing "
                f"one or more of: {', '.join(f'`{f}`' for f in required)}"
            )
            # field-level rollup
            field_miss: Counter[str] = Counter()
            for _, missing in gaps:
                field_miss.update(missing)
            for f, n in field_miss.most_common():
                out.append(f"  - `{f}` missing on {n} pages")
            out.append("  - Sample pages:")
            for p, missing in gaps[:5]:
                out.append(
                    f"    - `{p.relative_to(VAULT)}` — missing: "
                    f"{', '.join(missing)}"
                )
            if len(gaps) > 5:
                out.append(f"    - ... and {len(gaps) - 5} more")
        out.append("")

    return "\n".join(out)


def _is_operational_no_fm_by_name(p: Path) -> bool:
    """Path-robust (no relative_to) operational check for the commit gate."""
    name = p.name
    return name in _OPERATIONAL_NO_FM or any(
        name.startswith(pre) for pre in _OPERATIONAL_NO_FM_PREFIXES
    )


def check_paths(paths) -> list[tuple[str, str]]:
    """Commit-gate: per-file HARD violations on staged pages.

    Flags only unparseable / broken frontmatter (empty, cat-n-prefix,
    unclosed-fm, no-fm-block on a content page, yaml-invalid) — pages that are
    invisible to every downstream tool. Deliberately does NOT flag conformance
    gaps (known migration backlog) or schema drift (a corpus-level signal that
    can't be judged from one staged file). Those stay report-only in the cron.
    """
    violations: list[tuple[str, str]] = []
    for raw in paths:
        p = Path(raw)
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        if fm is None:
            cat = categorize_fm_failure(text)
            if cat == "no-fm-block" and _is_operational_no_fm_by_name(p):
                continue
            violations.append((p.name, f"frontmatter unparseable: {cat}"))
            continue
        yaml_err = yaml_validity(fm)
        if yaml_err is not None:
            violations.append((p.name, f"yaml-invalid: {yaml_err}"))
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="wiki schema audit / commit gate")
    ap.add_argument(
        "--check", action="store_true",
        help="commit-gate mode: exit 1 on any unparseable-frontmatter violation",
    )
    ap.add_argument(
        "--paths", nargs="*", default=None,
        help="restrict to these staged files (gate mode)",
    )
    args = ap.parse_args(argv)

    if args.check:
        paths = args.paths or []
        violations = check_paths(paths)
        if violations:
            print(f"✗ {len(violations)} frontmatter-integrity violation(s) — "
                  f"fix before committing:", file=sys.stderr)
            for name, issue in violations:
                print(f"    {name}: {issue}", file=sys.stderr)
            return 1
        print("✓ frontmatter integrity OK", file=sys.stderr)
        return 0

    wiki_dir = VAULT / "wiki"
    if not wiki_dir.exists():
        print(f"# wiki-schema-audit: `{wiki_dir}` does not exist", file=sys.stderr)
        return 1
    type_counts, type_files, conformance_gaps, fm_failures = scan_wiki(wiki_dir)
    print(render_report(type_counts, type_files, conformance_gaps, fm_failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
