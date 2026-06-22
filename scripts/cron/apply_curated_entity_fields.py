#!/usr/bin/env python3
"""Enforce operator-curated frontmatter fields against agent-cron regen.

Agent crons (page-backfill / raw-backfill) periodically regenerate knowledge
pages and DROP or overwrite hand-curated fields. Prompt rules
don't hold — same agent-attention limit seen across the frontmatter-hygiene
work. So the curated truth lives
OUTSIDE the page in an overlay, and this deterministic enforcer stamps it
back in. Run it as a frequent cron; whatever a regen did, the next pass
restores the curated values.

Overlay (source-of-truth): JSON mapping page-slug → {field: value}. Only
the listed fields are enforced; everything else in the page (and the body)
is left byte-identical.

  {
    "example-page": {
      "_comment": "optional — emitted as a YAML comment above the block",
      "curated_field": "value",
      "curated_list": ["[[<namespace>/example-target]]"]
    }
  }

Enforcement is OVERWRITE (curated value wins over whatever's there) — so the
overlay is the canonical home for these fields; edit the overlay, not the
page, for anything listed here. Gated: writes only if the result parses
as a mapping with `type:` AND the body is unchanged.

Scope: the overlay slugs are resolved against the curated-overlay namespace(s).
By default the engine scans every knowledge namespace declared in the pack's
schema.yaml (partitioning.namespaces); set CURATED_NAMESPACE to pin one.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
OVERLAY_PATH = Path(os.environ.get(
    "CURATED_FIELDS_PATH", "/opt/data/config/curated-entity-fields.json"))


def curated_namespaces() -> list[str]:
    """Namespace dir(s) the overlay slugs are resolved against. CURATED_NAMESPACE
    pins one; otherwise every knowledge namespace the pack declares in
    schema.yaml. Empty (packless / no namespaces declared) ⇒ the wiki root."""
    pinned = os.environ.get("CURATED_NAMESPACE", "").strip()
    if pinned:
        return [pinned]
    ns = sorted(schema_lib.knowledge_namespaces(schema_lib.governing_schema(VAULT)))
    return ns if ns else [""]


def resolve_page(slug: str, namespaces: list[str]) -> Path | None:
    """First existing `<namespace>/<slug>.md` (or `<slug>.md` at wiki root for
    an empty namespace), in declared namespace order. None if the slug already
    carries a namespace prefix that resolves, or nothing matches."""
    # A slug may itself be namespace-qualified (e.g. "entities/acme").
    direct = WIKI / f"{slug}.md"
    if direct.exists():
        return direct
    for ns in namespaces:
        cand = (WIKI / ns / f"{slug}.md") if ns else (WIKI / f"{slug}.md")
        if cand.exists():
            return cand
    return None

_FM_RE = re.compile(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*\n)(.*)\Z", re.S)
_TYPE_LINE_RE = re.compile(r"^type:", re.MULTILINE)


def split_fm_body(text: str):
    m = _FM_RE.match(text)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def parses_with_type(fm_text: str) -> bool:
    try:
        d = yaml.safe_load(fm_text)
    except Exception:
        return False
    return isinstance(d, dict) and "type" in d


def render_field(name: str, value) -> list[str]:
    """Render one field as YAML lines. Lists → block form; scalars inline.
    Strings containing wikilinks/colons are quoted."""
    if isinstance(value, list):
        if not value:
            return [f"{name}: []"]
        out = [f"{name}:"]
        for item in value:
            s = str(item)
            if "[[" in s or ":" in s or s.strip() != s:
                out.append('  - "' + s.replace('"', '\\"') + '"')
            else:
                out.append(f"  - {s}")
        return out
    if isinstance(value, bool):
        # YAML booleans are lowercase; str(bool) is Python-cased ("True").
        return [f"{name}: {'true' if value else 'false'}"]
    s = str(value)
    if "[[" in s or ":" in s or "#" in s:
        return [f'{name}: "{s}"']
    return [f"{name}: {s}"]


def _strip_keys(fm_body: str, keys: set[str]) -> str:
    """Remove top-level `key:` lines (and their indented block continuations)
    for the given keys. Leaves all other lines verbatim."""
    lines = fm_body.splitlines()
    out: list[str] = []
    skip_block = False
    for line in lines:
        m = re.match(r"^([A-Za-z_][\w-]*):", line)
        if m:
            skip_block = m.group(1) in keys
            if skip_block:
                continue
            out.append(line)
            continue
        # continuation line (indented list item / scalar / blank within block)
        if skip_block and (line.startswith((" ", "\t")) or line.strip() == ""):
            # a blank line ends the block unless followed by more indent;
            # keep it simple: drop indented continuations, keep blanks
            if line.strip() == "":
                skip_block = False
                out.append(line)
            # else: drop the indented continuation
            continue
        skip_block = False
        out.append(line)
    return "\n".join(out)


def enforce(text: str, fields: dict) -> tuple[str | None, list[str]]:
    """Return (new_text|None, changed_keys). None if not applicable/unsafe."""
    parts = split_fm_body(text)
    if parts is None:
        return None, []
    opening, fm_body, closing, body = parts

    real = {k: v for k, v in fields.items() if not k.startswith("_")}
    comment = fields.get("_comment")

    # Already correct? (parse + compare each enforced key)
    try:
        cur = yaml.safe_load(fm_body)
    except Exception:
        cur = None
    if isinstance(cur, dict) and all(cur.get(k) == v for k, v in real.items()):
        return None, []   # nothing to do

    stripped = _strip_keys(fm_body, set(real.keys()))
    block: list[str] = []
    if comment:
        block.append(f"# {comment}")
    for k, v in real.items():
        block.extend(render_field(k, v))

    # Insert the curated block right after the `type:` line (stable anchor).
    slines = stripped.splitlines()
    out: list[str] = []
    inserted = False
    for line in slines:
        out.append(line)
        if not inserted and _TYPE_LINE_RE.match(line):
            out.extend(block)
            inserted = True
    if not inserted:
        out = block + slines

    new_fm = "\n".join(out)
    if not new_fm.endswith("\n"):
        new_fm += "\n"
    if not parses_with_type(new_fm):
        return None, []
    new_text = opening + new_fm + closing + body
    # body must be byte-identical
    np = split_fm_body(new_text)
    if np is None or np[3] != body:
        return None, []
    return new_text, list(real.keys())


def main() -> int:
    if not OVERLAY_PATH.exists():
        print(f"No overlay at {OVERLAY_PATH} — nothing to enforce.")
        print(json.dumps({"wakeAgent": False}))
        return 0
    try:
        overlay = json.loads(OVERLAY_PATH.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: overlay not valid JSON: {e}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    enforced = 0
    perm_skips = 0
    namespaces = curated_namespaces()
    print("=== apply-curated-fields ===")
    print(f"  overlay: {OVERLAY_PATH} ({len(overlay)} page(s))")
    for slug, fields in overlay.items():
        if not isinstance(fields, dict):
            print(f"  ! {slug}: overlay value not an object — skipped")
            continue
        path = resolve_page(slug, namespaces)
        if path is None:
            print(f"  ! {slug}: page not found — skipped")
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        new_text, changed = enforce(text, fields)
        if new_text is None:
            continue  # already correct, or unsafe (gate refused)
        try:
            path.write_text(new_text)
        except PermissionError:
            perm_skips += 1
            print(f"  ! {slug}: PERMISSION DENIED — chmod 646 + re-run")
            continue
        enforced += 1
        print(f"  + {slug}: restored/enforced {', '.join(changed)}")

    print()
    print(f"Enforced curated fields on {enforced} page(s).")
    if perm_skips:
        print(f"Permission-skipped: {perm_skips}.")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
