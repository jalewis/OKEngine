#!/usr/bin/env python3
"""source_normalize — lift body-only citations into the `sources:` frontmatter (okengine#563).

Some pages carry their evidence as `[[sources/...]]` wikilinks in a `## Sources` body section and
have no `sources:` frontmatter key at all. That evidence is real and checkable, and it is
**invisible to every consumer**: `review_autoverify._grade_evidence`, the cockpit's `_provenance`
and the reader's all read `fm["sources"]` only. On one live CTI vault 405 pages were in this state —
citing named publishers while grading as unsourced and rendering as "no sources".

Nothing INSTRUCTS this shape: no lane in the engine or any pack emits a `## Sources` body section,
and the pack prompts already ask for the frontmatter field. It is emergent model behaviour — an
agent writes the natural markdown section and omits the field — which is precisely why it is
corrected here and enforced at the write path rather than argued for in a prompt.

The fix is deliberately NOT to teach the three consumers to also parse bodies. That would create a
second canonical location for the same fact across three separately-deployed surfaces, which is
precisely the drift that produced okengine#563 in the first place (a cockpit and a lane disagreeing
about what counts as grounded hid 861 pages). Normalise at the source instead: one location, and
every existing consumer is already correct.

Scope — a page is rewritten ONLY when all of these hold:
  * its type is neither `source` nor `dashboard`. A `source` record IS the primary document (its own
    url/raw is its grounding, and body links on it are references, not provenance). A `dashboard` is
    a GENERATED aggregate view: its body list of source records is a rendering, not a citation, and
    the next regeneration would overwrite a lifted field anyway.
    Everything else is in scope. The impact is not confined to `entities/`: on one live vault 40
    entity pages carried the defect, and another 365 non-entity pages did too — of which 325 showed
    a false "no sources" badge and **41 were stuck in `needs_review`**, held for a human because the
    grading lane could not see evidence the page actually cited;
  * it has NO `sources:` frontmatter key (pages that already have one are not broken: they grade and
    render. Merging into an existing list is a different, riskier edit and is out of scope here);
  * the body contains at least one `[[sources/...]]` link that RESOLVES to a real vault page.
    Dangling refs are never promoted — that would manufacture a citation to nothing.

Idempotent: once the key exists the page no longer matches, so a second run is a no-op.

Env: WIKI_PATH (vault root, default /opt/vault). `--dry-run` reports without writing.
Pure script (no_agent): always emits {"wakeAgent": false}.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
# a top-level `sources:` key, in either block or inline-list form
_HAS_SOURCES = re.compile(r"^sources:", re.M)
# `source`: the primary document itself — its url/raw is its grounding, and body links on it are
# references, not provenance. `dashboard`: a GENERATED aggregate view whose body list of source
# records is a rendering, not a citation (and a regen would overwrite a lifted field anyway).
_EXEMPT_TYPES = {"source", "dashboard"}
# STRUCTURAL pages are machinery, not knowledge: an append-only run log, a generated bundle, a
# health page. Lifting an operational log's body citations into `sources:` would manufacture a
# frontmatter list of every source the vault has ever touched, which is meaningless as provenance
# and unbounded in size. `corpus_audit._STRUCTURAL` already recognises this same set; this lane
# skipped `_`/`.`/INDEX/.bak but not `log`, and `log.md` is exactly where it wedged.
_STRUCTURAL_STEMS = {"INDEX", "index", "log", "BUNDLE", "HEALTH", "AGENTS", "_about"}


def _frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _resolves(ref: str) -> bool:
    """True when a `sources/...` ref names a real page. Mirrors the containment checks in
    review_autoverify._source_page: a citation is not a filesystem instruction, and a prose string
    with a slash must never be walked as a path (an overlong component can abort the whole lane)."""
    key = ref.strip().removesuffix(".md")
    if not key.startswith("sources/"):
        return False
    parts = Path(key).parts
    if (not parts or Path(key).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or any(len(part.encode("utf-8")) > 255 for part in parts)
            or len(key.encode("utf-8")) > 4096):
        return False
    try:
        return (WIKI / (key + ".md")).is_file()
    except OSError:
        return False


def body_citations(body: str) -> list[str]:
    """Resolving `sources/...` refs cited in the body, in first-appearance order, deduped.

    Dedupe through a SET, not `ref not in out` against the list. That membership test is O(n) per
    citation, so a page carrying tens of thousands of them costs O(n^2) -- and it also re-ran
    `_resolves` (a filesystem stat) for every repeat of a ref that does NOT resolve, since a
    non-resolving ref never lands in `out` to be found there.

    Measured: this lane wedged on a live vault's `log.md` (4.3 MB, tens of thousands of citations)
    and never returned. `seen` records every distinct ref examined, resolving or not, so each one
    costs exactly one stat and one hash lookup.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _WIKILINK.findall(body):
        ref = raw.strip().removesuffix(".md")
        if not ref.startswith("sources/") or ref in seen:
            continue
        seen.add(ref)
        if _resolves(ref):
            out.append(ref)
    return out


