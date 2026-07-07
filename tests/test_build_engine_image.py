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


import os
import shutil
import subprocess


def test_missing_pin_fails_loud_not_stale_default(tmp_path):
    """okengine#193 shift-left (no-silent-omission): a manifest that can't yield pinned_tag must FAIL
    the build, not silently fall back to a stale hardcoded pin. The old `${PIN:-v2026.6.19}` would
    clone a Hermes two versions behind current and ship a mismatched base."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SH, tmp_path / "scripts" / "build-engine-image.sh")
    # a manifest WITHOUT pinned_tag (engine_release present so the failure is unambiguously the pin)
    (tmp_path / "engine-manifest.yaml").write_text(
        "engine_release: v9.9.9\nruntime:\n  # pinned_tag intentionally absent\n", encoding="utf-8")
    r = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "build-engine-image.sh")],
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""), "SKIP_BUILD": "1"})
    assert r.returncode != 0, f"expected failure, got 0:\n{r.stdout}\n{r.stderr}"
    assert "pinned_tag" in r.stderr, r.stderr


def test_no_stale_hardcoded_pin_fallback():
    """The stale-literal fallbacks (`${PIN:-v2026.6.19}`, `${RELEASE:-unknown}`) are gone — a bad
    manifest must fail, not guess. PIN/RELEASE stay env-overridable."""
    txt = SH.read_text(encoding="utf-8")
    assert "v2026.6.19" not in txt, "stale hardcoded pin fallback must be removed"
    assert "RELEASE:-unknown" not in txt, "'unknown' version fallback must be removed"
    assert '[ -n "$PIN" ]' in txt and '[ -n "$RELEASE" ]' in txt, "must fail loud on an empty PIN/RELEASE"
