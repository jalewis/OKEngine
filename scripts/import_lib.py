#!/usr/bin/env python3
"""import_lib — assembly helpers for adopting an EXISTING (foreign) vault into a pack (okengine#158
… no, #154). Reuses the heavy primitives (backfill_ids, okf_migrate, the normalize suite); these are
the thin, ordered, dry-run-first steps an import migration sequences:

  scan            — inventory the corpus (pages, typed/untyped, type distribution)
  retype_by_type  — apply a {old_type: new_type} map (the pack's type_aliases / deterministic retypes)
  set_type_for_slugs — curated per-page retype (the classifier/curated-list step, e.g. concept->segment)
  remap_fields    — per-type frontmatter field rename + default (reconcile a foreign schema's shapes)
  id_collisions   — backfill_ids dry-run report (authority duplicates = human-merge worklist)

Every transform is DRY-RUN-FIRST (returns a change report; writes only when apply=True), line-based
on frontmatter so other keys/comments/order are preserved (write-guard-safe), and idempotent.
The import migration (a pack-local m_*.py under <pack>/.okengine/migrations/) wires these in order
and rides `framework upgrade`'s snapshot / rollback / validate harness.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "cron"))

_FM = re.compile(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*(?:\n|\Z))", re.S)


def _iter_pages(wiki: Path):
    for p in wiki.rglob("*.md"):
        n = p.name
        if n.startswith(("_", ".")) or n == "INDEX.md" or n.startswith("INDEX-") or ".bak." in n:
            continue
        yield p


def _split_fm(text: str):
    m = _FM.match(text)
    return (m.group(2), m.group(3) and text[m.end():] or "", m) if m else (None, None, None)


def _fm_get(fm_block: str, key: str):
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", fm_block, re.M)
    return m.group(1).strip() if m else None


def scan(wiki: Path) -> dict:
    """Inventory: total pages, untyped (no `type:`), and the type distribution."""
    total = untyped = 0
    types: dict[str, int] = {}
    for p in _iter_pages(wiki):
        fm, _, _ = _split_fm(p.read_text(encoding="utf-8", errors="replace"))
        if fm is None:
            untyped += 1
            continue
        t = _fm_get(fm, "type")
        if not t:
            untyped += 1
        else:
            types[t] = types.get(t, 0) + 1
            total += 1
    return {"pages": total + untyped, "typed": total, "untyped": untyped,
            "types": dict(sorted(types.items(), key=lambda kv: -kv[1]))}


def _rewrite_type(text: str, new_type: str) -> str:
    fm, _, m = _split_fm(text)
    if fm is None:
        return text
    if re.search(r"^type:[ \t]*.*$", fm, re.M):
        new_fm = re.sub(r"^type:[ \t]*.*$", f"type: {new_type}", fm, count=1, flags=re.M)
    else:
        new_fm = f"type: {new_type}\n" + fm
    return m.group(1) + new_fm + text[m.end(2):]


def retype_by_type(wiki: Path, type_map: dict, apply: bool) -> list[str]:
    """Apply {old_type: new_type}. Report one line per page changed."""
    out = []
    for p in _iter_pages(wiki):
        text = p.read_text(encoding="utf-8", errors="replace")
        fm, _, _ = _split_fm(text)
        if fm is None:
            continue
        cur = _fm_get(fm, "type")
        if cur in type_map and type_map[cur] != cur:
            out.append(f"retype {p.name}: {cur} -> {type_map[cur]}")
            if apply:
                p.write_text(_rewrite_type(text, type_map[cur]), encoding="utf-8")
    return out


def set_type_for_slugs(wiki: Path, slug_type: dict, apply: bool) -> list[str]:
    """Curated per-page retype {slug: new_type} — the classifier/curated-list step."""
    out = []
    for p in _iter_pages(wiki):
        st = slug_type.get(p.stem)
        if not st:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        fm, _, _ = _split_fm(text)
        if fm is None or _fm_get(fm, "type") == st:
            continue
        out.append(f"retype {p.name}: -> {st} (curated)")
        if apply:
            p.write_text(_rewrite_type(text, st), encoding="utf-8")
    return out


def remap_fields(wiki: Path, per_type: dict, apply: bool) -> list[str]:
    """Per-type frontmatter reconciliation. `per_type` = {type: {"rename": {old:new}, "default":
    {field:value}}}. Rename rewrites a key (keeping its value); default adds a key only if absent."""
    out = []
    for p in _iter_pages(wiki):
        text = p.read_text(encoding="utf-8", errors="replace")
        fm, _, m = _split_fm(text)
        if fm is None:
            continue
        spec = per_type.get(_fm_get(fm, "type") or "")
        if not spec:
            continue
        new_fm, changed = fm, []
        for old, new in (spec.get("rename") or {}).items():
            if re.search(rf"^{re.escape(old)}:", new_fm, re.M):
                new_fm = re.sub(rf"^{re.escape(old)}:", f"{new}:", new_fm, count=1, flags=re.M)
                changed.append(f"{old}->{new}")
        for field, val in (spec.get("default") or {}).items():
            if not re.search(rf"^{re.escape(field)}:", new_fm, re.M):
                new_fm = new_fm.rstrip("\n") + f"\n{field}: {val}\n"
                changed.append(f"+{field}")
        if changed:
            out.append(f"remap {p.name}: {', '.join(changed)}")
            if apply:
                p.write_text(m.group(1) + new_fm + text[m.end(2):], encoding="utf-8")
    return out


def id_collisions(vault: Path) -> dict:
    """backfill_ids dry-run report. {to_stamp, slug_collisions, authority_collisions} — the last is
    the human-merge worklist (never auto-collapsed)."""
    import backfill_ids
    rep = backfill_ids.run(Path(vault), apply=False)
    return rep


# ── okengine#154 LAYOUT step — link-preserving cross-namespace re-home ─────────────────────────
# okengine derives a page's namespace from its TYPE (entities/ vs sources/ vs concepts/ …), normally
# enforced by the MCP write path. A bulk import COPIES the foreign tree, bypassing that — so a
# `type: source` page can sit under `frontier/`. This re-homes by type and rewrites every
# `[[reference]]` in lockstep (the cross-namespace generalization of cron/okf_migrate's within-ns
# reshard; same `iwe rename` link contract).
_BASE_NS = {"source": "sources", "concept": "concepts", "prediction": "predictions",
            "finding": "findings", "dashboard": "dashboards", "briefing": "briefings",
            "trend": "trends"}
_LINK = re.compile(r"\[\[([^\]|#\n]+)([\]#|])")


def derive_ns_map(schema: dict) -> dict:
    """Default ``{type -> canonical namespace}`` for :func:`rehome_by_type`: base/L1 types -> their
    fixed namespace; pack entity types (schema ``types``) and any alias resolving to one ->
    ``entities``. The migration EXTENDS this for pack types that own a DEDICATED namespace (e.g.
    ``{"report": "reports"}``) — those are documents, not entity-graph nouns, so they don't live in
    entities/."""
    m = dict(_BASE_NS)
    entity_types = set(schema.get("types") or {})
    for t in entity_types:
        m[t] = "entities"
    for alias, canon in (schema.get("type_aliases") or {}).items():
        if canon in entity_types or m.get(canon) == "entities":
            m[alias] = "entities"
        elif canon in _BASE_NS:
            m[alias] = _BASE_NS[canon]
    return m


def _shard(slug: str) -> str:
    c = (slug[:1] or "_").lower()
    return c if c.isalnum() else "_"


def _canonical_path(rel: Path, exp_ns: str, slug: str) -> str:
    """Wiki-relative path (no .md) where a page of namespace ``exp_ns`` belongs. Sharded namespaces
    -> ``<ns>/<first-letter>/<slug>``; sources keep their ``YYYY/MM`` date subpath; else flat."""
    if exp_ns == "sources":
        m = re.search(r"(\d{4})/(\d{2})", str(rel))
        return f"sources/{m.group(1)}/{m.group(2)}/{slug}" if m else f"sources/{slug}"
    if exp_ns in ("entities", "concepts"):
        return f"{exp_ns}/{_shard(slug)}/{slug}"
    return f"{exp_ns}/{slug}"


def layout_misplaced(wiki: Path, ns_for_type: dict) -> dict:
    """Report-only: ``{"<current_ns> -> <expected_ns>": count}`` for pages whose type's namespace
    (per ``ns_for_type``) differs from where they sit. The analyze surfaces this; the migration's
    layout step fixes it."""
    out: dict[str, int] = {}
    for p in _iter_pages(wiki):
        fm, _, _ = _split_fm(p.read_text(encoding="utf-8", errors="replace"))
        if fm is None:
            continue
        exp = ns_for_type.get(_fm_get(fm, "type"))
        cur = p.relative_to(wiki).parts[0]
        if exp and exp != cur:
            out[f"{cur} -> {exp}"] = out.get(f"{cur} -> {exp}", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def rehome_by_type(wiki: Path, ns_for_type: dict, apply: bool) -> list[str]:
    """okengine#154 LAYOUT step — link-preserving cross-namespace re-home.

    Move every page whose ``ns_for_type[type]`` differs from its current namespace into the right
    one AND rewrite every ``[[reference]]`` to it in lockstep. Invariant: the vault-wide ``[[link]]``
    total is unchanged (a 1:1 path swap can neither create nor drop a reference); a change RAISES so
    the ``framework upgrade`` snapshot rolls back. Collisions — the target path already holds a
    DIFFERENT page (a genuine duplicate) — are SKIPPED and reported, never overwritten (dedup is a
    human-merge decision, like authority ids)."""
    move_map: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    taken: set[str] = set()
    for p in _iter_pages(wiki):
        rel = p.relative_to(wiki)
        fm, _, _ = _split_fm(p.read_text(encoding="utf-8", errors="replace"))
        if fm is None:
            continue
        exp = ns_for_type.get(_fm_get(fm, "type"))
        if not exp or exp == rel.parts[0]:
            continue
        new = _canonical_path(rel, exp, p.stem)
        old = str(rel.with_suffix(""))
        if new in taken or ((wiki / f"{new}.md").exists() and new not in move_map):
            collisions.append((old, new))
            continue
        move_map[old] = new
        taken.add(new)

    out = [f"rehome {old} -> {new}" for old, new in move_map.items()]
    summary = (f"rehome-by-type: {len(move_map)} re-homed, {len(collisions)} collision(s) skipped "
               f"(target occupied — duplicate, left for human merge)")
    if not apply:
        return out + [summary + " [dry-run]"]

    def _link_total() -> int:
        return sum(len(_LINK.findall(q.read_text(encoding="utf-8", errors="replace")))
                   for q in wiki.rglob("*.md"))
    before = _link_total()

    for old, new in move_map.items():
        dst = wiki / f"{new}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        (wiki / f"{old}.md").rename(dst)

    def _repl(mt):
        nw = move_map.get(mt.group(1).strip())
        return f"[[{nw}{mt.group(2)}" if nw else mt.group(0)
    for q in wiki.rglob("*.md"):
        text = q.read_text(encoding="utf-8", errors="replace")
        nt = _LINK.sub(_repl, text)
        if nt != text:
            q.write_text(nt, encoding="utf-8")

    after = _link_total()
    if before != after:
        raise RuntimeError(f"rehome-by-type INVARIANT VIOLATED: wikilink total {before} -> {after} "
                           f"— a move dropped/duplicated references; rolling back")
    return out + [f"{summary}; {before} links preserved"]


def collapse_source_dates(wiki: Path, apply: bool) -> list[str]:
    """okengine#154 layout sub-step — normalize source date-DEPTH to the OKF two-segment standard.

    :func:`rehome_by_type` fixes CROSS-namespace placement (a source out of ``frontier/``), but a
    foreign vault can also nest sources a level too deep — ``sources/YYYY/MM/DD/slug`` instead of
    okengine's ``sources/YYYY/MM/slug`` (the index/dedup/hot_set scans assume EXACTLY two date
    segments). Collapse the day-dir, rewriting every ``[[reference]]`` in lockstep (same link-total
    invariant as rehome). Same-slug-different-day collisions disambiguate with a ``-DD`` suffix, never
    overwrite. Run AFTER rehome_by_type so all sources are under ``sources/`` first."""
    src = wiki / "sources"
    if not src.is_dir():
        return []
    move_map: dict[str, str] = {}
    taken: set[str] = set()
    disambig = 0
    for p in src.rglob("*.md"):
        if p.name.startswith(("_", ".")) or p.name == "INDEX.md" or p.name.startswith("INDEX-"):
            continue
        parts = p.relative_to(wiki).with_suffix("").parts          # sources / YYYY / MM / DD / slug
        if len(parts) != 5:                                        # only the YYYY/MM/DD/slug shape
            continue
        _, yyyy, mm, dd, slug = parts
        new = f"sources/{yyyy}/{mm}/{slug}"
        if new in taken or ((wiki / f"{new}.md").exists() and new not in move_map):
            new = f"sources/{yyyy}/{mm}/{slug}-{dd}"
            disambig += 1
        move_map[str(p.relative_to(wiki).with_suffix(""))] = new
        taken.add(new)

    out = [f"collapse {old} -> {new}" for old, new in move_map.items()]
    summary = (f"collapse-source-dates: {len(move_map)} day-dir source(s) -> YYYY/MM, "
               f"{disambig} same-slug disambiguated")
    if not apply:
        return out + [summary + " [dry-run]"]

    def _link_total() -> int:
        return sum(len(_LINK.findall(q.read_text(encoding="utf-8", errors="replace")))
                   for q in wiki.rglob("*.md"))
    before = _link_total()

    for old, new in move_map.items():
        dst = wiki / f"{new}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        (wiki / f"{old}.md").rename(dst)

    def _repl(mt):
        nw = move_map.get(mt.group(1).strip())
        return f"[[{nw}{mt.group(2)}" if nw else mt.group(0)
    for q in wiki.rglob("*.md"):
        text = q.read_text(encoding="utf-8", errors="replace")
        nt = _LINK.sub(_repl, text)
        if nt != text:
            q.write_text(nt, encoding="utf-8")

    after = _link_total()
    if before != after:
        raise RuntimeError(f"collapse-source-dates INVARIANT VIOLATED: wikilink total {before} -> "
                           f"{after} — a move dropped/duplicated references; rolling back")
    return out + [f"{summary}; {before} links preserved"]
