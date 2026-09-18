"""Every `per-selected-item` engine lane must have a selector that writes its manifest (#478).

`completion: per-selected-item` makes the runner verify the model's receipt AGAINST the
lane's selection manifest. A selector that never writes one yields

    selection manifest unavailable: [Errno 2] No such file or directory: .../<lane>.json

on EVERY run, permanently — and the failure reads like a model fault, which is where most
of a day's debugging went. Measured on one live deployment: 171 failed receipts in this
class, still accruing.

`framework validate` gained the same check (okengine#478), but it only sees the PACK's
crons — these are ENGINE crons, which that path never inspects. Hence this test: it is the
only thing watching `config/engine-crons.json` against `scripts/cron/`.

WAIVED below is a RATCHET, not an exemption. It records lanes that are known-broken today
so a NEW one cannot be added silently. Fixing a lane means deleting its entry; the test then
holds the line. Do not add to it to make a red test green.
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CRONS = REPO / "config" / "engine-crons.json"
SDIR = REPO / "scripts" / "cron"

# Lanes declaring the contract whose selector writes no manifest, as of okengine#478.
# Each needs EITHER a manifest writer OR its `completion` dropped — tracked for remediation.
# EMPTY, and it must stay that way. The six original offenders (concept-backfill,
# orphans-drain, publisher-canonical-drain, source-staleness-refresh, trends-refresh,
# review-drain) were resolved in okengine#481 by DROPPING `completion` — they never
# implemented the per-item contract, so declaring it produced 171 guaranteed failures
# and no benefit. Adding real per-item receipts to any of them is a deliberate feature
# (selector writes the manifest, prompt emits matching keys), not a config toggle.
WAIVED: set[str] = set()

_WRITES_MANIFEST = ("write_selection_manifest", "OKENGINE_SELECTION_MANIFEST")

pytestmark = pytest.mark.skipif(not CRONS.is_file(), reason="engine-crons.json absent")


def _per_selected_item_lanes():
    data = json.loads(CRONS.read_text())
    jobs = data.get("jobs") if isinstance(data, dict) else data
    for job in jobs:
        if (job.get("output_contract") or {}).get("completion") == "per-selected-item":
            yield job


def _writes_manifest(job) -> bool | None:
    """True/False, or None when the selector cannot be located (undetectable, not a pass)."""
    script = job.get("script") or ""
    if not script:
        return False
    src = SDIR / Path(script).name
    if not src.is_file():
        return None
    text = src.read_text(encoding="utf-8", errors="replace")
    return any(token in text for token in _WRITES_MANIFEST)


def test_no_new_lane_promises_a_manifest_it_never_writes():
    offenders = {job.get("name") for job in _per_selected_item_lanes()
                 if _writes_manifest(job) is False}
    new = offenders - WAIVED
    assert not new, (
        "lane(s) declare completion=per-selected-item but their selector never writes a "
        "selection manifest — every run will fail receipt verification with "
        f"'selection manifest unavailable': {sorted(new)}")


def test_waivers_are_still_real_so_the_ratchet_tightens():
    """A waived lane that has since been FIXED must be removed from WAIVED.

    Without this the list rots into a permanent exemption and the detector silently stops
    covering lanes it is supposed to protect.
    """
    offenders = {job.get("name") for job in _per_selected_item_lanes()
                 if _writes_manifest(job) is False}
    stale = WAIVED - offenders
    assert not stale, (
        f"WAIVED lists lane(s) that now write their manifest — delete them from WAIVED "
        f"so the ratchet tightens: {sorted(stale)}")


def test_unlocatable_selectors_are_reported_not_silently_passed():
    """An absent selector is UNDETECTABLE. Fail loudly rather than assume it is fine."""
    unknown = {job.get("name") for job in _per_selected_item_lanes()
               if _writes_manifest(job) is None}
    assert not unknown, (
        "selector script not found for per-selected-item lane(s), so the manifest contract "
        f"cannot be checked — this is undetectable, not a pass: {sorted(unknown)}")


def test_every_per_item_job_declares_the_manifest_path_the_runner_reads():
    missing = {
        job.get("name") for job in _per_selected_item_lanes()
        if not isinstance(job.get("selection_manifest"), str)
        or not job["selection_manifest"]
    }
    assert not missing, (
        "per-selected-item lane(s) write a manifest but do not configure the path "
        f"the verifier reads: {sorted(missing)}"
    )


def test_every_per_item_job_has_bounded_iterations():
    unbounded = {
        job.get("name") for job in _per_selected_item_lanes()
        if not isinstance(job.get("max_iterations"), int)
        or job["max_iterations"] < 1
    }
    assert not unbounded, (
        "receipt-enforced model lane(s) lack an explicit iteration bound: "
        f"{sorted(unbounded)}"
    )


def test_the_contract_and_the_writer_token_have_not_drifted():
    """Pin the two strings this detector greps for — a rename would make it vacuous."""
    lib = SDIR / "selection_manifest.py"
    assert lib.is_file(), "scripts/cron/selection_manifest.py missing — detector is vacuous"
    assert re.search(r"^def write_selection_manifest\(", lib.read_text(), re.M), \
        "write_selection_manifest() renamed — update _WRITES_MANIFEST or this test lies"
