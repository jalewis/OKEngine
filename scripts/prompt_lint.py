#!/usr/bin/env python3
"""Semantic lint and size inventory for engine cron prompt sources."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def lint() -> tuple[list[str], dict]:
    jobs = json.loads((ROOT / "config/engine-crons.json").read_text(encoding="utf-8"))
    templates = json.loads((ROOT / "templates/pack/skeleton/crons/engine-template-prompts.json")
                           .read_text(encoding="utf-8"))
    errors, referenced, metrics = [], set(), {}

    def inspect(name: str, reference: str, contract: object) -> None:
        path = ROOT / reference
        referenced.add(path.resolve())
        try:
            canonical = path.read_text(encoding="utf-8").rstrip()
        except OSError:
            errors.append(f"{name}: prompt file is missing: {reference}")
            return
        lowered = canonical.lower()
        positive_raw = any(
            re.search(r"allowed:.*(?:file_write|patch)|use `?(?:file_write|patch)`?", line)
            and not re.search(r"\b(?:do not|don't|never|not)\b", line)
            for line in lowered.splitlines())
        governed = "mcp__okengine_write" in lowered or "governed mcp write" in lowered
        if positive_raw and governed:
            errors.append(f"{name}: contradictory raw-write and governed-write instructions")
        for line in canonical.splitlines():
            if "/opt/vault/wiki/raw" in line and "never" not in line.lower():
                errors.append(f"{name}: obsolete /opt/vault/wiki/raw path instruction")
        if "mcp discovery contract:" in lowered:
            errors.append(f"{name}: duplicated universal MCP guidance belongs to generation")
        completion = contract.get("completion") if isinstance(contract, dict) else None
        if completion == "per-selected-item" and "okengine-receipt" not in lowered:
            errors.append(f"{name}: per-item judgment prompt lacks receipt requirement")
        size = len(canonical.encode("utf-8"))
        metrics[name] = {"source": reference, "bytes": size,
                         "estimated_tokens": (size + 3) // 4}

    for job in jobs:
        if job.get("no_agent"):
            continue
        reference = job.get("prompt_file")
        if isinstance(reference, str):
            inspect(job["name"], reference, job.get("output_contract"))
        elif not job.get("no_agent") and job.get("name") not in templates:
            errors.append(f"{job['name']}: agent job has no Markdown prompt_file")
    for name, value in templates.items():
        reference = value.get("prompt_file") if isinstance(value, dict) else None
        if not isinstance(reference, str):
            errors.append(f"{name}: engine-template prompt lacks a Markdown reference")
            continue
        inspect(name, f"templates/pack/skeleton/{reference}",
                value.get("output_contract") if isinstance(value, dict) else None)

    roots = [ROOT / "prompts/cron", ROOT / "templates/pack/skeleton/prompts"]
    # glob-ok: both prompt registries are deliberately flat contract directories
    unreferenced = {path.resolve() for root in roots for path in root.glob("*.md")} - referenced
    errors.extend(f"unreferenced prompt: {path.relative_to(ROOT)}" for path in sorted(unreferenced))
    return errors, {"schema_version": 1, "lanes": metrics,
                    "total_bytes": sum(item["bytes"] for item in metrics.values()),
                    "total_estimated_tokens": sum(item["estimated_tokens"]
                                                  for item in metrics.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact")
    args = parser.parse_args(argv)
    errors, report = lint()
    if args.artifact:
        target = Path(args.artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    for error in errors:
        print(f"FAIL: {error}")
    print(f"prompt-lint: {len(report['lanes'])} lane(s), {report['total_bytes']} bytes, "
          f"{len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
