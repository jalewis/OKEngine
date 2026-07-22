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
