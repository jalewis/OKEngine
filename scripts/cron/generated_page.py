#!/usr/bin/env python3
"""generated_page — stable identity for pages a deterministic lane regenerates whole (okengine#933).

Dashboards, reports and operational pages are rebuilt from scratch on every run. Before this
module each writer emitted a fresh frontmatter block, so the immutable ``id`` that
``scripts/backfill_ids.py`` stamped was dropped on the next run. An id-less page cannot be
updated through the enforced write path ("missing required field(s): id"), and the backfill
count never converged.

Every engine and first-party-extension writer of a generated page routes its final text
through :func:`write` (or :func:`with_id`), which:

  1. keeps an ``id`` the writer pinned in its own frontmatter,
  2. otherwise preserves the ``id`` already on the page on disk (ids are immutable),
  3. otherwise derives exactly the id ``backfill_ids`` would stamp on that page, so a later
     backfill is a no-op: the governing schema's type authority when it applies, else a slug
     minted from the namespace and the page's natural key (title, name, then filename stem).
     A sibling page in the same directory that already carries the derived slug id gets the
     backfill's path-stable disambiguation.

The derivation (:func:`derive_for_page`), the frontmatter parse, the id insertion and the
reserved-name exemption are the single source that ``backfill_ids`` also imports, so the two
cannot drift. This is a staged cron library (``/opt/data/scripts``), not a baked write-path lib.
"""
from __future__ import annotations

import re
from pathlib import Path

import id_lib
import schema_lib
import yaml
from yaml import YAMLError

FM_RE = re.compile(r"\A(---\s*\n)(.*?\n)(---\s*(?:\n|\Z))", re.DOTALL)
#: Structural files that never carry an id (backfill_ids skips them too).
RESERVED = frozenset({"index.md", "log.md", "agents.md", "readme.md", "hot.md",
                      "bundle.md", "health.md"})
#: Pagination pages of a (reserved) INDEX.md: structural, regenerated with shifting content.
_INDEX_PAGE_RE = re.compile(r"INDEX-p\d+\.md", re.IGNORECASE)
#: Only a minted slug can be disambiguated; an authority id shared by two pages is a real duplicate.
_DISAMBIGUABLE = frozenset({"slug"})


def is_exempt(path: Path) -> bool:
    """True for files that are not id-bearing pages (reserved, hidden, backups, the schema)."""
    name = path.name
    lower = name.lower()
    return (lower in RESERVED or name.startswith(("_", "."))
            or ".bak." in name or lower == "schema.yaml"
            or _INDEX_PAGE_RE.fullmatch(name) is not None)


def parse_frontmatter(text: str):
    """``(frontmatter dict, match)``; ``(None, None)`` without a frontmatter block and
    ``(None, match)`` when the block is not valid YAML. A non-mapping block parses as ``{}``."""
    m = FM_RE.match(text)
    if not m:
        return None, None
    try:
        fm = yaml.safe_load(m.group(2))
    except (YAMLError, ValueError, TypeError):   # invalid YAML or an unconstructible scalar
        return None, m
    return (fm if isinstance(fm, dict) else {}), m


def page_id(fm: object) -> str:
    """The page's stamped id, or ``""`` when it carries none (only a non-blank string counts)."""
    value = fm.get("id") if isinstance(fm, dict) else None
    return value.strip() if isinstance(value, str) else ""


def insert_id(text: str, pid: str, m: "re.Match") -> str:
    """Insert ``id: "<pid>"`` as the first frontmatter line, preserving the rest byte-for-byte."""
    return m.group(1) + f'id: "{pid}"\n' + m.group(2) + m.group(3) + text[m.end():]


def derive_for_page(vault: Path, rel: str, fm: dict) -> tuple[str, str]:
    """``(id, kind)`` for a page at wiki-relative ``rel`` with frontmatter ``fm``."""
    namespace = rel.partition("/")[0]
    stem = Path(rel).stem
    schema = schema_lib.merged_schema(vault, namespace)
    ptype = str(fm.get("type") or "").strip()
    authority, id_field = schema_lib.type_id_authority(schema, ptype)
    local_id = fm.get(id_field) if authority else None
    return id_lib.derive_id(authority=authority, local_id=local_id,
                            minted_scope=namespace,
                            slug_source=id_lib.natural_key(fm, fallback=stem))


def disambiguate(pid: str, rel: str) -> str:
    """The backfill's path-stable replacement for a slug id another page already claims."""
    scope = id_lib.parse_id(pid)[0]
    return id_lib.make_id(scope, f"{Path(rel).stem}-{id_lib._short_hash(rel, 6)}")


def _head(handle) -> str:
    """The text of ``handle`` through its closing frontmatter line (all of it when there is none)."""
    head = ""
    for line in handle:
        head += line
        if FM_RE.match(head):
            return head
    return head


def _read_id(path: Path) -> str:
    """The id stamped on the page at ``path``; the body is never read."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = _head(handle)
    except OSError:
        return ""
    fm, _ = parse_frontmatter(head)
    return page_id(fm)


def _sibling_claims(path: Path, pid: str) -> bool:
    """True when a page in ``path``'s directory already carries ``pid``. The page itself never
    matches: this runs only while it carries no id."""
    siblings = sorted(path.parent.glob("*.md"))  # glob-ok: one directory's dated siblings
    return any(not is_exempt(sib) and _read_id(sib) == pid for sib in siblings)


def wiki_root(path: Path) -> Path:
    """The vault's ``wiki/`` directory containing ``path``: its nearest ancestor named ``wiki``."""
    resolved = Path(path).resolve()
    for parent in resolved.parents:
        if parent.name == "wiki":
            return parent
    raise ValueError(f"{path} is not inside a vault wiki/ directory; cannot derive its id")


def with_id(path: Path, text: str, *, wiki: Path | None = None) -> str:
    """Return ``text`` (a whole page about to be written at ``path``) carrying an id.

    Unchanged when the file is exempt, has no parseable frontmatter, or already pins an id.
    ``wiki`` defaults to the nearest ``wiki/`` ancestor of ``path``.
    """
    path = Path(path)
    if is_exempt(path):
        return text
    fm, m = parse_frontmatter(text)
    if fm is None or m is None or page_id(fm):
        return text
    pid = _read_id(path)
    if not pid:
        root = Path(wiki).resolve() if wiki is not None else wiki_root(path)
        rel = path.resolve().relative_to(root).as_posix()
        pid, kind = derive_for_page(root.parent, rel, fm)
        if kind in _DISAMBIGUABLE and _sibling_claims(path, pid):
            pid = disambiguate(pid, rel)
    return insert_id(text, pid, m)


def write(path: Path, text: str, *, encoding: str = "utf-8", wiki: Path | None = None) -> str:
    """Write a generated page with its identity preserved or derived; returns the text written."""
    final = with_id(path, text, wiki=wiki)
    Path(path).write_text(final, encoding=encoding)
    return final
