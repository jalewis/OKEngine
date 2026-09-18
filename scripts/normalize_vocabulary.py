#!/usr/bin/env python3
"""Normalise a minted vocabulary onto the values a vault's schema actually declares.

WHY THIS EXISTS RATHER THAN A TRANSFORM IN THE CARRY PATH. Ingest provenance is CARRIED from
the raw capture to the source page, never re-derived — that rule is the whole reason
`repair_carried_provenance` exists, after a compile model handed a value it could not use
invented its own and stamped one constant kind on ~30 pages a day for three weeks. Mapping
`cyber-news` to `news` while copying would fix the symptom by re-opening exactly that door:
the page would then hold a value its raw record never said. So the raw record is corrected,
and the carry stays a pure copy.

THE MAP IS SUPPLIED BY THE CALLER, NOT BY THE ENGINE. Whether `intrusion-report` means
`incident-report` is a question about a domain's vocabulary, and the engine ships no domain
knowledge. What the engine does enforce is that the TARGET is a value the vault's composed
schema actually declares — a normalisation that writes a second undeclared value is the same
defect with a different spelling, so it refuses before touching anything.

  normalize_vocabulary.py --map cyber-news=news --map intrusion-report=incident-report
  normalize_vocabulary.py --tree wiki --field source_kind --map blog=post --apply
  normalize_vocabulary.py --field tlp --map amber=AMBER --apply

`--tree raw` (the default) corrects the ingest record; `--tree wiki` corrects pages the compile
agent authored directly, which carry no raw record to be corrected from.

DRY-RUN BY DEFAULT. Pass --apply to write.

Fix the LANE as well: a backfill alone re-drifts on the ingest's next run. `corpus_audit`'s
"Vocabulary the ingest minted" section names the lane that wrote each value.

Env: WIKI_PATH (default /opt/vault)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
import schema_lib  # noqa: E402
import provenance_lib  # noqa: E402  — the SINGLE source of the declared vocabulary

import yaml  # noqa: E402

_FM_RE = re.compile(r"\A---\s*\n(?P<fm>.*?\n)---[ \t]*\n?", re.S)
MAX_EXAMPLES = 10


def parse_map(pairs: list[str]) -> dict[str, str]:
    """`old=new` pairs into a mapping, rejecting a malformed pair rather than dropping it."""
    out: dict[str, str] = {}
    for pair in pairs:
        old, sep, new = pair.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise ValueError(f"--map expects OLD=NEW, got {pair!r}")
        out[old.strip()] = new.strip()
    return out


def load_map_file(path: Path) -> dict[str, str]:
    """A JSON object of OLD -> NEW.

    `--map OLD=NEW` cannot express every value a corpus actually contains: a live vocabulary
    held `report (38-minute read)` and `blog-post ---`, and a shell splits both. A mapping of
    279 entries also has no business on a command line.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    # ValueError, not json.JSONDecodeError, which it subclasses: cosmic-ray's ExceptionReplacer
    # substitutes a class of its own for each caught name, and it cannot do that to a DOTTED
    # name — the handler then evaluates an undefined attribute and the worker dies, which the
    # gate reports as an infrastructure error rather than a survivor. Same behaviour, and the
    # mutant becomes measurable instead of un-runnable.
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read --map-file {path}: {exc}") from exc
    if not isinstance(loaded, dict) or not loaded:
        raise ValueError(f"--map-file {path} must hold a non-empty JSON object of OLD -> NEW")
    out: dict[str, str] = {}
    for old, new in loaded.items():
        if not isinstance(old, str) or not isinstance(new, str) or not old.strip() \
                or not new.strip():
            raise ValueError(f"--map-file {path}: {old!r} -> {new!r} is not a string pair")
        out[old] = new.strip()
    return out


def undeclarable(mapping: dict[str, str], field: str, closed: dict[str, set]) -> list[str]:
    """Targets the governing schema does not declare. Empty when the field is unconstrained."""
    allowed = closed.get(field)
    if allowed is None:
        return []
    return sorted({new for new in mapping.values() if new not in allowed})


def rewrite(text: str, field: str, new: str) -> str | None:
    """Set one top-level scalar in the frontmatter. None when the edit would not be safe.

    Only the field's own line changes, so nothing neighbouring is re-serialised. The result is
    re-parsed before it is returned: a surgical frontmatter edit that strands a continuation
    line has broken live pages before (six source records in one run), and the check costs
    nothing next to re-reading them by hand.
    """
    m = _FM_RE.match(text)
    if not m:
        return None
    block = m.group("fm")
    pattern = re.compile(rf"^{re.escape(field)}:[^\n]*(?:\n[ \t]+[^\n]*)*", re.M)
    updated, count = pattern.subn(lambda _m: f"{field}: {json.dumps(new)}", block, count=1)
    if not count:
        return None
    try:
        parsed = yaml.safe_load(updated)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict) or parsed.get(field) != new:
        return None
    return text[:m.start("fm")] + updated + text[m.end("fm"):]


