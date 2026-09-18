#!/usr/bin/env python3
"""Verify and combine independently-run mutation shards."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import mutation_gate as gate


def combine(manifest: dict, fragments: list[dict], *, critical_only: bool = False) -> dict:
    targets, _ = gate.select_targets(manifest, "full", set())
    runtime_costs = manifest.get("runtime_costs", {})
    worker_seconds = runtime_costs.get("worker_seconds", {}) \
        if isinstance(runtime_costs, dict) else {}
    if not isinstance(worker_seconds, dict):
        raise ValueError("mutation manifest runtime_costs.worker_seconds must be a mapping")
    targets = [
        {**target, "estimated_worker_seconds": worker_seconds.get(target["path"])}
        for target in targets
    ]
    if critical_only:
        targets = [target for target in targets if target.get("critical")]
    if not fragments:
        raise ValueError("no mutation shard summaries supplied")
    expected_hash = gate.manifest_identity(manifest)
    counts = {fragment.get("shard", {}).get("count") for fragment in fragments}
    if len(counts) != 1:
        raise ValueError("mutation shards disagree on shard count")
    shard_count = counts.pop()
    if not isinstance(shard_count, int) or shard_count <= 0:
        raise ValueError("mutation shard count is invalid")
    if len(fragments) != shard_count:
        raise ValueError(f"expected {shard_count} mutation shards, received {len(fragments)}")

    by_index: dict[int, dict] = {}
    results: dict[str, dict] = {}
    campaign_errors: list[str] = []
    elapsed = 0.0
    for fragment in fragments:
        if fragment.get("manifest_sha256") != expected_hash:
            raise ValueError("mutation shard manifest identity does not match this checkout")
        shard = fragment.get("shard", {})
        index = shard.get("index")
        if not isinstance(index, int) or index < 0 or index >= shard_count:
            raise ValueError(f"mutation shard index is invalid: {index!r}")
        if index in by_index:
            raise ValueError(f"duplicate mutation shard index: {index}")
        by_index[index] = fragment
        if shard.get("manifest_target_count") != len(targets):
            raise ValueError(
                f"mutation shard {index} reports the wrong manifest target count"
            )
        expected_paths = [item["path"] for item in gate.select_shard(targets, shard_count, index)]
        if shard.get("selected_paths") != expected_paths:
            raise ValueError(f"mutation shard {index} selected paths do not match the manifest")
        actual_paths = [item.get("path") for item in fragment.get("targets", [])]
        if actual_paths != expected_paths:
            raise ValueError(f"mutation shard {index} did not measure every selected target")
        if fragment.get("not_measured"):
            raise ValueError(f"mutation shard {index} reports unmeasured targets")
        for result in fragment["targets"]:
            results[result["path"]] = result
        campaign_errors.extend(fragment.get("campaign_errors", []))
        elapsed += float(fragment.get("elapsed_seconds", 0))

    ordered_paths = [target["path"] for target in targets]
    report = {
        "mode": "full",
        "manifest_sha256": expected_hash,
        "shards": shard_count,
        "targets": [results[path] for path in ordered_paths],
        "not_measured": [],
        "elapsed_seconds_sum": round(elapsed, 1),
        "campaign_errors": campaign_errors,
        "errors": campaign_errors,
    }
    report["overall"] = gate.aggregate(report["targets"])
    report["critical"] = gate.aggregate(report["targets"], critical_only=True)
    floors = manifest.get("floors", {})
    report["errors"] = gate.compliance_errors(
        report, float(floors.get("overall", 80)), float(floors.get("critical", 90))
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", nargs="+", type=Path)
    parser.add_argument("--manifest", type=Path, default=Path("mutation/targets.json"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/mutation"))
    parser.add_argument("--critical-only", action="store_true",
                        help="validate and combine only the manifest's critical targets")
    args = parser.parse_args(argv)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    try:
        manifest = gate.load_json(args.manifest)
        report = combine(
            manifest,
            [gate.load_json(path) for path in args.summary],
            critical_only=args.critical_only,
        )
    except ValueError as exc:
        report = {"mode": "full", "targets": [], "not_measured": [],
                  "overall": gate.aggregate([]), "critical": gate.aggregate([], True),
                  "errors": [str(exc)]}
    (args.artifacts / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    gate.write_junit(args.artifacts / "junit.xml", report)
    print(json.dumps({"overall": report["overall"], "critical": report["critical"],
                      "errors": report["errors"]}, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
