"""scope_lib — shared scope-config + term-matching for the relevance-gate lanes (okengine#167).

The BOUNDARY is pack/operator config (`pack_config.scope` in the vault's schema.yaml — see the
issue: mechanism generic, scope domain-owned); this lib only reads and applies it:

    pack_config:
      scope:
        statement: <one sentence: what this vault tracks>
        in_scope:  [<phrases>]        # each phrase contributes keyword terms
        out_of_scope: [<phrases>]
        on_uncertain: keep            # the only supported value — err-toward-keep is the contract

Matching is deliberately DUMB and legible (the operator must be able to predict it): each scope
phrase is split into lowercase terms (stopwords dropped, parentheticals kept); a page's slug +
title + excerpt is scored by distinct in-terms vs out-terms hit. The gate NEVER deletes — it flags
`off_scope: true` + `scope_reason` (the same reversible marker the manual F2 pass established).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_STOP = {"the", "a", "an", "and", "or", "of", "for", "with", "that", "this", "your", "their",
         "any", "no", "not", "e", "g", "eg", "etc", "in", "on", "to", "as", "is", "are", "its"}
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def load_scope(vault: Path) -> dict | None:
    """The pack's scope config, or None (no scope declared -> lanes no-op LOUDLY, never guess)."""
    f = vault / "schema.yaml"
    if not f.is_file():
        return None
    try:
        d = yaml.safe_load(f.read_text(encoding="utf-8", errors="replace")) or {}
    except yaml.YAMLError:
        return None
    scope = (d.get("pack_config") or {}).get("scope")
    if not isinstance(scope, dict) or not (scope.get("in_scope") or scope.get("out_of_scope")):
        return None
    return scope


def _terms(phrases) -> set[str]:
    out: set[str] = set()
    for p in (phrases or []):
        for w in re.split(r"[^a-z0-9-]+", str(p).lower()):
            w = w.strip("-")
            if len(w) >= 3 and w not in _STOP:
                out.add(w)
    return out


def compile_scope(scope: dict) -> tuple[set[str], set[str]]:
    """(in_terms, out_terms). Terms appearing on BOTH sides are dropped from OUT (a term the
    operator lists as in-scope can never count against a page — err-toward-keep)."""
    in_t = _terms(scope.get("in_scope"))
    out_t = _terms(scope.get("out_of_scope")) - in_t
    return in_t, out_t


def page_blob(path: Path, excerpt_chars: int = 400) -> tuple[dict, str]:
    """(frontmatter, lowercase match-blob of slug+title+excerpt) for one page."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(4096)      # slug+title+excerpt only — never the whole page
    except OSError:
        return {}, ""
    m = _FM.match(text)
    fm = {}
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        if not isinstance(fm, dict):
            fm = {}
    body = text[m.end():] if m else text
    blob = f"{path.stem} {fm.get('title') or ''} {body[:excerpt_chars]}".lower()
    return fm, blob


def score(blob: str, in_terms: set[str], out_terms: set[str]) -> tuple[int, int, list[str]]:
    """(in_hits, out_hits, matched_out_terms) — distinct-term counts."""
    words = set(re.split(r"[^a-z0-9-]+", blob))
    ins = len(in_terms & words)
    outs = sorted(out_terms & words)
    return ins, len(outs), outs


def flag(path: Path, reason: str) -> bool:
    """Reversible off_scope flag (frontmatter-prepend, same marker as the F2 pass). True if
    written; False if already flagged / no frontmatter."""
    text = path.read_text(encoding="utf-8", errors="replace")
    m = _FM.match(text)
    if not m or "off_scope:" in m.group(1):
        return False
    ins = f"off_scope: true\nscope_reason: {reason}\n"
    path.write_text(text[:m.start(1)] + ins + text[m.start(1):], encoding="utf-8")
    return True
