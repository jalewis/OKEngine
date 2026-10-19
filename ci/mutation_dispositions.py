#!/usr/bin/env python3
"""Keep mutation/survivors.json pointing at the mutants its dispositions were written for.

A disposition is keyed by Cosmic Ray's fingerprint, ``module:line:column:operator:occurrence``.
That key is a POSITION, so any edit above a recorded survivor moves the mutant off its key. The
diff-scoped ``mutation-changed`` lane never re-scores the moved, untouched lines, so the first
lane to notice was the nightly critical campaign, which then failed on "survivor lacks
owner/disposition" for mutants that had been reviewed (okengine#1047: write_server.py's
recorded 552 had become 553 in pipeline 12608 and 554 in 12756).

``check`` names every exact key and pattern that no longer names a mutation site in the current
source. It needs only Cosmic Ray's own enumeration, not a campaign, so it runs on every merge
request and catches the drift at the change that causes it. ``rekey --base REV`` repairs the
drift: each stale entry is read against the module at REV, its line is carried through the
unchanged lines between REV and the working tree, and it is rewritten to the site that now sits
there. An entry whose own line changed cannot be carried; it is reported for review instead of
being guessed.
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import functools
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

DISPOSITIONS = Path("mutation/survivors.json")
WILDCARDS = ("*", "?", "[")


@functools.lru_cache(maxsize=256)
def mutation_sites(source: str) -> tuple[tuple[int, int, str, int], ...]:
    """Every ``(line, column, operator, occurrence)`` ``cosmic-ray init`` generates for source.

    The enumeration init performs: every registered operator that takes no arguments, over every
    AST node, numbering occurrences per operator in walk order.
    """
    from cosmic_ray import plugins
    from cosmic_ray.ast import ast_nodes, get_ast

    nodes = list(ast_nodes(get_ast(source)))
    sites = []
    for name in plugins.operator_names():
        operator_class = plugins.get_operator(name)
        if operator_class.arguments():
            continue
        operator = operator_class()
        occurrence = 0
        for node in nodes:
            for start, _end in operator.mutation_positions(node):
                sites.append((start[0], start[1], name, occurrence))
                occurrence += 1
    return tuple(sites)


def site_key(module: str, site: tuple[int, int, str, int]) -> str:
    line, column, operator, occurrence = site
    return f"{module}:{line}:{column}:{operator}:{occurrence}"


def split_key(key: str) -> tuple[str, str, str, str, str] | None:
    parts = key.split(":")
    if len(parts) != 5:
        return None
    module, line, column, operator, occurrence = parts
    return module, line, column, operator, occurrence


def entries(doc: dict) -> list[str]:
    """Every exact key and every pattern string in a survivors document, in file order."""
    return list(doc.get("survivors", {})) + [rule["pattern"] for rule in doc.get("patterns", [])]


def has_wildcard(text: str) -> bool:
    return any(char in text for char in WILDCARDS)


def stale_entries(repo: Path, doc: dict) -> list[tuple[str, str]]:
    """``(entry, reason)`` for every entry that names no mutation site in the current source.

    A pattern is matched like the gate matches it (fnmatch over the whole key, so `module:*`
    covers a file); it is stale when it matches no current site. A pattern whose module part is
    itself a wildcard spans files and is not checked here.
    """
    stale: list[tuple[str, str]] = []
    cache: dict[str, set[str] | None] = {}
    for entry in entries(doc):
        module = entry.split(":", 1)[0]
        if has_wildcard(module):
            continue
        if not has_wildcard(entry) and split_key(entry) is None:
            stale.append((entry, "not a module:line:column:operator:occurrence key"))
            continue
        if module not in cache:
            path = repo / module
            cache[module] = ({site_key(module, site) for site in mutation_sites(
                path.read_text(encoding="utf-8"))} if path.is_file() else None)
        keys = cache[module]
        if keys is None:
            stale.append((entry, "module does not exist"))
        elif not (any(fnmatch.fnmatchcase(key, entry) for key in keys)
                  if has_wildcard(entry) else entry in keys):
            stale.append((entry, "names no mutation site in the current source"))
    return stale


@functools.lru_cache(maxsize=256)
def line_map(old: str, new: str) -> dict[int, int]:
    """1-based old line -> new line, for the lines the edit left unchanged.

    Unchanged runs come from a line diff. A line the diff cannot pair because it MOVED is still
    carried when its exact text occurs once in the old source and once in the new one.
    """
    old_lines, new_lines = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    mapping: dict[int, int] = {}
    for old_start, new_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            mapping[old_start + offset + 1] = new_start + offset + 1
    old_count, new_count = Counter(old_lines), Counter(new_lines)
    # A unique line the diff left unpaired cannot have its new twin paired to another old line:
    # the diff pairs only equal lines, and the old text occurs once.
    new_index = {text: number for number, text in enumerate(new_lines, 1)}
    for number, text in enumerate(old_lines, 1):
        if number not in mapping and old_count[text] == 1 and new_count[text] == 1:
            mapping[number] = new_index[text]
    return mapping


def rekey(entry: str, old: str, new: str) -> str | None:
    """`entry` (valid against `old`) carried to `new`, or None when it cannot be carried.

    The mutated line must be unchanged (`line_map`). An exact key must find the same number of
    sites for its column and operator on the carried line, and takes the site of the same rank
    there with its new occurrence. A pattern keeps every other field as written and must match
    as many sites on the carried line as it matched on the old one.
    """
    parts = split_key(entry)
    if parts is None or not parts[1].isdigit() or has_wildcard(parts[0]):
        return None
    module, line_text, column_text, operator, occurrence_text = parts
    line = int(line_text)
    target = line_map(old, new).get(line)
    if target is None:
        return None
    old_sites, new_sites = mutation_sites(old), mutation_sites(new)
    if has_wildcard(entry):
        carried = ":".join((module, str(target), column_text, operator, occurrence_text))
        before = sum(fnmatch.fnmatchcase(site_key(module, site), entry) for site in old_sites)
        after = sum(fnmatch.fnmatchcase(site_key(module, site), carried) for site in new_sites)
        return carried if after and after == before else None
    if not column_text.isdigit() or not occurrence_text.isdigit():
        return None
    column, occurrence = int(column_text), int(occurrence_text)
    old_group = sorted(site[3] for site in old_sites if site[:3] == (line, column, operator))
    new_group = sorted(site[3] for site in new_sites if site[:3] == (target, column, operator))
    if occurrence not in old_group or len(new_group) != len(old_group):
        return None
    return site_key(module, (target, column, operator, new_group[old_group.index(occurrence)]))


def git_show(repo: Path, revision: str, module: str) -> str | None:
    result = subprocess.run(["git", "show", f"{revision}:{module}"], cwd=repo,
                            capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def rekey_document(repo: Path, text: str, base: str) -> tuple[str, list[tuple[str, str]],
                                                               list[tuple[str, str]]]:
    """Rewrite each stale entry of a survivors file in place; return (text, moved, unresolved).

    Keys are replaced as quoted strings so the rest of the file -- order, indentation and every
    rationale -- is left byte for byte as it was.
    """
    doc = json.loads(text)
    existing = set(entries(doc))
    moved: list[tuple[str, str]] = []
    unresolved: list[tuple[str, str]] = []
    for entry, reason in stale_entries(repo, doc):
        parts = split_key(entry)
        old = git_show(repo, base, parts[0]) if parts else None
        current = repo / parts[0] if parts else None
        new_key = None
        if old is not None and current is not None and current.is_file():
            new_key = rekey(entry, old, current.read_text(encoding="utf-8"))
        if new_key is None:
            unresolved.append((entry, reason))
        elif new_key in existing:
            unresolved.append((entry, f"carries to {new_key}, which already has an entry"))
        else:
            text = text.replace(json.dumps(entry), json.dumps(new_key), 1)
            existing.add(new_key)
            moved.append((entry, new_key))
    return text, moved, unresolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("command", choices=("check", "rekey"))
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--dispositions", type=Path, default=DISPOSITIONS)
    parser.add_argument("--base", help="rekey: the revision the stale entries are valid at")
    args = parser.parse_args(argv)
    path = args.repo / args.dispositions
    text = path.read_text(encoding="utf-8")
    if args.command == "check":
        stale = stale_entries(args.repo, json.loads(text))
        for entry, reason in stale:
            print(f"STALE: {entry}: {reason}", file=sys.stderr)
        if stale:
            print(f"{len(stale)} disposition(s) no longer name their mutant. Carry them forward "
                  f"with: python ci/mutation_dispositions.py rekey --base <merge base>",
                  file=sys.stderr)
            return 1
        print("mutation dispositions: every entry names a current mutation site")
        return 0
    if not args.base:
        parser.error("rekey needs --base")
    text, moved, unresolved = rekey_document(args.repo, text, args.base)
    path.write_text(text, encoding="utf-8")
    for old_key, new_key in moved:
        print(f"moved: {old_key} -> {new_key}")
    for entry, reason in unresolved:
        print(f"UNRESOLVED: {entry}: {reason}", file=sys.stderr)
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
