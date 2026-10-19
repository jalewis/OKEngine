#!/usr/bin/env python3
"""Split the full test suite into disjoint CI shards, then prove the split and combine it.

The full suite is ONE test population executed once (testing.no-duplicate-execution); sharding
only spreads that single execution across parallel runner containers (okengine#898). Three
properties make that safe, and each has a check here rather than a convention:

* **Deterministic, disjoint, complete.** ``plan`` assigns every discovered test file to exactly
  one shard by longest-processing-time packing over committed per-file CI durations. Ties and
  unknown files resolve by path, so every shard computes the same plan independently.
  ``verify_partition`` refuses a plan in which any file sits in no shard or in two.
* **Every collected test ran exactly once.** Each shard records the node ids it collected; shard 1
  also records the full-suite collection. ``verify`` requires the shard collections to be pairwise
  disjoint, to stay inside their assigned files, and to union to exactly the full collection.
* **Coverage is judged on the union, never on a slice.** ``verify`` fails when any shard's
  coverage data, JUnit report or manifest is missing or empty, so the combine job cannot pass on
  partial data. The 100% floors are enforced by coverage.py and check_branch_coverage.py on the
  combined data in the combine job.
"""
from __future__ import annotations

import argparse
from importlib import metadata
import json
import sys
import xml.etree.ElementTree as ET  # nosec B405 - parses this pipeline's own pytest JUnit output
from pathlib import Path

TEST_ROOT = "tests"
DEFAULT_DURATIONS = Path(__file__).resolve().parent / "test-durations.json"
SHARD_FILES = ("files.txt", "layers.json", "junit.xml", "coverage.data", "manifest.json")


class ShardError(Exception):
    """A split, shard artifact or collection that cannot be trusted."""


def discover(root: Path) -> list[str]:
    """Every file pytest collects from ``tests/`` under its default ``python_files`` patterns."""
    base = root / TEST_ROOT
    found = {p for pattern in ("test_*.py", "*_test.py") for p in base.rglob(pattern)}
    files = sorted(p.relative_to(root).as_posix() for p in found if "__pycache__" not in p.parts)
    if not files:
        raise ShardError(f"no test files found under {base}")
    spaced = [f for f in files if any(ch.isspace() for ch in f)]
    if spaced:
        raise ShardError(f"test paths must not contain whitespace: {spaced}")
    return files


