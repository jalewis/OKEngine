#!/usr/bin/env python3
"""Semantic lint and size inventory for engine cron prompt sources.

Receipt rule (okengine#916, the engine half of okpacks-library!225): a lane owes the fenced
``okengine-receipt`` instruction when its EFFECTIVE output contract says
``completion: per-selected-item`` (the runner verifies a receipt) AND its selector prints the
receipt the model must copy (``okengine-receipt`` / ``print_receipt_template(``). Neither fact
lives in the prompt, and the skeleton ``engine-template-prompts.json`` carries only
``prompt_file`` -- reading the contract from there silently passed every engine-template lane.
So the requirement is derived from the lane's real contract (``config/engine-crons.json``, composed
with any pack policy, or an extension operation) and from its selector source:

- per-item lane, selector prints a receipt, prompt never mentions it  -> FAIL;
- per-item lane whose selector prints none (okengine#799)             -> REPORTED, not failed;
- per-item lane whose selector is unreadable                          -> FAIL (UNDETECTABLE);
- a template prompt bound to no engine model lane                     -> REPORTED (UNDETECTABLE).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_HERE = Path(__file__).resolve().parent
RECEIPT_FENCE = "okengine-receipt"
# How a selector prints the receipt: the literal fence, or the shared helper that emits it.
SELECTOR_RECEIPT_MARKS = (RECEIPT_FENCE, "print_receipt_template(")


def _sibling(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selector_prints_receipt(path: Path | None) -> bool | None:
    """True/False from the selector source; None when there is no readable source."""
    if path is None:
        return None
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return any(mark in source for mark in SELECTOR_RECEIPT_MARKS)


def receipt_verdict(name: str, contract: object, selector: Path | None,
                    text: str) -> tuple[list[str], list[str]]:
    """(errors, reported) for one lane's prompt against its effective contract."""
    if not isinstance(contract, dict) or contract.get("completion") != "per-selected-item":
        return [], []                    # the runner verifies no receipt for this lane
    label = selector.name if selector is not None else "(none declared)"
    prints = selector_prints_receipt(selector)
    if prints is None:
        return [f"{name}: per-item lane but its selector {label} is unreadable — receipt "
                "requirement UNDETECTABLE, not a pass"], []
    if not prints:
        return [], [f"{name}: runner verifies a receipt its selector {label} never prints "
                    "(engine-side, okengine#799)"]
    if RECEIPT_FENCE not in text.lower():
        return [f"{name}: per-item judgment prompt lacks receipt requirement — selector "
                f"{label} prints an {RECEIPT_FENCE} the runner verifies, but the prompt never "
                "tells the model to return it"], []
    return [], []


def engine_selector(job: dict) -> Path | None:
    script = job.get("script")
    if not isinstance(script, str) or not script:
        return None
    return ROOT / "scripts/cron" / Path(script).name


def extension_lanes() -> tuple[dict[str, dict], list[str]]:
    """Every first-party extension operation -> {contract, selector, prompt, source}."""
    discovery = _sibling("extension_discovery", "extension_discovery.py")
    compose = _sibling("extension_compose", "extension_compose.py")
    records, errors = discovery.discover(None, engine_root=ROOT)
    lanes: dict[str, dict] = {}
    for record in records:
        ext_dir = Path(record["dir"])
        ops, error = compose._collect_ops(record["manifest"], ext_dir)
        if error:
            errors.append(f"{record['id']}: {error}")
        for op_name, op in ops:
            reference = op.get("prompt_file")
            entry = op.get("entrypoint")
            entry = entry.get("script") if isinstance(entry, dict) else entry
            lanes[f"{record['id']}:{op_name}" if op_name else record["id"]] = {
                "contract": op.get("output_contract"),
                "selector": ext_dir / entry if isinstance(entry, str) and entry else None,
                "prompt": op.get("prompt"),
                "source": ext_dir / reference if isinstance(reference, str) else None,
            }
    return lanes, errors


def lint_extensions() -> tuple[list[str], list[str]]:
    lanes, errors = extension_lanes()
    reported: list[str] = []
    for name, lane in sorted(lanes.items()):
        text = lane["prompt"] if isinstance(lane["prompt"], str) else ""
        if not text and lane["source"] is not None:
            try:
                text = lane["source"].read_text(encoding="utf-8")
            except OSError:
                errors.append(f"{name}: prompt file is missing: {lane['source']}")
                continue
        if not text:
            continue                     # a no_agent operation has no prompt to lint
        found, noted = receipt_verdict(name, lane["contract"], lane["selector"], text)
        errors.extend(found)
        reported.extend(noted)
    return errors, reported


def lint() -> tuple[list[str], dict]:
    jobs = json.loads((ROOT / "config/engine-crons.json").read_text(encoding="utf-8"))
    templates = json.loads((ROOT / "templates/pack/skeleton/crons/engine-template-prompts.json")
                           .read_text(encoding="utf-8"))
    contracts = _sibling("output_contract", "cron/output_contract.py")
    by_name = {job.get("name"): job for job in jobs if isinstance(job, dict)}
    errors, reported, referenced, metrics = [], [], set(), {}

    def inspect(name: str, reference: str, contract: object, selector: Path | None) -> None:
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
        found, noted = receipt_verdict(name, contract, selector, canonical)
        errors.extend(found)
        reported.extend(noted)
        size = len(canonical.encode("utf-8"))
        metrics[name] = {"source": reference, "bytes": size,
                         "estimated_tokens": (size + 3) // 4}

    for job in jobs:
        if job.get("no_agent"):
            continue
        reference = job.get("prompt_file")
        if isinstance(reference, str):
            inspect(job["name"], reference, job.get("output_contract"), engine_selector(job))
        elif job.get("name") not in templates:
            errors.append(f"{job['name']}: agent job has no Markdown prompt_file")
    for name, value in templates.items():
        reference = value.get("prompt_file") if isinstance(value, dict) else None
        if not isinstance(reference, str):
            errors.append(f"{name}: engine-template prompt lacks a Markdown reference")
            continue
        policy = value.get("output_contract")
        lane = by_name.get(name)
        if lane is None or lane.get("no_agent"):
            reported.append(f"{name}: template prompt binds no engine model lane — its receipt "
                            "requirement is UNDETECTABLE")
            contract, selector = policy, None
        else:
            selector = engine_selector(lane)
            try:
                contract = contracts.compose(lane.get("output_contract"),
                                             policy if isinstance(policy, dict) else None, name)
            except ValueError as exc:
                errors.append(f"{name}: template output_contract does not compose: {exc}")
                contract = None
        inspect(name, f"templates/pack/skeleton/{reference}", contract, selector)

    extension_errors, extension_reported = lint_extensions()
    errors.extend(extension_errors)
    reported.extend(extension_reported)
    roots = [ROOT / "prompts/cron", ROOT / "templates/pack/skeleton/prompts"]
    # glob-ok: both prompt registries are deliberately flat contract directories
    unreferenced = {path.resolve() for root in roots for path in root.glob("*.md")} - referenced
    errors.extend(f"unreferenced prompt: {path.relative_to(ROOT)}" for path in sorted(unreferenced))
    return errors, {"schema_version": 1, "lanes": metrics, "reported": reported,
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
    for note in report.get("reported", []):
        print(f"KNOWN: {note}")
    for error in errors:
        print(f"FAIL: {error}")
    print(f"prompt-lint: {len(report['lanes'])} lane(s), {report['total_bytes']} bytes, "
          f"{len(report.get('reported', []))} reported, {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
