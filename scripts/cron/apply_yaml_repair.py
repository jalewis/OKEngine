#!/usr/bin/env python3
"""Phase 2 of the broken-YAML propose/dispose repair.

The agent (phase 1) reads each broken-frontmatter file and proposes a
corrected frontmatter block as text, writing a JSON proposal to:
    $WIKI_PATH/wiki/.yaml-repair-proposals.json

Schema (list):
  [
    {"path": "wiki/sources/foo.md",
     "frontmatter": "type: source\ntags: [a, b]\n...",   # NO --- delimiters
     "notes": "optional"},
    ...
  ]

This script applies each proposal under STRICT gates — it replaces ONLY
the frontmatter block, never the body, and writes only when:
  1. the proposed frontmatter parses under yaml.safe_load
  2. it is a mapping containing `type:`
  3. the original file's body can be cleanly located (a closing `---`)
  4. the resulting file's body is byte-identical to the original body

Any proposal failing a gate is skipped + logged; the file stays broken.
Idempotent: a file already valid is skipped. Consumes the proposals file
on success (renames to .applied), unless permission-skips occurred.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
PROPOSALS = VAULT / "wiki" / ".yaml-repair-proposals.json"

# opening --- , frontmatter body (lazy), closing --- , rest(body)
_FM_RE = re.compile(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*\n?)(.*)\Z", re.S)


def split_fm_body(text: str):
    """Return (fm_text, body_text) using the FIRST closing ---, or None."""
    m = _FM_RE.match(text)
    if not m:
        return None
    return m.group(2), m.group(4)


def parses_with_type(fm_text: str) -> bool:
    try:
        data = yaml.safe_load(fm_text)
    except Exception:
        return False
    return isinstance(data, dict) and "type" in data


def is_valid_file(text: str) -> bool:
    """True if the file's existing frontmatter already parses (skip it)."""
    parts = split_fm_body(text)
    if parts is None:
        return False  # no clean frontmatter block — can't be confirmed valid
    return parses_with_type(parts[0])


def locate_body(text: str, body_starts_with: str | None):
    """Return the body string to preserve.

    - If `body_starts_with` is given (structureless file, no clean close):
      find that verbatim line AFTER the opening `---`, and the body is the
      original text from that line to EOF. Returns None if not locatable.
    - Otherwise: use the first clean closing `---` (split_fm_body)."""
    if body_starts_with:
        if not text.startswith("---"):
            return None
        marker = body_starts_with.rstrip("\n")
        # search line-by-line after the opening delimiter line
        lines = text.splitlines(keepends=True)
        for i in range(1, len(lines)):
            if lines[i].rstrip("\n") == marker:
                return "".join(lines[i:])
        return None
    parts = split_fm_body(text)
    return parts[1] if parts is not None else None


def apply_one(path: Path, proposed_fm: str, body_starts_with: str | None = None) -> tuple[bool, str]:
    """Apply one proposal. Returns (applied, message)."""
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return False, f"read error: {e}"

    body = locate_body(text, body_starts_with)
    if body is None:
        return False, "could not locate body (boundary / body_starts_with) — skipped"

    fm = proposed_fm.strip("\n")
    # gate 1+2: parses + has type
    if not parses_with_type(fm):
        return False, "proposed frontmatter does not parse / lacks type: — skipped"

    new_text = "---\n" + fm + "\n---\n" + body
    # gate 4: re-split the result and confirm the preserved body is intact
    new_parts = split_fm_body(new_text)
    if new_parts is None or new_parts[1] != body:
        return False, "body would change — refused"

    try:
        path.write_text(new_text)
    except PermissionError:
        return False, "PERMISSION DENIED (host-owned) — chmod 646 + re-run"
    return True, "repaired"


def main() -> int:
    if not PROPOSALS.exists():
        print(f"No proposals at {PROPOSALS} — nothing to apply.")
        print(json.dumps({"wakeAgent": False}))
        return 0
    try:
        proposals = json.loads(PROPOSALS.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: proposals not valid JSON: {e}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1
    if not isinstance(proposals, list):
        print("ERROR: proposals must be a JSON list", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    applied = 0
    skipped = 0
    perm_skips = 0
    print("=== apply-yaml-repair ===")
    for entry in proposals:
        if not isinstance(entry, dict):
            print(f"  ! non-object entry skipped: {entry!r}")
            continue
        rel = entry.get("path")
        fm = entry.get("frontmatter")
        if not rel or not isinstance(fm, str):
            print(f"  ! malformed entry skipped: {entry!r}")
            continue
        path = (VAULT / rel) if not rel.startswith("/") else Path(rel)
        if not path.exists():
            print(f"  ! {rel}: file not found — skipped")
            skipped += 1
            continue
        try:
            if is_valid_file(path.read_text(errors="replace")):
                print(f"  = {rel}: already valid — skipped")
                continue
        except OSError:
            pass
        ok, msg = apply_one(path, fm, entry.get("body_starts_with"))
        if ok:
            applied += 1
            print(f"  + {rel}: {msg}")
        else:
            if "PERMISSION DENIED" in msg:
                perm_skips += 1
            else:
                skipped += 1
            print(f"  ? {rel}: {msg}")
        if entry.get("notes"):
            print(f"      note: {entry['notes']}")

    print()
    print(f"Applied {applied}; skipped {skipped}; permission-skipped {perm_skips}.")
    if perm_skips:
        print("Proposals retained for re-run after chmod.")
    else:
        consumed = PROPOSALS.with_suffix(".json.applied")
        PROPOSALS.replace(consumed)
        print(f"Proposals consumed → {consumed.name}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
