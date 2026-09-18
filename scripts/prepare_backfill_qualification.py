#!/usr/bin/env python3
"""Create a unique, deterministic qualification corpus across the requested packs."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path

import yaml

_ENGINE = Path(__file__).resolve().parent.parent
_REVIEW_SPEC = importlib.util.spec_from_file_location(
    "okengine_prepare_review_context", _ENGINE / "scripts/review_context.py")
review_context = importlib.util.module_from_spec(_REVIEW_SPEC)
assert _REVIEW_SPEC.loader
_REVIEW_SPEC.loader.exec_module(review_context)


# The pack set is OPERATOR INPUT, not an engine literal (okengine#510).
#
# This carried a map of five deployment names — one operator's fleet, including a real
# company's vault — inside a domain-agnostic engine. On any other fleet nothing matched and
# qualification reported a clean pass over ZERO packs: the pilot cohort had become the tool's
# operating boundary.
#
# Auto-discovery is NOT the fix, and this was measured rather than assumed: treating "has
# pack.yaml" as the marker finds 13 directories on this host, including archived checkouts
# and pack SOURCE repos, so preparing fixtures would have written synthetic records into 8
# trees that are not deployments — and a pack plus its `-test` twin
# collapsed to the same slug, colliding fixture ids. Nothing on disk distinguishes "a pack
# checkout" from "a deployment this matrix targets": that pairing (pack ↔ gateway container)
# is fleet composition. So it is supplied, and absence is a loud failure.


def pack_slug(name: str) -> str:
    """An id-safe token for a pack, derived from its directory name.

    The FULL name is used, so two checkouts of the same pack (for example `<pack>` and
    `<pack>-test`) cannot collide into one fixture id. Only ever used to build
    synthetic ids/URLs within a generation, so uniqueness matters and prettiness does not.
    """
    return "".join(c if (c.isalnum() or c == "-") else "-" for c in name.strip().lower())


def pack_label(name: str) -> str:
    """A human label for fixture prose, derived from the same name."""
    return " ".join(part.capitalize() for part in pack_slug(name).split("-") if part) or name


def requested_packs(cli_packs: list[str] | None) -> list[str]:
    """The packs to qualify: repeated --pack, else OKENGINE_QUALIFICATION_PACKS (comma-separated)."""
    if cli_packs:
        return list(dict.fromkeys(p.strip() for p in cli_packs if p.strip()))
    env = os.environ.get("OKENGINE_QUALIFICATION_PACKS", "")
    return list(dict.fromkeys(p.strip() for p in env.split(",") if p.strip()))


def predictions_enabled(pack_dir: Path) -> bool:
    """Whether this pack enables okengine.predictions.

    Replaces a hardcoded subset of pack names. Enablement is vault-level state in
    `.okengine/extensions.yaml` (written by `framework extensions enable`), so the
    capability is asked about rather than inferred from a deployment's name. Verified to
    reproduce the previous subset exactly on this fleet.
    """
    path = pack_dir / ".okengine" / "extensions.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    enabled = data.get("enabled") if isinstance(data, dict) else None
    return isinstance(enabled, dict) and "okengine.predictions" in enabled


def write_new(path: Path, content: str) -> None:
    if path.exists():
        raise SystemExit(f"qualification fixture already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("generation", help="unique lowercase token, e.g. g20")
    # No deployment default: the engine ships no operator filesystem layout.
    parser.add_argument("--pack-root", default=os.environ.get("OKENGINE_PACK_ROOT", ""))
    parser.add_argument("--pack", action="append",
                        help="pack directory name to qualify; repeatable. Defaults to "
                             "OKENGINE_QUALIFICATION_PACKS (comma-separated). No built-in "
                             "list — the engine ships no fleet composition.")
    parser.add_argument("--engine-dir", default=str(_ENGINE))
    parser.add_argument("--allow-unattributable", action="store_true")
    args = parser.parse_args()
    generation = args.generation.strip().lower()
    if not generation or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in generation):
        raise SystemExit("generation must contain only lowercase letters, digits, and hyphens")
    if not args.pack_root.strip():
        raise SystemExit(
            "no pack root: pass --pack-root or set OKENGINE_PACK_ROOT to the "
            "directory containing the pack checkouts")
    root = Path(args.pack_root)
    packs = requested_packs(args.pack)
    if not packs:
        raise SystemExit(
            "no packs requested: pass --pack (repeatable) or set "
            "OKENGINE_QUALIFICATION_PACKS to a comma-separated list of pack directory "
            "names under the pack root. There is deliberately no built-in list — "
            "qualifying zero packs must never look like a clean pass (okengine#510)")
    missing = [p for p in packs if not (root / p / "pack.yaml").is_file()]
    if missing:
        raise SystemExit(
            f"not a pack checkout (no pack.yaml) under {root}: {', '.join(missing)}")
    source_context = review_context.collect(Path(args.engine_dir))
    context_errors = review_context.problems(source_context)
    if context_errors and not args.allow_unattributable:
        raise SystemExit(review_context.render(source_context, context_errors))
    print(review_context.render(source_context, context_errors,
                                overridden=args.allow_unattributable))
    concept = f"qwen-final-local-backfill-control-{generation}"

    for pack in packs:
        label, slug = pack_label(pack), pack_slug(pack)
        base = root / pack
        source = base / "wiki/sources/2026/07/27" / f"qwen-final-control-{generation}.md"
        write_new(source, f"""---
