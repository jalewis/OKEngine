#!/usr/bin/env python3
"""conformance_audit.py — check EXISTING pages against current CONTENT rules (okengine#158 P1).

Schema conformance (required fields, types, namespaces) is covered by the validator +
`schema-drift-lint`. This is its companion for rules that live in PROMPTS/CONVENTIONS — the
ones that historically shipped with write-time enforcement only, so old pages silently drifted
(e.g. entity `sources:` written as prose like "Cisco Talos disclosure" instead of a source-page
path, which links nothing in the graph and starves prediction candidate-watch).

Rules come from the composed schema's top-level `conformance.rules` (engine `base-schema.yaml`
floor + pack additions) — ONE source of truth, so a rule can't be enforced-but-unaudited. P1
implements the `ref_fields` kind (list-field entries must be page-paths, not prose). The audit is
detection only: it writes wiki/dashboards/conformance.md (violations per rule + sample pages) and
points at each rule's remediation. Deterministic / no agent.

Env: WIKI_PATH (default /opt/vault) · CONFORMANCE_SAMPLES (samples per rule, default 25)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
DASH = WIKI / "dashboards" / "conformance.md"
SAMPLES = int(os.environ.get("CONFORMANCE_SAMPLES", "25"))
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)


def _fm(p: Path) -> dict:
    try:
        m = _FM_RE.match(p.read_text(encoding="utf-8", errors="replace")[:8000])
    except OSError:
        return {}
    if not m:
        return {}
    try:
        import yaml
        d = schema_lib.fast_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _skip(p: Path) -> bool:
    n = p.name
    return n.startswith(("_", ".")) or n == "INDEX.md" or n.startswith("INDEX-") or ".bak." in n


def _check_nonempty_fields(fm: dict, fields: list[str]) -> list[tuple[str, list[str]]]:
    """Return (field, [defect]) for each named field that is missing or empty.
    Presence semantics mirror the validator's `_present`: None, blank scalar,
    and empty list all count as empty (the capture lane writes `published:`
    with no value for dateless feed items — that must surface here)."""
    out = []
    for f in fields:
        if f not in fm:
            out.append((f, ["(missing)"]))
            continue
        v = fm[f]
        empty = (
            v is None
            or (isinstance(v, str) and not v.strip())
            or (isinstance(v, list) and not [e for e in v if e is not None and str(e).strip()])
        )
        if empty:
            out.append((f, ["(empty)"]))
    return out


def _check_ref_fields(fm: dict, fields: list[str]) -> list[tuple[str, list[str]]]:
    """Return (field, [prose entries]) for each named list-field carrying a non-page-ref entry."""
    out = []
    for f in fields:
        v = fm.get(f)
        if not isinstance(v, list):
            continue
        prose = [str(e) for e in v if e is not None and str(e).strip()
                 and not schema_lib.is_page_ref(e)]
        if prose:
            out.append((f, prose))
    return out


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    schema = schema_lib.compose_schema(VAULT)[0]   # base⊕pack — the conformance floor lives in base
    rules = schema_lib.conformance_rules(schema)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # accumulate per-rule: violating pages + sample rows
    findings: dict[str, dict] = {r["id"]: {"rule": r, "pages": 0, "entries": 0, "samples": []}
                                 for r in rules}
    checked = 0
    if rules:
        for p in WIKI.rglob("*.md"):
            if _skip(p):
                continue
            checked += 1
            fm = _fm(p)
            if not fm:
                continue
            rel = p.relative_to(WIKI).as_posix()[:-3]
            for r in rules:
                if r["kind"] == "ref_fields":
                    hits = _check_ref_fields(fm, r.get("fields") or [])
                elif r["kind"] == "nonempty_fields":
                    # optional per-rule type scope (e.g. only `source` pages)
                    if r.get("type") and str(fm.get("type") or "") != str(r["type"]):
                        continue
                    hits = _check_nonempty_fields(fm, r.get("fields") or [])
                else:
                    continue  # unknown kinds are forward-compatible no-ops
                if hits:
                    acc = findings[r["id"]]
                    acc["pages"] += 1
                    acc["entries"] += sum(len(pr) for _, pr in hits)
                    if len(acc["samples"]) < SAMPLES:
                        for fld, pr in hits:
                            acc["samples"].append((rel, fld, pr))

    # render dashboard
    summary = "  ·  ".join(f"{rid}: **{a['pages']}**" for rid, a in findings.items()) or "no rules"
    L = ["---", "type: dashboard", 'title: "Conformance audit"', f"updated: {now}", "---", "",
         f"# Conformance audit — {now}", "",
         "_Existing pages vs current CONTENT rules (okengine#158). Schema-field conformance lives in "
         "the schema-drift-lint dashboard; this covers rules beyond fields (e.g. refs must be "
         "page-paths, not prose). Detection only — see each rule's remediation._", "",
         f"- pages checked: **{checked}**  ·  {summary}", ""]
    for rid, a in findings.items():
        r = a["rule"]
        L += ["", f"## {rid}  ({r['kind']} · {r.get('severity', 'fix')})", "",
              f"_{r.get('remediation', '')}_", "",
              f"**{a['pages']}** page(s) · **{a['entries']}** non-conformant entr(ies)"]
        if a["samples"]:
            L += ["", "| Page | Field | Non-conformant entries |", "|---|---|---|"]
            for rel, fld, pr in a["samples"]:
                vals = ", ".join(s.replace("|", "\\|")[:60] for s in pr[:4])
                L.append(f"| [{rel}]({rel}.md) | {fld} | {vals} |")
    L.append("")
    DASH.parent.mkdir(parents=True, exist_ok=True)
    DASH.write_text("\n".join(L), encoding="utf-8")

    tally = ", ".join(f"{a['pages']} {rid}" for rid, a in findings.items()) or "no rules defined"
    print(f"conformance-audit: {checked} pages checked; {tally} -> wiki/dashboards/conformance.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
