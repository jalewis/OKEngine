#!/usr/bin/env python3
"""Bounded, resumable wake-gate for a model-write historical repair plan.

The selector never edits canonical pages. It verifies each plan precondition,
writes an exact selection manifest plus a detailed batch artifact, and lets the
lane-scoped MCP writer perform evidence-backed recompilation or review flags.
Completed dispositions are imported from an enforced receipt with --receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import yaml

from selection_manifest import write_selection_manifest

_FM = re.compile(r"\A---[ \t]*\n(.*?)\n---", re.S)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _metadata(path: Path) -> dict:
    match = _FM.match(path.read_text(errors="replace"))
    if not match:
        return {}
    try:
        doc = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _load(path: Path, default):
    try:
        value = json.loads(path.read_text())
        return value
    except (OSError, ValueError):
        return default


def record_receipt(state_path: Path, receipt_path: Path) -> int:
    document = _load(receipt_path, {})
    if isinstance(document, dict) and document.get("valid") is False:
        return 0
    receipt = document.get("receipt") if isinstance(document, dict) else None
    receipt = receipt if isinstance(receipt, dict) else document
    dispositions = receipt.get("items") or []
    if not isinstance(dispositions, list):
        raise ValueError("receipt dispositions must be a list")
    state = _load(state_path, {"api": 1, "completed": {}, "imported_receipts": []})
    completed = state.setdefault("completed", {})
    for item in dispositions:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        status = str(item.get("disposition") or "")
        if key and status in {"accepted", "duplicate", "skipped", "rejected", "deferred"}:
            completed[key] = {"status": status, "reason": item.get("reason")}
    imported = state.setdefault("imported_receipts", [])
    receipt_key = str(receipt_path.resolve())
    if receipt_key not in imported:
        imported.append(receipt_key)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n")
    temp.replace(state_path)
    return len(dispositions)


def import_receipts(state_path: Path) -> int:
    lane_id = os.environ.get("OKENGINE_LANE_ID", "")
    if not lane_id:
        return 0
    receipt_dir = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "cron-plus" / "receipts" / lane_id
    state = _load(state_path, {"imported_receipts": []})
    imported = set(state.get("imported_receipts") or [])
    count = 0
    # glob-ok: cron-plus stores canonical lane receipts directly in this flat runtime directory.
    for path in sorted(receipt_dir.glob("*.json")):
        if str(path.resolve()) not in imported:
            count += record_receipt(state_path, path)
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=Path(os.environ.get("WIKI_PATH", "/opt/vault")))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--batch-output", type=Path)
    parser.add_argument("--limit", type=int, default=int(os.environ.get("MODEL_WRITE_REPAIR_BATCH", "3")))
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    root = args.vault
    plan_path = args.plan or root / ".okengine" / "model-write-repair-plan.json"
    state_path = args.state or root / ".okengine" / "model-write-repair-state.json"
    batch_path = args.batch_output or root / ".okengine" / "model-write-repair-batch.json"
    if args.receipt:
        print(json.dumps({"recorded": record_receipt(state_path, args.receipt)}))
        return 0

    imported = import_receipts(state_path)
    if imported:
        print(f"imported {imported} receipt disposition(s) into repair checkpoint")

    plan = _load(plan_path, {})
    actions = plan.get("actions") or []
    state = _load(state_path, {"api": 1, "completed": {}})
    completed = state.get("completed") or {}
    batch, stale = [], []
    seen = set()
    selected_paths = set()
    for action in actions:
        if not isinstance(action, dict):
            continue
        path = str(action.get("path") or "")
        kind = str(action.get("action") or "")
        key = f"{path}|{kind}"
        if not path or key in seen or key in completed or path in selected_paths:
            continue
        seen.add(key)
        target = root / "wiki" / path
        if not target.is_file():
            stale.append({"item": key, "reason": "missing"})
            continue
        if action.get("expected_sha256") and _digest(target) != action["expected_sha256"]:
            stale.append({"item": key, "reason": "sha256-precondition"})
            continue
        metadata = _metadata(target)
        expected_version = action.get("expected_version")
        if expected_version is not None and str(metadata.get("version")) != str(expected_version):
            stale.append({"item": key, "reason": "version-precondition"})
            continue
        declared = metadata.get("raw")
        declared = declared if isinstance(declared, list) else ([declared] if isinstance(declared, str) else [])
        batch.append(dict(action, item=key,
                          canonical_path=f"wiki/{path}",
                          declared_raw=[str(value) for value in declared]))
        selected_paths.add(path)
        if len(batch) >= max(1, args.limit):
            break

    selected = [item["item"] for item in batch]
    if not selected:
        print(f"repair plan drained or stale; pending=0 stale={len(stale)}")
        print(json.dumps({"wakeAgent": False}))
        return 0
    manifest = write_selection_manifest(
        selected, root / ".okengine" / "model-write-repair-selection.json")
    receipt_items = []
    for item in batch:
        entry = {
            "key": item["item"],
            "disposition": "<accepted|duplicate|skipped|rejected|failed|deferred>",
            "writes": [],
            "reason": "<required for duplicate, skipped, rejected, failed, or deferred>",
        }
        if item.get("action") != "quarantine-for-review":
            entry["writes"] = [{
                "path": "wiki/<path>", "sha256": "sha256:<current-file-hash>",
            }]
        receipt_items.append(entry)
    receipt_template = {
        "api": 1,
        "lane_id": manifest["lane_id"],
        "contract_digest": manifest["contract_digest"],
        "input_digest": manifest["input_digest"],
        "items": receipt_items,
    }
    batch_doc = {"api": 1, "plan": str(plan_path), "actions": batch, "stale": stale,
                 "input_digest": manifest["input_digest"],
                 "receipt_template": receipt_template}
    batch_path.parent.mkdir(parents=True, exist_ok=True)
    temp = batch_path.with_suffix(".tmp")
    temp.write_text(json.dumps(batch_doc, indent=2) + "\n")
    temp.replace(batch_path)
    print("=== model-write historical repair batch ===")
    for item in batch:
        print(f"- {item['item']} expected={item.get('expected_sha256')}")
    print(f"batch artifact: {batch_path}")
    print(f"selection input_digest: {manifest['input_digest']}")
    print("FINAL RESPONSE CONTRACT (MANDATORY):")
    print("Return exactly one fenced okengine-receipt JSON object using the template below. ")
    print("Replace every angle-bracket placeholder. Do not return YAML, a table, ===== markers, ")
    print("a plain ``` fence, or prose outside the receipt fence.")
    print("```okengine-receipt")
    print(json.dumps(receipt_template, indent=2))
    print("```")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