id: sources:qwen-final-control-{slug}-{generation}
type: source
qualification_fixture: true
title: {label} {generation.upper()} Verification Cooperative qualification record
publisher: {label} {generation.upper()} Verification Cooperative
published: 2026-07-27
url: https://qualification.invalid/final/{slug}/{generation}
---
{label} {generation.upper()} Verification Cooperative is a durable testing
organization responsible for [[concepts/q/w/{concept}]]. Its full name is unique and
it has no aliases. This controlled record requires local Qwen Coder,
schema-valid writes, verified receipts, and no cloud fallback.
""")
        page_quality_evidence = (
            base / "wiki/sources/2026/07/27"
            / f"qwen-page-quality-evidence-{generation}.md"
        )
        write_new(page_quality_evidence, f"""---
id: sources:qwen-page-quality-evidence-{slug}-{generation}
type: source
title: {label} {generation.upper()} page-quality evidence
publisher: {label} {generation.upper()} Verification Cooperative
published: 2026-07-27
url: https://qualification.invalid/page-quality/{slug}/{generation}
---
The controlled [[sources/2026/07/27/qwen-final-control-{generation}]] record
demonstrates that local Qwen Coder can perform governed enrichment without a
cloud fallback. Its verification requires an accepted write, a schema-valid
readback hash, and an exact per-selected-item completion receipt.

This qualification evidence also establishes the operational value of bounded
context, deterministic selection, and explicit terminal dispositions. Together
those controls prevent silent work loss and make each agent action observable.
""")
        page_quality_queue = (
            base / "wiki/operational"
            / f"qwen-page-quality-qualification-{generation}.json"
        )
        write_new(page_quality_queue, json.dumps([{
            "page": f"sources/2026/07/27/qwen-final-control-{generation}",
            "tier": "thin",
            "words": 48,
            "sections": 0,
            "sources": 0,
            "inbound": 1,
        }], indent=2) + "\n")
        raw = base / "raw/qualification" / f"qwen-final-control-{generation}.md"
        write_new(raw, f"""# {label} Qwen qualification {generation.upper()}

Publisher: {label} {generation.upper()} Verification Cooperative
URL: https://qualification.invalid/raw/{slug}/{generation}
Published: 2026-07-27

The laboratory validates local Qwen Coder backfills with verified receipts and no cloud fallback.
""")
        if predictions_enabled(base):
            prediction = base / "wiki/predictions" / f"qwen-qualification-{generation}.md"
            write_new(prediction, f"""---
id: predictions:qwen-qualification-{slug}-{generation}
type: prediction
qualification_fixture: true
status: open
confidence: 0.5
subject: Qwen qualification {generation.upper()}
made_on: 2026-07-27
resolves_by: 2026-07-28
horizon: short
measurement_method: Review the ephemeral {generation.upper()} qualification report.
sources:
- sources/2026/07/27/qwen-final-control-{generation}
---
# Qwen qualification {generation.upper()}

All controlled {label} backfill lanes will complete through local Qwen Coder.
""")
    print(f"prepared qualification generation {generation} across {len(packs)} packs: "
          f"{', '.join(packs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
