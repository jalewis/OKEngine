#!/usr/bin/env python3
"""Plan/apply repair of legacy frontmatter SHAPES to the write path's canonical shape (okengine#994 [19]).

Two selectable classes (``--classes scalars,wikilinks``):

* ``scalars``   — a schema-declared list field stored as a scalar string (``tags: a, b``) becomes
  the list the write path makes of it (``[a, b]``). A field declared ``prose-list`` becomes the one
  item ``[whole]``; a ``list`` field splits on its commas, but never at a comma inside brackets, a
  date or a number (``okengine.list_scalars``, okengine#1005), so prose is not shredded.
* ``wikilinks`` — a ``[[target]]`` frontmatter value, alone or inside a (possibly YAML-mangled,
  nested) list, becomes the plain path ``target``.

The canonical value is never computed here: it is the enforced write path's own ``_coerce_fm``
(loaded from ``okengine-mcp/write_server.py``), applied until it is stable. This lane writes files
directly — it is a ``no_agent`` maintainer, so it bypasses the MCP SERVER — but it holds the same
INVARIANTS, and adds a reshape-only proof of its own:

* a field is rewritten only when the new value is the canonical shape of the SAME data
  (``_reshape_only``): the items are the stored items, wikilink-stripped, in order — a scalar's
  items are its delimited parts (``okengine.list_scalars``) or, for a ``prose-list``, the whole
  string, so a split that cuts an item in half is a data change, never a reshape. A value whose
  canonical form would drop data (``[[x|Display]]``, ``[[x#anchor]]``, a duplicate item) is skipped
  and reported, never rewritten;
* ``_coerce_fm`` of the new value is the new value (a fixpoint), so a second plan shows 0;
* an attested page (#980 trust assertions) is skipped whole, and a #980 operator-owned field on any
  page is skipped: both stay byte for byte;
* immutable fields (``id``, ``created``, ``created_by``, ``discovered_by``) get only the canonical
  shape of the same value, which the reshape-only proof already requires;
* a tombstoned page and a reserved file are skipped, as the write path refuses them;
* only the bytes of a repaired key change: every other key's text is left untouched, and the
  rewritten frontmatter is re-parsed and compared with the stored one before it is written.

Dry-run is the default. ``--verify`` exits 1 while a repair of the selected classes remains.
``--apply`` writes inside one corpus transaction (the fence the MCP write path takes, journaling
the touched pages), re-planning under the fence, and refuses unless the deployment's engine
runtime stamp is at least ``FIXED_IN``: the write path that will serve the repaired pages must be
the one whose canonicalization this lane applied.
"""
from __future__ import annotations

import argparse
import collections
import copy
import datetime
import importlib.util
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml
from yaml import YAMLError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_package  # noqa: E402  — `okengine` importable when this script runs by path
engine_package.ensure()
from okengine import attestation, corpus_transaction  # noqa: E402
from okengine.list_scalars import split_list_scalar  # noqa: E402
from okengine.corpus_transaction import CorpusLockTimeout  # noqa: E402

CLASSES = ("scalars", "wikilinks")
#: The first engine release whose write path stores a canonical repair (okengine#994 [19]) and
#: coerces a scalar list field by the shared okengine#1005 rule (`prose-list` fields whole, no split
#: inside brackets/dates/numbers): an older gateway would re-shred what a repair kept whole.
FIXED_IN = (0, 15, 7)
_IMMUTABLE = ("id", "created", "created_by", "discovered_by")
_PLAIN_LINK = re.compile(r"\[\[([^\[\]|#]+)\]\]")
_RELEASE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_MAX_COERCE_PASSES = 4
#: The write path's frontmatter split (`_FM`), read on the file's raw text: `\r\n` line endings are
#: kept, so a repair can splice bytes into the file exactly as it is stored (the write path sees the
#: same split after universal-newline reading).
_FM_RAW = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)---(.*)\Z", re.S)


class RepairRefused(RuntimeError):
    pass


def _engine_dirs() -> list[Path]:
    here = Path(__file__).resolve()
    return [here.parents[2] / "okengine-mcp", Path("/opt/hermes/okengine-mcp")]


def load_write_path(vault: Path):
    """The enforced write path, bound to `vault`, so its canonicalization is the one applied."""
    os.environ["WIKI_PATH"] = str(vault)
    source = next((d / "write_server.py" for d in _engine_dirs() if (d / "write_server.py").is_file()),
                  None)
    if source is None:
        raise RepairRefused("cannot locate okengine-mcp/write_server.py")
    name = f"okengine_write_path_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "_stored_repairs"):
        raise RepairRefused(f"{source} predates okengine#994 [19]; refusing to canonicalize with it")
    return module