def _governing_schema() -> dict:
    """The composed artifact if present, else the pack schema — the same resolution the write path
    uses. Absent/unparsable yields {}, which disables ref repair rather than guessing."""
    for cand in (VAULT / ".okengine" / "composed-schema.yaml", VAULT / "schema.yaml"):
        try:
            if cand.is_file():
                return yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def declared_ref_fields(schema: dict) -> set[str]:
    """Fields the schema declares as page REFERENCES (`conformance.rules[kind=ref_fields].fields`).

    ONLY these are repaired. Many other fields are path-SHAPED without being edges -- `id` is an
    identity (rewriting it breaks the id-index and every link that resolves through it) and `raw`
    is a storage location. A blanket "repair anything that looks like a path" would have rewritten
    397 `id` and 53 `raw` values on one live vault.
    """
    out: set[str] = set()
    conf = schema.get("conformance")
    rules = conf.get("rules") if isinstance(conf, dict) else None
    for r in rules if isinstance(rules, list) else []:
        if isinstance(r, dict) and r.get("kind") == "ref_fields":
            out |= {str(f) for f in (r.get("fields") or [])}
    return out


_PAGE_INDEX: dict[str, list[str]] | None = None


def _page_index() -> dict[str, list[str]]:
    """{basename -> [vault-relative page keys]} for the WHOLE vault, built lazily."""
    global _PAGE_INDEX
    if _PAGE_INDEX is None:
        idx: dict[str, list[str]] = {}
        for q in WIKI.rglob("*.md"):
            idx.setdefault(q.stem, []).append(q.relative_to(WIKI).as_posix()[:-3])
        _PAGE_INDEX = idx
    return _PAGE_INDEX


def canonical_pathref(ref: str) -> str | None:
    """A resolvable form of a page reference, or None.

    The live failure: assessments referenced `entities/v/o/volt-typhoon` while the page lives at
    `entities/v/volt-typhoon`. The pack shards `entities` by first letter and RESHARDS to a second
    letter only once a bucket exceeds 500, so the writer assumed a depth only some buckets have.
    Resolution is therefore by basename WITHIN THE SAME NAMESPACE, and only when unique -- two
    pages sharing a basename cannot be told apart, and guessing would point the edge at the wrong
    page. (Measured: 2084 uniquely repairable, 0 ambiguous, 478 with no target at all.)
    """
    key = (ref or "").strip().removesuffix(".md")
    if not key or "/" not in key:
        return None
    try:
        if (WIKI / (key + ".md")).is_file():
            return key
    except OSError:
        return None
    ns = key.split("/", 1)[0]
    hits = [h for h in _page_index().get(key.rsplit("/", 1)[-1], []) if h.split("/", 1)[0] == ns]
    return hits[0] if len(hits) == 1 else None


