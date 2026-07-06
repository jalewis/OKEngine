"""Regression guard for okengine#139: build-engine-image.sh must clean its temp Hermes
clone via an EXIT trap (fires on any exit), not inline rm's that only ran on the
happy/error paths and leaked ~160M per failed/interrupted build."""
import re
from pathlib import Path

SH = Path(__file__).resolve().parent.parent / "scripts" / "build-engine-image.sh"


def test_temp_clone_cleaned_via_exit_trap():
    txt = SH.read_text(encoding="utf-8")
    # an EXIT trap performs the cleanup (rm of the mktemp parent), guarded by CLEAN_WORK
    assert re.search(r"trap '\[ \"\$\{CLEAN_WORK:-0\}\" = 1 \] && rm -rf \"\$\(dirname \"\$WORK\"\)\"' EXIT", txt), \
        "no EXIT trap that removes the temp clone"
    # the old inline-only cleanup (success/error path) is gone
    assert txt.count('rm -rf "$(dirname "$WORK")"') == 1, \
        "expected exactly one cleanup (the trap); inline rm's should be removed"
