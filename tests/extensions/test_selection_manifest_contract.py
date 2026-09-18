"""A per-item receipt is legal only when its selector owns a selection manifest."""
from pathlib import Path

import yaml


REPO = Path(__file__).parents[2]


def test_extension_per_item_contracts_have_manifest_aware_selectors():
    offenders = []
    for manifest_path in sorted((REPO / "extensions").glob("*/extension.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text())
        extension_dir = manifest_path.parent
        for operation_name, operation in (manifest.get("operations") or {}).items():
            contract = operation.get("output_contract") or {}
            if contract.get("completion") != "per-selected-item":
                continue
            entrypoint = operation.get("entrypoint")
            selector = extension_dir / entrypoint if isinstance(entrypoint, str) else None
            source = selector.read_text() if selector and selector.is_file() else ""
            if "OKENGINE_SELECTION_MANIFEST" not in source:
                offenders.append(f"{manifest['id']}:{operation_name}")
    assert offenders == [], (
        "per-selected-item requires the selector to write the runner-owned selection "
        f"manifest; either implement it or remove the false contract: {offenders}"
    )
