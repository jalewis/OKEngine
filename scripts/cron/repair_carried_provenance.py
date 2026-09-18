#!/usr/bin/env python3
"""Restore ingest provenance the raw→wiki compile dropped or invented.

Ingest provenance is CARRIED, never re-derived: the fetching lane knows which feed an item
came from, which channel, which query matched it and what kind of source it is, and the
compile agent is told to copy those keys verbatim (select_raw_batch.PROVENANCE_KEYS). When a
key is missing from that list the agent has no instruction to copy it, and a model asked for
a field nobody gave it will supply one.

That is not hypothetical. `source_kind` was absent from the carry list; the compile model
changed on 2026-07-26 and from 2026-07-28 every source page on a live vault was written with
one single kind, ~30/day across four unrelated publishers on two independent ingest channels,
while the raw files beside them still carried four different kinds. A downstream lane
selecting on an exact kind matched nothing for three weeks and reported success throughout.

The raw pages kept the truth the whole time, so the repair is a copy, not a guess.

WHAT IT CHANGES
  For each wiki page carrying a `raw:` reference, for each PROVENANCE_KEY the RAW page
  declares: if the wiki page disagrees or lacks it, set it to the raw page's value.

WHAT IT WILL NOT DO
  * invent: a key the raw page does not declare is left exactly as it is
  * reformat: the raw page's own value TEXT is copied verbatim, so nothing is re-serialised
    and no neighbouring field is touched
  * move: pages are edited where they sit, never re-filed (okengine#54)
  * guess at a missing raw: an unresolvable `raw:` ref is REPORTED, never counted clean

DRY-RUN BY DEFAULT. Pass --apply to write.

Env: WIKI_PATH (/opt/vault)
Usage: repair_carried_provenance.py [--apply] [--limit N] [--key K ...]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schema_lib  # noqa: E402
from select_raw_batch import PROVENANCE_KEYS  # noqa: E402  — ONE definition of the carry set
# ONE definition of the declared vocabulary, shared with corpus_audit and the write path. A
# `no_agent` lane bypasses the write SERVER; it does not get to bypass the corpus invariants,
# and schema conformance is one of them. Copying a raw value verbatim is only safe while the
# field is unconstrained or its enum is extensible — on the vault this was built against, raw
# pages carried `intrusion-report` and `cyber-news`, which no schema there declares. So the
# rule is resolved from the composed schema, never assumed.
from provenance_lib import closed_enums as enum_rules  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
_FM_RE = re.compile(r"\A---[ \t]*\n(?P<fm>.*?\n)---[ \t]*(?:\n|\Z)", re.S)
MAX_EXAMPLES = 12


def frontmatter_text(path: Path) -> str | None:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FM_RE.match(head)
    return m.group("fm") if m else None


def scalar_lines(fm: str) -> dict[str, str]:
    """key -> the value TEXT exactly as written, for top-level scalar keys only.

    Copying the text rather than a parsed value keeps quoting, numbers and dates identical to
    what the ingest wrote; a yaml round-trip would rewrite every neighbouring field.
    Multi-line and block values are deliberately not matched -- provenance is scalar.
    """
    out: dict[str, str] = {}
    for line in fm.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):[ \t]+(\S.*?)[ \t]*$", line)
        if m:
            out.setdefault(m.group(1), m.group(2))
    return out


def raw_path_for(vault: Path, fm: str) -> Path | None:
    m = re.search(r"^raw:[ \t]+(\S.*?)[ \t]*$", fm, re.M)
    if not m:
        return None
    rel = m.group(1).strip().strip("'\"")
    if not rel or rel.startswith(("/", "..")):
        return None
    return vault / rel


def apply_values(text: str, updates: dict[str, str]) -> str:
    """Set each key to its raw value text, editing only those lines."""
    m = _FM_RE.match(text)
    fm, rest = m.group("fm"), text[m.end():]
    lines = fm.splitlines(keepends=True)
    remaining = dict(updates)
    for i, line in enumerate(lines):
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):[ \t]+", line)
        if km and km.group(1) in remaining:
            key = km.group(1)
            lines[i] = f"{key}: {remaining.pop(key)}\n"
    for key, value in remaining.items():          # absent entirely -> append to the block
        lines.append(f"{key}: {value}\n")
    return "---\n" + "".join(lines) + "---\n" + rest


def scan(vault: Path, keys: tuple[str, ...], apply: bool, limit: int | None) -> dict:
    wiki = vault / "wiki"
    state = {
        "pages": 0, "with_raw": 0, "raw_missing": 0, "raw_unreadable": 0,
        "divergent_pages": 0, "repaired_pages": 0, "by_key": {}, "examples": [],
        "unresolvable_examples": [], "applied": apply,
        "schema_refused": 0, "refused_examples": [],
    }
    if not wiki.is_dir():
        state["error"] = f"no wiki/ under {vault}"
        return state
    try:
        closed_enums = enum_rules(schema_lib.merged_schema(vault, "sources"))
    except Exception as exc:            # a vault with no resolvable schema is not a pass
        state["error"] = f"cannot resolve the governing schema: {exc}"
        return state
    for page in sorted(wiki.rglob("*.md")):
        if page.name.startswith(("INDEX", "_")):
            continue
        fm = frontmatter_text(page)
        if fm is None:
            continue
        state["pages"] += 1
        raw = raw_path_for(vault, fm)
        if raw is None:
            continue
        state["with_raw"] += 1
        rel = page.relative_to(wiki).as_posix()
        if not raw.is_file():
            state["raw_missing"] += 1
            if len(state["unresolvable_examples"]) < MAX_EXAMPLES:
                state["unresolvable_examples"].append(f"{rel} -> {raw.name} (absent)")
            continue
        raw_fm = frontmatter_text(raw)
        if raw_fm is None:
            state["raw_unreadable"] += 1
            if len(state["unresolvable_examples"]) < MAX_EXAMPLES:
                state["unresolvable_examples"].append(f"{rel} -> {raw.name} (no frontmatter)")
            continue
        raw_vals, page_vals = scalar_lines(raw_fm), scalar_lines(fm)
        updates = {k: raw_vals[k] for k in keys
                   if k in raw_vals and page_vals.get(k) != raw_vals[k]}
        # Never write a value the enforced write path would refuse. A raw page can carry
        # anything; a closed enum is a corpus invariant that binds every writer, model or not.
        refused = {k: v for k, v in updates.items()
                   if k in closed_enums and v.strip("'\"") not in closed_enums[k]}
        for k, v in refused.items():
            updates.pop(k)
            state["schema_refused"] += 1
            if len(state["refused_examples"]) < MAX_EXAMPLES:
                state["refused_examples"].append(
                    f"{rel}: {k}={v!r} is not in the declared {k} enum — left as-is")
        if not updates:
            continue
        state["divergent_pages"] += 1
        for k in updates:
            state["by_key"][k] = state["by_key"].get(k, 0) + 1
        if len(state["examples"]) < MAX_EXAMPLES:
            state["examples"].append(
                f"{rel}: " + ", ".join(f"{k} {page_vals.get(k, '(absent)')!r}->{v!r}"
                                       for k, v in sorted(updates.items())))
        if apply:
            try:
                text = page.read_text(encoding="utf-8", errors="replace")
                page.write_text(apply_values(text, updates), encoding="utf-8")
            except OSError as exc:
                print(f"repair-carried-provenance: cannot write {rel}: {exc}", file=sys.stderr)
                continue
            state["repaired_pages"] += 1
        if limit and state["divergent_pages"] >= limit:
            break
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", default=str(VAULT))
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--key", action="append", dest="keys",
                    help="restrict to one provenance key (repeatable)")
    args = ap.parse_args(argv)

    keys = tuple(args.keys) if args.keys else tuple(PROVENANCE_KEYS)
    unknown = sorted(set(keys) - set(PROVENANCE_KEYS))
    if unknown:
        print(f"repair-carried-provenance: not carried provenance: {unknown}; "
              f"carried keys are {list(PROVENANCE_KEYS)}", file=sys.stderr)
        return 2

    state = scan(Path(args.vault), keys, args.apply, args.limit)
    if state.get("error"):
        print(f"repair-carried-provenance: {state['error']}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False, "status": "undetectable"}))
        return 1

    mode = "repaired" if args.apply else "would repair"
    print(f"repair-carried-provenance: {state['pages']} page(s), {state['with_raw']} with a raw "
          f"ref; {state['divergent_pages']} diverge from their raw page; {mode} "
          f"{state['repaired_pages'] if args.apply else state['divergent_pages']}")
    for key, n in sorted(state["by_key"].items(), key=lambda kv: -kv[1]):
        print(f"    {key}: {n}")
    for ex in state["examples"]:
        print(f"    - {ex}")

    # A raw ref that does not resolve is not a clean page: the compile's provenance cannot be
    # checked at all, so say so rather than folding it into a pass.
    unresolvable = state["raw_missing"] + state["raw_unreadable"]
    if unresolvable:
        print(f"repair-carried-provenance: {unresolvable} page(s) name a raw file that could "
              f"not be read — their provenance is UNVERIFIED, not clean", file=sys.stderr)
        for ex in state["unresolvable_examples"]:
            print(f"    ? {ex}", file=sys.stderr)
    if not args.apply and state["divergent_pages"]:
        print("repair-carried-provenance: dry run — pass --apply to write", file=sys.stderr)
    print(json.dumps({"wakeAgent": False, **{k: v for k, v in state.items()
                                             if k not in ("examples", "unresolvable_examples",
                                    "refused_examples")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
