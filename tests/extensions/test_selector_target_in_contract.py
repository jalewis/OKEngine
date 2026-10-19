"""A selector must only direct its lane to a namespace that lane may write.

forecast-review's selector told the model to write `briefings/forecast-review-<date>` while the
lane's output_contract (and the extension's write capability) allowed only predictions/ and
dashboards/. Nothing flagged the disagreement: the model improvised a different seat each week,
and on one live vault the reviews scattered across three namespaces, so every surface that read
one of them went stale. This pins the three surfaces together for every extension operation.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

EXTENSIONS = Path(__file__).resolve().parents[2] / "extensions"
# A selector names its lane's output seat as `out_path = f"<namespace>/..."`.
_TARGET = re.compile(r"""out_path\s*=\s*f?["']([a-z][a-z0-9_-]*)/""")


def _declared_targets() -> list[tuple[str, str, str, list[str], list[str]]]:
    rows = []
    for manifest in sorted(EXTENSIONS.glob("*/extension.yaml")):
        spec = yaml.safe_load(manifest.read_text()) or {}
        write = [str(g).split("/", 1)[0] for g in (spec.get("capabilities") or {}).get("write") or []]
        ops = spec.get("operations") or {}
        for name, op in (ops.items() if isinstance(ops, dict) else []):
            entry = (op or {}).get("entrypoint")
            contract = (op or {}).get("output_contract") or {}
            source = manifest.parent / str(entry)
            if not entry or not source.is_file():
                continue
            for namespace in sorted(set(_TARGET.findall(source.read_text(errors="ignore")))):
                rows.append((f"{manifest.parent.name}:{name}", entry, namespace,
                             list(contract.get("allowed_namespaces") or []), write))
    return rows


def test_the_scan_finds_the_selectors_that_name_a_target():
    """NEGATIVE-SPACE guard: an empty scan would pass vacuously. The known targets must be found."""
    found = {(lane, ns) for lane, _e, ns, _c, _w in _declared_targets()}
    assert ("okengine.predictions:forecast-review", "briefings") in found
    assert ("okengine.messaging-synthesis:messaging-synthesis", "briefings") in found


@pytest.mark.parametrize("lane, entry, namespace, contract, write",
                         _declared_targets(), ids=lambda v: v if isinstance(v, str) else "")
def test_a_selector_target_is_writable_by_its_own_lane(lane, entry, namespace, contract, write):
    assert contract, f"{lane}: {entry} names a target but the op declares no output_contract"
    assert namespace in contract or "*" in contract, (
        f"{lane}: {entry} sends the model to {namespace}/ but its output_contract allows only "
        f"{contract} — the model will improvise a seat instead")
    assert namespace in write or "*" in write or "**" in write, (
        f"{lane}: {entry} targets {namespace}/ outside the extension's write capability {write}")