def load_durations(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    durations = data["files"]
    if not isinstance(durations, dict) or not durations:
        raise ShardError(f"{path}: 'files' must be a non-empty mapping of path -> seconds")
    return {str(name): float(seconds) for name, seconds in durations.items()}


def _weights(files: list[str], durations: dict[str, float]) -> dict[str, float]:
    known = sorted(durations[f] for f in files if f in durations)
    # A file with no recorded duration (new since the last re-measure) weighs the median file, so
    # a burst of new files spreads out instead of all landing on whichever shard is lightest.
    fallback = known[len(known) // 2] if known else 1.0
    return {f: durations.get(f, fallback) for f in files}


def plan(files: list[str], total: int, durations: dict[str, float]) -> list[list[str]]:
    """Assign each file to one of ``total`` shards, heaviest first onto the lightest shard."""
    if total < 1:
        raise ShardError(f"shard total must be >= 1, got {total}")
    weights = _weights(files, durations)
    loads = [0.0] * total
    shards: list[list[str]] = [[] for _ in range(total)]
    for name in sorted(files, key=lambda f: (-weights[f], f)):
        target = min(range(total), key=lambda i: (loads[i], i))
        shards[target].append(name)
        loads[target] += weights[name]
    result = [sorted(shard) for shard in shards]
    verify_partition(files, result)
    return result


def verify_partition(files: list[str], shards: list[list[str]]) -> None:
    """Every file in exactly one shard, and no shard holding a file outside the population."""
    owners: dict[str, list[int]] = {}
    for index, shard in enumerate(shards, start=1):
        for name in shard:
            owners.setdefault(name, []).append(index)
    missing = sorted(set(files) - set(owners))
    doubled = {name: idx for name, idx in sorted(owners.items()) if len(idx) > 1}
    unknown = sorted(set(owners) - set(files))
    empty = [index for index, shard in enumerate(shards, start=1) if not shard]
    problems = []
    if missing:
        problems.append(f"assigned to no shard: {missing}")
    if doubled:
        problems.append(f"assigned to more than one shard: {doubled}")
    if unknown:
        problems.append(f"assigned but not part of the suite: {unknown}")
    if empty:
        problems.append(f"shards with no files: {empty}")
    if problems:
        raise ShardError("invalid test split -- " + "; ".join(problems))


def _collected(path: Path) -> set[str]:
    """Node ids from a ``--test-layer-report`` file (conftest writes every collected item)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    nodeids = [node for nodes in data["layers"].values() for node in nodes]
    if len(nodeids) != int(data["total"]) or len(set(nodeids)) != len(nodeids):
        raise ShardError(f"{path}: layer report total does not match its node ids")
    if not nodeids:
        raise ShardError(f"{path}: collected zero tests")
    return set(nodeids)


def _node_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def verify(shard_dir: Path, total: int, files: list[str]) -> dict:
    """Prove the shards' artifacts describe one complete, exactly-once execution."""
    present = sorted(p.name for p in shard_dir.iterdir() if p.is_dir()) if shard_dir.is_dir() else []
    expected = [str(i) for i in range(1, total + 1)]
    if sorted(present) != sorted(expected):
        raise ShardError(f"expected shard directories {expected} in {shard_dir}, found {present}")
    for index in expected:
        for name in SHARD_FILES:
            item = shard_dir / index / name
            if not item.is_file() or item.stat().st_size == 0:
                raise ShardError(f"shard {index}: {name} is missing or empty -- refusing to "
                                 "judge coverage or completeness on partial data")
    manifests = {i: json.loads((shard_dir / i / "manifest.json").read_text(encoding="utf-8"))
                 for i in expected}
    for index, manifest in manifests.items():
        if (manifest.get("index"), manifest.get("total")) != (int(index), total):
            raise ShardError(f"shard {index}: manifest claims shard "
                             f"{manifest.get('index')}/{manifest.get('total')}, expected "
                             f"{index}/{total}")
    versions = {m.get("coverage_version") for m in manifests.values()}
    if len(versions) != 1 or None in versions:
        raise ShardError(f"shards measured with different coverage.py versions: {sorted(map(str, versions))}")

    assigned = [(shard_dir / i / "files.txt").read_text(encoding="utf-8").split() for i in expected]
    verify_partition(files, assigned)

    full_path = shard_dir / "1" / "collection.json"
    if not full_path.is_file():
        raise ShardError("shard 1: collection.json (the full-suite collection) is missing")
    full = _collected(full_path)
    # The file partition above plus "no shard strays outside its own files" makes the shard
    # collections pairwise disjoint by construction: a node id belongs to exactly one file.
    seen: set[str] = set()
    for index, own_files in zip(expected, assigned):
        nodes = _collected(shard_dir / index / "layers.json")
        strays = sorted(n for n in nodes if _node_file(n) not in set(own_files))
        if strays:
            raise ShardError(f"shard {index} collected tests outside its files: {strays[:10]}")
        seen |= nodes
    never = sorted(full - seen)
    extra = sorted(seen - full)
    if never or extra:
        raise ShardError(f"shard collections differ from the full suite: {len(never)} never run "
                         f"{never[:10]}, {len(extra)} not in the full collection {extra[:10]}")
    return {"shards": total, "files": len(files), "tests": len(full),
            "coverage_version": versions.pop()}


def merge_junit(reports: list[Path], out: Path) -> dict:
    """One JUnit document holding every shard's suites, with totals summed on the root."""
    merged = ET.Element("testsuites", name="pytest tests (sharded)")
    totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    elapsed = 0.0
    for index, report in enumerate(reports, start=1):
        suites = list(ET.parse(report).getroot().iter("testsuite"))  # nosec B314
        if not suites:
            raise ShardError(f"{report}: no <testsuite> element")
        for suite in suites:
            suite.set("name", f"{suite.get('name', 'pytest')} shard {index}/{len(reports)}")
            for key in totals:
                totals[key] += int(suite.get(key, "0"))
            elapsed += float(suite.get("time", "0"))
            merged.append(suite)
    for key, value in totals.items():
        merged.set(key, str(value))
    merged.set("time", f"{elapsed:.3f}")
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(merged).write(out, encoding="utf-8", xml_declaration=True)
    return totals


def durations_from_junit(report: Path, files: list[str]) -> dict[str, float]:
    """Per-file seconds from a pytest JUnit report (classname = dotted file path [+ class])."""
    known = set(files)
    per_file: dict[str, float] = {}
    for case in ET.parse(report).getroot().iter("testcase"):  # nosec B314
        parts = case.get("classname", "").split(".")
        for cut in range(len(parts), 0, -1):
            candidate = "/".join(parts[:cut]) + ".py"
            if candidate in known:
                per_file[candidate] = per_file.get(candidate, 0.0) + float(case.get("time") or 0)
                break
    return {name: round(per_file[name], 3) for name in sorted(per_file)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select", help="write one shard's test files, one per line")
    select.add_argument("--index", type=int, required=True, help="1-based, as CI_NODE_INDEX")
    select.add_argument("--total", type=int, required=True)
    select.add_argument("--out", type=Path, required=True)
    check = sub.add_parser("verify", help="prove shard artifacts form one complete execution")
    check.add_argument("--total", type=int, required=True)
    check.add_argument("--shard-dir", type=Path, required=True)
    junit = sub.add_parser("merge-junit", help="merge shard JUnit reports")
    junit.add_argument("--out", type=Path, required=True)
    junit.add_argument("reports", type=Path, nargs="+")
    note = sub.add_parser("manifest", help="record which shard produced these artifacts")
    note.add_argument("--index", type=int, required=True)
    note.add_argument("--total", type=int, required=True)
    note.add_argument("--out", type=Path, required=True)
    record = sub.add_parser("record-durations", help="rewrite the durations file from CI JUnit")
    record.add_argument("--junit", type=Path, required=True)
    record.add_argument("--source", required=True, help="the CI job id(s) the JUnit came from")
    args = parser.parse_args(argv)
    try:
        if args.command == "select":
            if not 1 <= args.index <= args.total:
                raise ShardError(f"shard index {args.index} outside 1..{args.total}")
            files = discover(args.root)
            shards = plan(files, args.total, load_durations(args.durations))
            mine = shards[args.index - 1]
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text("".join(f"{name}\n" for name in mine), encoding="utf-8")
            print(f"shard {args.index}/{args.total}: {len(mine)} of {len(files)} test files")
        elif args.command == "verify":
            summary = verify(args.shard_dir, args.total, discover(args.root))
            print("suite shards: " + json.dumps(summary, sort_keys=True))
        elif args.command == "manifest":
            payload = {"index": args.index, "total": args.total,
                       "coverage_version": metadata.version("coverage")}
            args.out.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            print("shard manifest: " + json.dumps(payload, sort_keys=True))
        elif args.command == "merge-junit":
            totals = merge_junit(args.reports, args.out)
            print(f"merged {len(args.reports)} JUnit reports: " + json.dumps(totals, sort_keys=True))
        else:
            measured = durations_from_junit(args.junit, discover(args.root))
            payload = {"source": args.source, "files": measured}
            args.durations.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                                      encoding="utf-8")
            print(f"recorded {len(measured)} file durations from {args.source}")
    except (ShardError, metadata.PackageNotFoundError, OSError, KeyError, ValueError, ET.ParseError) as exc:
        print(f"suite shards: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