def scan(vault: Path, field: str, mapping: dict[str, str], apply: bool,
         limit: int | None, tree: str = "raw", schema_aliases: bool = False) -> dict:
    root = vault / tree
    state = {"field": field, "tree": tree, "applied": apply, "captures": 0, "with_field": 0,
             "changed": 0, "by_value": {}, "unmapped": {}, "unwritable": [], "examples": []}
    if not root.is_dir():
        state["error"] = f"no {tree}/ under {vault}"
        return state
    try:
        schema = schema_lib.merged_schema(vault, "sources")
        closed = provenance_lib.closed_enums(schema)
    except Exception as exc:            # a vault with no resolvable schema is not a pass
        state["error"] = f"cannot resolve the governing schema: {exc}"
        return state
    if schema_aliases:
        declared = (schema.get("value_aliases") or {}).get(field) or {}
        if not isinstance(declared, dict):
            state["error"] = f"value_aliases.{field} is not a mapping"
            return state
        # An explicit command-line/file map wins, allowing a reviewed one-off decision without
        # changing the universal alias contract.
        mapping = {**declared, **mapping}
        state["schema_aliases"] = True
    bad = undeclarable(mapping, field, closed)
    if bad:
        state["error"] = (f"refusing to normalise: {', '.join(bad)} is not a declared {field} "
                          f"value on this vault — mapping one undeclared value onto another "
                          f"leaves the corpus exactly as unconformant as it started")
        return state
    allowed = closed.get(field)
    folded_mapping = {str(k).strip().casefold(): v for k, v in mapping.items()}
    for path in sorted(root.rglob("*.md")):
        if limit is not None and state["changed"] >= limit:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue                    # vanished mid-scan (ingest race) — skip, don't crash
        m = _FM_RE.match(text)
        if not m:
            continue
        state["captures"] += 1
        km = re.search(rf"^{re.escape(field)}:[ \t]+(\S.*?)[ \t]*$", m.group("fm"), re.M)
        if not km:
            continue
        state["with_field"] += 1
        current = km.group(1).strip().strip("'\"")
        new = mapping.get(current, folded_mapping.get(current.casefold()))
        if new is None:
            # Not in the map. Report it only when the schema would REFUSE it — an undeclared
            # value the caller did not think to map is the finding this run must not bury.
            if allowed is not None and current not in allowed:
                state["unmapped"][current] = state["unmapped"].get(current, 0) + 1
            continue
        rel = path.relative_to(vault).as_posix()
        updated = rewrite(text, field, new)
        if updated is None:
            if len(state["unwritable"]) < MAX_EXAMPLES:
                state["unwritable"].append(rel)
            continue
        state["changed"] += 1
        state["by_value"][current] = state["by_value"].get(current, 0) + 1
        if len(state["examples"]) < MAX_EXAMPLES:
            state["examples"].append(f"{rel}: {field} {current!r} -> {new!r}")
        if apply:
            try:
                path.write_text(updated, encoding="utf-8")
            except OSError as exc:
                state["changed"] -= 1
                state["by_value"][current] -= 1
                if len(state["unwritable"]) < MAX_EXAMPLES:
                    state["unwritable"].append(f"{rel} ({type(exc).__name__})")
    return state


def report(state: dict) -> str:
    if state.get("error"):
        return f"normalize-vocabulary: ERROR — {state['error']}"
    verb = "rewrote" if state["applied"] else "would rewrite"
    lines = [
        f"normalize-vocabulary [{state['tree']}/]: {state['captures']} page(s) · "
        f"{state['with_field']} carry `{state['field']}` · {verb} {state['changed']}"
        + ("" if state["applied"] else "  (dry run — pass --apply to write)")
    ]
    for value, n in sorted(state["by_value"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {n:6d}  {value}")
    for line in state["examples"]:
        lines.append(f"  {line}")
    if state["unmapped"]:
        label = "UNPARSEABLE / AMBIGUOUS" if state.get("schema_aliases") else "UNMAPPED"
        detail = ("no declared meaning-preserving alias; no value was guessed"
                  if state.get("schema_aliases") else
                  "no mapping given; they remain unconformant")
        lines.append(f"  {label} — {len(state['unmapped'])} undeclared value(s) left as-is "
                     f"({detail}):")
        for value, n in sorted(state["unmapped"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:6d}  {value}")
    if state["unwritable"]:
        lines.append(f"  UNWRITABLE — {len(state['unwritable'])} capture(s) left untouched "
                     f"because the edit would not re-parse: " + ", ".join(state["unwritable"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    parser.add_argument("--tree", choices=("raw", "wiki"), default="raw",
                        help="raw/ corrects the ingest record; wiki/ corrects authored pages")
    parser.add_argument("--field", default="source_kind")
    parser.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                        help="repeatable; the target must be a value the schema declares")
    parser.add_argument("--map-file", type=Path, metavar="PATH",
                        help="JSON object of OLD->NEW; the form that scales, and the only one "
                             "that can carry a value containing a space or a leading dash")
    parser.add_argument("--schema-aliases", action="store_true",
                        help="use value_aliases.<field> from the composed schema; unknown prose is "
                             "reported as unparseable/ambiguous and is never guessed")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if not args.map and not args.map_file and not args.schema_aliases:
        print("normalize-vocabulary: no --map or --map-file given — nothing to do",
              file=sys.stderr)
        return 2
    try:
        mapping = load_map_file(args.map_file) if args.map_file else {}
        mapping.update(parse_map(args.map))
    except ValueError as exc:
        print(f"normalize-vocabulary: {exc}", file=sys.stderr)
        return 2
    state = scan(Path(args.vault), args.field, mapping, args.apply, args.limit, args.tree,
                 args.schema_aliases)
    print(report(state))
    return 1 if state.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
