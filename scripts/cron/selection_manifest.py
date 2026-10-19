"""Shared, atomic selection-manifest writer for receipt-enforced cron lanes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def write_selection_manifest(selected: list[str], default_path: Path) -> dict:
    """Write the exact selected item keys and their contract identity atomically."""
    items = [str(item) for item in selected]
    manifest = {
        "api": 1,
        "selected": items,
        "input_digest": "sha256:" + hashlib.sha256(
            json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        "lane_id": os.environ.get("OKENGINE_LANE_ID", ""),
        "contract_digest": os.environ.get("OKENGINE_CONTRACT_DIGEST", ""),
    }
    path = Path(os.environ.get("OKENGINE_SELECTION_MANIFEST", str(default_path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, indent=2) + "\n")
    temp.replace(path)
    return manifest


# The canonical terminal vocabulary `run_receipts.validate()` accepts. Every non-write
# disposition must carry a reason; the write dispositions must list the pages they wrote.
RECEIPT_DISPOSITIONS = ("accepted", "updated", "merged", "deferred-for-review",
                        "insufficient-evidence", "rejected-out-of-scope", "duplicate", "failed")


def page_selection_key(path: Path, vault: Path) -> str:
    """Bind an update-in-place selection to its exact pre-write content.

    The key is the vault-relative page path plus the page's sha256 at selection time, so the
    runner can tell a page this run changed from one it left alone even without write telemetry.
    """
    rel = path.relative_to(vault / "wiki").as_posix()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"wiki/{rel}|sha256:{digest}"


def receipt_template(manifest: dict, writes: dict[str, str]) -> dict:
    """The fenced receipt the model must return: one item per selected key, in order.

    `writes` maps each selected key to the page path an accepted disposition would write
    (a placeholder when the lane creates a page whose slug the model chooses).
    """
    return {
        "api": 1,
        "lane_id": manifest["lane_id"],
        "contract_digest": manifest["contract_digest"],
        "input_digest": manifest["input_digest"],
        "items": [{
            "key": key,
            "disposition": "<" + "|".join(RECEIPT_DISPOSITIONS) + ">",
            "writes": [{"path": writes.get(key) or key.partition("|")[0], "sha256": None}],
            "reason": "<required unless accepted|updated|merged>",
        } for key in manifest["selected"]],
    }


def print_receipt_template(manifest: dict, writes: dict[str, str] | None = None) -> None:
    """Print the mandatory final-response receipt for a non-empty selection (no-op when empty)."""
    if not manifest["selected"]:
        return
    print("FINAL RESPONSE CONTRACT (MANDATORY): return ONLY this fenced receipt, one item per")
    print("selected key. Use accepted/updated only for items you actually wrote; every other")
    print("disposition needs a concrete reason and no writes (remove its placeholder writes).")
    print("The runner verifies writes against the vault and supplies the hashes.")
    print("```okengine-receipt")
    print(json.dumps(receipt_template(manifest, writes or {}), indent=2))
    print("```")