def _canonical_json(value: Any) -> str:
    """Type-strict, sharing-insensitive identity: YAML anchors in the stored file are not data.
    A mapping is ordered by its keys' own canonical text, so keys of mixed types (`null` beside a
    string, which YAML allows) still compare instead of failing to sort."""
    return json.dumps(_tagged(value), default=lambda v: f"!{type(v).__qualname__} {v!r}")


def _tagged(value: Any) -> Any:
    if isinstance(value, dict):
        return {"!map": sorted([_canonical_json(k), _tagged(v)] for k, v in value.items())}
    if isinstance(value, list):
        return [_tagged(v) for v in value]
    return value


def same(left: Any, right: Any) -> bool:
    return _canonical_json(left) == _canonical_json(right)


def _atoms(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _atoms(item)
    else:
        yield value


def _plain(atom: Any) -> Any:
    if not isinstance(atom, str):
        return atom
    text = atom.strip()
    match = _PLAIN_LINK.fullmatch(text)
    return match.group(1).strip() if match else text


def reshape_only(old: Any, new: Any) -> bool:
    """True when `new` holds exactly the data of `old`: its items, wikilink-stripped, in order.

    A scalar list field's items are either its delimited parts (``split_list_scalar``: commas
    outside brackets, dates and numbers -- the write path's #196/#1005 rule) or, for a declared
    ``prose-list`` field, the whole string as one item; both are the same data. Empty items carry
    no data. Anything else -- a dropped display text or anchor, a collapsed duplicate, a changed
    type, a comma split that cuts an item in half -- is a data change, which this lane never makes."""
    if isinstance(old, str) and isinstance(new, list):
        readings = (split_list_scalar(old), [old])
    else:
        readings = (list(_atoms(old)),)
    for atoms in readings:
        expected = [a for a in (_plain(x) for x in atoms) if a != ""]
        if isinstance(new, list) and same(expected, new):
            return True
        if not isinstance(new, list) and isinstance(old, str) and same(expected, [new]):
            return True
    return False


def canonical(write_path, page: Path, key: str, value: Any) -> Optional[Any]:
    """The write path's canonical shape of `value`: `_coerce_fm` applied until stable, or None."""
    current = value
    for _ in range(_MAX_COERCE_PASSES):
        coerced = write_path._coerce_fm({key: current}, page)
        if coerced is None:
            return None
        if same(coerced.get(key), current):
            return current
        current = coerced.get(key)
    return None


def _field_class(old: Any, new: Any) -> str:
    return "scalars" if isinstance(old, str) and isinstance(new, list) else "wikilinks"


def _dump(key: str, value: Any) -> str:
    return yaml.safe_dump({key: value}, allow_unicode=True, default_flow_style=False).rstrip("\n")


def _spans(fm_text: str) -> dict[str, tuple[int, int]]:
    node = yaml.compose(fm_text)
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {k.value: (k.start_mark.index, v.end_mark.index) for k, v in node.value
            if isinstance(k, yaml.ScalarNode)}


def _line_ending(text: str, start: int) -> str:
    """The ending of the line that holds `start`: a mixed-ending file keeps each line's own."""
    newline = text.find("\n", start)
    return "\r\n" if newline > 0 and text[newline - 1] == "\r" else "\n"


def _rewrite(text: str, fm_match, repairs: dict[str, Any]) -> tuple[str, list]:
    """`text` with only the repaired keys' YAML replaced, and the edits as (start, end, new text)
    in `text`'s own offsets. Every other byte is kept; the new lines use the key line's ending."""
    offset = fm_match.start(1)
    spans = _spans(fm_match.group(1))
    edits = []
    for key in sorted(repairs, key=spans.get):
        start, end = spans[key][0] + offset, spans[key][1] + offset
        original = text[start:end]
        tail = original[len(original.rstrip()):]
        dumped = _dump(key, repairs[key]).replace("\n", _line_ending(text, start))
        edits.append((start, end, dumped + tail))
    pieces, cursor = [], 0
    for start, end, new in edits:
        pieces += [text[cursor:start], new]
        cursor = end
    return "".join(pieces) + text[cursor:], edits


def plan_page(write_path, wiki: Path, page: Path, classes: tuple[str, ...]) -> dict:
    """One page's plan: the repairs it gets, the fields skipped (and why), and the new text."""
    rel = page.relative_to(wiki).as_posix()
    result: dict = {"page": rel, "repairs": {}, "skipped": [], "deferred": [], "page_skip": None,
                    "text": None}
    try:
        with page.open(encoding="utf-8", newline="") as handle:  # raw: keep `\r\n` as stored
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        result["page_skip"] = "unreadable"
        return result
    match = _FM_RAW.match(text)
    try:
        fm = yaml.safe_load(match.group(1)) if match else None
    except YAMLError:
        fm = None
    if not isinstance(fm, dict) or not fm:
        return result
    if str(fm.get("status") or "").strip().lower() == "tombstoned":
        result["page_skip"] = "tombstoned"
        return result
    if write_path._reserved_refuse(page):
        result["page_skip"] = "reserved"
        return result
    # One write-path pass over the whole frontmatter finds the candidates (its canonicalization is
    # per key); only a field it changes is then driven to a fixpoint on its own.
    coerced = write_path._coerce_fm(copy.deepcopy(fm), page)
    if coerced is None:
        result["page_skip"] = "refused-by-write-path"
        return result
    changes = {key: (old, canonical(write_path, page, key, old)) for key, old in fm.items()
               if isinstance(key, str) and not same(coerced.get(key), old)}
    if not changes:
        return result
    if attestation.carries_attestation(fm):
        result["page_skip"] = "attested"
        result["skipped"] = [(key, "attested-page") for key in sorted(changes)]
        return result
    spans = _spans(match.group(1))
    for key, (old, new) in sorted(changes.items()):
        if new is None:
            result["skipped"].append((key, "unstable-canonical"))
        elif key in attestation.OPERATOR_FIELDS:
            result["skipped"].append((key, "operator-owned"))
        elif not reshape_only(old, new):
            result["skipped"].append((key, "data-change"))
        elif key not in spans:
            result["skipped"].append((key, "unlocated"))
        elif "#" in match.group(1)[slice(*spans[key])] and "#" not in _canonical_json(old):
            result["skipped"].append((key, "comment"))
        elif _field_class(old, new) not in classes:
            result["deferred"].append((key, _field_class(old, new)))
        else:
            result["repairs"][key] = (old, new, _field_class(old, new))
    if result["repairs"]:
        new_text, edits = _rewrite(text, match, {k: v[1] for k, v in result["repairs"].items()})
        if _verified(fm, text, new_text, result["repairs"], edits):
            result["text"] = new_text
        else:
            result["skipped"] += [(key, "verification") for key in sorted(result["repairs"])]
            result["repairs"] = {}
    return result


def _verified(stored: dict, text: str, new_text: str, repairs: dict, edits: list) -> bool:
    """Every byte outside the edited spans is the stored byte, and the rewritten frontmatter parses
    and changes exactly the repaired keys, to those values."""
    if not _outside_edits_unchanged(text, new_text, edits):
        return False
    match = _FM_RAW.match(new_text)
    try:
        fm = yaml.safe_load(match.group(1)) if match else None
    except YAMLError:
        return False
    if not isinstance(fm, dict) or list(fm) != list(stored):
        return False
    return all(same(fm[k], repairs[k][1]) if k in repairs else same(fm[k], stored[k]) for k in fm)


def _outside_edits_unchanged(text: str, new_text: str, edits: list) -> bool:
    """`new_text` is `text` with each (start, end, replacement) edit applied and nothing else."""
    position, cursor = 0, 0
    for start, end, new in edits:
        kept = text[cursor:start]
        if new_text[position:position + len(kept)] != kept:
            return False
        position += len(kept) + len(new)
        cursor = end
    return new_text[position:] == text[cursor:]


def _pages(wiki: Path):
    # rglob-ok: a whole-vault shape repair reads every page, like the schema-drift audit
    for page in sorted(wiki.rglob("*.md")):
        if page.is_file():
            yield page


def plan(write_path, vault: Path, classes: tuple[str, ...]) -> list[dict]:
    wiki = vault / "wiki"
    return [p for p in (plan_page(write_path, wiki, page, classes) for page in _pages(wiki))
            if p["repairs"] or p["skipped"] or p["deferred"]]


def summarize(plans: list[dict]) -> dict:
    by_class: dict = collections.Counter()
    pages_by_class: dict = collections.Counter()
    by_field: dict = collections.defaultdict(collections.Counter)
    skipped: dict = collections.Counter()
    deferred: dict = collections.Counter()
    for page in plans:
        classes = set()
        for key, (_old, _new, cls) in page["repairs"].items():
            by_class[cls] += 1
            by_field[cls][key] += 1
            classes.add(cls)
        for cls in classes:
            pages_by_class[cls] += 1
        for _key, reason in page["skipped"]:
            skipped[reason] += 1
        for _key, cls in page["deferred"]:
            deferred[cls] += 1
    return {"pages": dict(pages_by_class), "fields": dict(by_class),
            "by_field": {cls: dict(c) for cls, c in by_field.items()},
            "skipped": dict(skipped), "deferred": dict(deferred),
            "repair_pages": sum(1 for p in plans if p["repairs"])}


def runtime_release(vault: Path) -> Optional[tuple[int, int, int]]:
    """The engine release the deployment's gateway runs (its ensure-runtime stamp), or None."""
    for stamp in (vault / ".hermes-data" / "engine-runtime.yaml",
                  Path(os.environ.get("HERMES_HOME") or "/opt/data") / "engine-runtime.yaml"):
        try:
            data = yaml.safe_load(stamp.read_text(encoding="utf-8")) or {}
        except (OSError, YAMLError):
            continue
        match = _RELEASE.match(str(data.get("engine_release") or "")) if isinstance(data, dict) else None
        if match:
            return tuple(int(x) for x in match.groups())
    return None


def require_fixed_runtime(vault: Path) -> None:
    release = runtime_release(vault)
    if release is None:
        raise RepairRefused("no engine runtime stamp (.hermes-data/engine-runtime.yaml): cannot "
                            "prove the gateway runs the okengine#994 write path; refusing --apply")
    if release < FIXED_IN:
        raise RepairRefused(
            f"the gateway runs v{'.'.join(map(str, release))}, before "
            f"v{'.'.join(map(str, FIXED_IN))}: roll the fixed image first; refusing --apply")


def apply(write_path, vault: Path, classes: tuple[str, ...]) -> list[dict]:
    """Re-plan and write under one corpus transaction, so a concurrent gateway write serializes."""
    require_fixed_runtime(vault)
    wiki = vault / "wiki"
    with corpus_transaction.mutation(vault, writer="repair_legacy_shapes",
                                     operation="legacy-shape-repair", tracking="touched"):
        plans = plan(write_path, vault, classes)
        for page in plans:
            if page["text"] is None:
                continue
            path = wiki / page["page"]
            corpus_transaction.touch(path)
            with path.open("w", encoding="utf-8", newline="") as handle:  # no newline translation
                handle.write(page["text"])
    return plans


def _parse_classes(value: str) -> tuple[str, ...]:
    chosen = tuple(sorted({c.strip() for c in value.split(",") if c.strip()}))
    unknown = sorted(set(chosen) - set(CLASSES))
    if unknown or not chosen:
        raise argparse.ArgumentTypeError(f"--classes takes {','.join(CLASSES)}; got {value!r}")
    return chosen


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault", type=Path, default=Path(os.environ.get("WIKI_PATH") or "/opt/vault"))
    parser.add_argument("--classes", type=_parse_classes, default=CLASSES)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list", action="store_true", help="print every planned page")
    args = parser.parse_args(argv)
    vault = args.vault.resolve()
    try:
        write_path = load_write_path(vault)
        plans = (apply if args.apply else plan)(write_path, vault, args.classes)
    except (RepairRefused, CorpusLockTimeout) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    summary = {"deployment": str(vault), "classes": list(args.classes),
               "mode": "apply" if args.apply else "verify" if args.verify else "plan",
               "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               **summarize(plans)}
    if args.list:
        for page in plans:
            for key, (old, new, cls) in page["repairs"].items():
                print(f"{'REPAIRED' if args.apply else 'WOULD REPAIR'} [{cls}] {page['page']} "
                      f"{key}: {old!r} -> {new!r}")
            for key, reason in page["skipped"]:
                print(f"SKIP [{reason}] {page['page']} {key}")
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else
          f"legacy-shape-repair ({summary['mode']}, {','.join(args.classes)}): "
          f"{summary['repair_pages']} page(s); fields {summary['fields']}; "
          f"skipped {summary['skipped']}; deferred {summary['deferred']}")
    return 1 if args.verify and summary["repair_pages"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