_BASENAME_INDEX: dict[str, list[str]] | None = None


def _basename_index() -> dict[str, list[str]]:
    """{basename without .md -> [refs]} for every page under sources/. Built lazily and ONLY when an
    unresolvable ref is actually seen, so a clean vault never pays for it."""
    global _BASENAME_INDEX
    if _BASENAME_INDEX is None:
        idx: dict[str, list[str]] = {}
        for q in (WIKI / "sources").rglob("*.md"):
            idx.setdefault(q.stem, []).append(q.relative_to(WIKI).as_posix()[:-3])
        _BASENAME_INDEX = idx
    return _BASENAME_INDEX


def canonical_ref(ref: str) -> str | None:
    """A resolvable form of `ref`, or None. Repairs citations that name a real record in a shape
    nothing can resolve, so they grade as nothing (okengine#563).

    Domain-agnostic by construction — the shapes, not any repository name, are what is recognised:
      * already resolvable            -> unchanged
      * missing the `sources/` prefix -> prefixed
      * an ID form `a:b:<key>`        -> resolved by unique basename under sources/

    A basename match is used ONLY when it is UNIQUE: two records sharing a basename cannot be told
    apart, and picking one would silently attribute a page to the wrong record.
    """
    ref = (ref or "").strip().removesuffix(".md")
    if not ref:
        return None
    if _resolves(ref):
        return ref
    if not ref.startswith("sources/") and _resolves("sources/" + ref):
        return "sources/" + ref
    if ":" in ref:
        hits = _basename_index().get(ref.rsplit(":", 1)[-1].strip()) or []
        if len(hits) == 1:
            return hits[0]
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    fixed = skipped_dangling = repaired_refs = repaired_edges = 0
    ref_fields = declared_ref_fields(_governing_schema())
    for p in sorted(WIKI.rglob("*.md")):
        if (p.name.startswith(("_", ".")) or p.name.upper().startswith("INDEX")
                or ".bak" in p.name or p.stem in _STRUCTURAL_STEMS):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _FM_RE.match(text)
        if not match:
            continue
        head = match.group(1)
        # Repair DANGLING page references in schema-declared ref fields, whatever else this page
        # needs. A ref that names a real page in an unresolvable form is an edge the graph, the
        # reader and every consumer silently drop.
        if ref_fields:
            _fm = _frontmatter(text)
            _edges = []
            for _k in ref_fields:
                _v = (_fm or {}).get(_k)
                for _s in ([_v] if isinstance(_v, str) else (_v if isinstance(_v, list) else [])):
                    if not isinstance(_s, str):
                        continue
                    _cur = _s.strip().removesuffix(".md")
                    if not _cur or "/" not in _cur:
                        continue
                    try:
                        if (WIKI / (_cur + ".md")).is_file():
                            continue
                    except OSError:
                        continue
                    _new = canonical_pathref(_s)
                    if _new and _new != _cur:
                        _edges.append((_k, _s, _new))
            if _edges:
                _nh = head
                for _key, _old, _new in _edges:
                    # A ref appears either as a LIST item (`- value`) or a SCALAR (`key: value`);
                    # matching only the list form silently no-ops on every scalar ref.
                    # [ \t] not \s: a greedy \s*$ eats the line terminator and glues the closing
                    # `---` onto the value (the same bug this lane hit repairing source refs).
                    # The quote must be BALANCED, not optional-and-discarded. An optional trailing
                    # quote also matches the CLOSING delimiter of a multi-line block scalar that
                    # merely happens to end with a ref line -- swallowing it makes the whole
                    # document unparsable. (It did: one page on the live vault.)
                    _pat = (r"^([ \t]*(?:-[ \t]*|" + re.escape(str(_key)) + r":[ \t]*))"
                            r"(?:\[\[)?(?P<q>['\"])?" + re.escape(_old) + r"(?:\]\])?(?(q)(?P=q))[ \t]*$")
                    _nh = re.sub(_pat,
                                 lambda mm, _f=_new: mm.group(1) + (mm.group("q") or "") + _f
                                 + (mm.group("q") or ""), _nh, flags=re.M)
                if _nh != head:
                    repaired_edges += len(_edges)
                    print(f"  edge  {p.relative_to(WIKI).as_posix()}: "
                          + ", ".join(f"{o} -> {n}" for _, o, n in _edges[:2]))
                    if not args.dry_run:
                        p.write_text(f"---\n{_nh}---\n{text[match.end():]}", encoding="utf-8")
                        try:                      # glob-then-read race: the page can vanish mid-scan
                            text = p.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            continue
                        match = _FM_RE.match(text)
                        if not match:
                            continue
                        head = match.group(1)
        if _HAS_SOURCES.search(head):
            # The key exists, so this page is not the body-only defect — but an entry in it may name
            # a real record in an unresolvable shape, which grades as nothing. Repair those in place.
            fm0 = _frontmatter(text)
            if not isinstance(fm0, dict) or str(fm0.get("type") or "") in _EXEMPT_TYPES:
                continue
            fixes = []
            for ref in (fm0.get("sources") or []):
                if not isinstance(ref, str) or _resolves(ref.strip().removesuffix(".md")):
                    continue
                newref = canonical_ref(ref)
                if newref and newref != ref.strip().removesuffix(".md"):
                    fixes.append((ref, newref))
            if not fixes:
                continue
            new_head = head
            for old, newref in fixes:
                # [ \t] not \s -- \s matches newlines, and a greedy \s*$ eats the line
                # terminator, gluing the closing `---` onto the last list entry.
                new_head = re.sub(
                    r"^([ \t]*-[ \t]*)['\"]?" + re.escape(old) + r"['\"]?[ \t]*$",
                    lambda mm, _f=newref: mm.group(1) + _f, new_head, flags=re.M)
            if new_head == head:
                # A repairable VALUE whose LINE the substitution did not match (an inline flow list,
                # say). Counting and printing it here would report a repair that was never written,
                # and would report it again on every subsequent run. Report what was WRITTEN — the
                # edge branch above already works this way.
                continue
            repaired_refs += len(fixes)
            print(f"  ref   {p.relative_to(WIKI).as_posix()}: "
                  + ", ".join(f"{o} -> {n}" for o, n in fixes[:3]))
            if not args.dry_run:
                p.write_text(f"---\n{new_head}---\n{text[match.end():]}", encoding="utf-8")
            continue
        fm = _frontmatter(text)
        if not isinstance(fm, dict) or str(fm.get("type") or "") in _EXEMPT_TYPES:
            continue
        body = text[match.end():]
        refs = body_citations(body)
        if not refs:
            # tell apart "cites nothing" from "cites only things that do not exist" — the second is
            # a data-quality signal a silent skip would swallow
            if any(r.strip().removesuffix(".md").startswith("sources/") for r in _WIKILINK.findall(body)):
                skipped_dangling += 1
                print(f"  skip  {p.relative_to(WIKI).as_posix()}: body cites only unresolvable sources")
            continue

        block = "sources:\n" + "".join(f"- {r}\n" for r in refs)
        new_head = head.rstrip("\n") + "\n" + block
        fixed += 1
        print(f"  lift  {p.relative_to(WIKI).as_posix()}: {len(refs)} source(s) -> frontmatter")
        if not args.dry_run:
            p.write_text(f"---\n{new_head}---\n{body}", encoding="utf-8")

    print(f"source-normalize: {fixed} page(s) lifted, {repaired_refs} unresolvable ref(s) repaired, "
          f"{repaired_edges} dangling edge(s) re-pointed, "
          f"{skipped_dangling} skipped (dangling only)"
          f"{' [dry-run]' if args.dry_run else ''}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
