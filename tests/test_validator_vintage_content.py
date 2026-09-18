"""`check_validator_vintage` compares CONTENT, not just the stamp (okengine#606).

The stamp was the whole detector, and it failed in the way hand-maintained stamps do: FOUR
distinct `validate.py` contents shipped under `VALIDATE_VERSION = "2026.07.3"` across the engine
skeleton and the pack repos, each carrying a fix the others lacked. Six copies had the
okengine#178 `@jitter`-base check and four did not; the pack copies enforced https-only feed
probing and the skeleton did not; only the skeleton handled a string-form `schedule`. Every one
of them reported the same vintage, so the detector said they agreed.

The rule now: a stamp that is merely BEHIND still warns (honest staleness — refreshing the whole
fleet is not a precondition for this landing), but a stamp that CLAIMS the skeleton's vintage
while the content differs is a FAIL. That second case is the defect class itself.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VAL = REPO / "scripts" / "framework_validate.py"
SKELETON = REPO / "templates" / "pack" / "skeleton" / "validate.py"

pytestmark = pytest.mark.skipif(not VAL.is_file(), reason="framework_validate absent")


def _load():
    spec = importlib.util.spec_from_file_location("framework_validate_vintage", VAL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_validate_vintage"] = m
    spec.loader.exec_module(m)
    return m


def _messages(report) -> str:
    return " | ".join(f"{sev}:{check}:{detail}" for sev, check, detail in report.rows)


def _skeleton_text() -> str:
    return SKELETON.read_text(encoding="utf-8")


def _skeleton_stamp() -> str:
    m = re.search(r'VALIDATE_VERSION\s*=\s*"([^"]+)"', _skeleton_text())
    assert m, "the skeleton lost its VALIDATE_VERSION stamp"
    return m.group(1)


def _run(pack: Path):
    v = _load()
    r = v.Report()
    v.check_validator_vintage(pack, r)
    return r


def test_a_verbatim_copy_of_the_skeleton_reports_nothing(tmp_path):
    """The pass case. Without it the failures below would prove only that the check is noisy."""
    (tmp_path / "validate.py").write_text(_skeleton_text(), encoding="utf-8")
    assert _run(tmp_path).rows == []


def test_the_same_stamp_with_different_content_fails(tmp_path):
    """okengine#606 exactly: a copy asserting the skeleton's vintage while being a different
    program. A stamp comparison passes this — which is how four vintages coexisted."""
    text = _skeleton_text().replace(
        'if base not in {"hourly", "2h", "4h", "6h", "12h", "daily", "weekly"}:',
        'if False:')
    assert text != _skeleton_text(), "the edit did not take — the skeleton's shape moved"
    (tmp_path / "validate.py").write_text(text, encoding="utf-8")
    report = _run(tmp_path)
    assert report.n_fail == 1
    assert _skeleton_stamp() in _messages(report)
    assert "content differs" in _messages(report)


def test_an_older_stamp_still_only_warns(tmp_path):
    """Honest staleness. Every deployed pack tree is behind the skeleton the moment it is
    bumped; if that were a FAIL, this check could not land without refreshing the fleet first."""
    (tmp_path / "validate.py").write_text(
        _skeleton_text().replace(f'VALIDATE_VERSION = "{_skeleton_stamp()}"',
                                 'VALIDATE_VERSION = "2026.07.3"'), encoding="utf-8")
    report = _run(tmp_path)
    assert report.n_fail == 0 and report.n_warn == 1
    assert "refresh" in _messages(report)


def test_an_unstamped_validator_still_only_warns(tmp_path):
    """Pre-consolidation vintage — no claim was made, so nothing was falsified."""
    (tmp_path / "validate.py").write_text("# ancient\n", encoding="utf-8")
    report = _run(tmp_path)
    assert report.n_fail == 0 and "no VALIDATE_VERSION" in _messages(report)


def test_a_bundle_is_exempt_even_though_its_validator_differs(tmp_path):
    """A bundle owns no types and ships no schema.yaml, so it carries a recipe validator that is
    legitimately a different file (okengine#181). Without this it would fail for being correct."""
    (tmp_path / "pack.yaml").write_text("name: okpack-demo\nkind: bundle\n", encoding="utf-8")
    (tmp_path / "validate.py").write_text(
        f'VALIDATE_VERSION = "{_skeleton_stamp()}"\n# a recipe validator\n', encoding="utf-8")
    assert _run(tmp_path).rows == []


def test_a_quoted_bundle_kind_is_recognised(tmp_path):
    """`kind: "bundle"` is the same declaration. Matching only the bare form would fail a pack
    for its YAML quoting style."""
    (tmp_path / "pack.yaml").write_text('name: okpack-demo\nkind: "bundle"\n', encoding="utf-8")
    (tmp_path / "validate.py").write_text(
        f'VALIDATE_VERSION = "{_skeleton_stamp()}"\n# a recipe validator\n', encoding="utf-8")
    assert _run(tmp_path).rows == []


def test_a_non_bundle_pack_yaml_does_not_exempt(tmp_path):
    """The exemption is for bundles, not for having a pack.yaml."""
    (tmp_path / "pack.yaml").write_text("name: okpack-demo\nkind: domain\n", encoding="utf-8")
    (tmp_path / "validate.py").write_text(
        f'VALIDATE_VERSION = "{_skeleton_stamp()}"\n# not the skeleton\n', encoding="utf-8")
    assert _run(tmp_path).n_fail == 1


def test_no_validator_is_not_a_finding(tmp_path):
    """A pack may ship none; that is a different question from shipping a drifted one."""
    assert _run(tmp_path).rows == []


# --- the property that makes byte-comparison possible at all -------------------------------

def test_the_skeleton_validator_carries_no_template_placeholder():
    """The two `{{PACK}}` literals (docstring, User-Agent) were the only per-pack difference,
    and they made every copy legitimately different — so nothing could be compared byte-for-byte
    and the stamp was the only available signal. The name is derived at runtime now; a
    placeholder coming back would silently disable the content check for every pack."""
    text = _skeleton_text()
    # Against the DECLARED placeholder set, not a bare "{{" — the validator legitimately contains
    # `{{create:false, update:false}}`, an escaped brace inside an f-string.
    declared = set(re.findall(r"\{\{[A-Z_]+\}\}",
                              (REPO / "templates" / "pack" / "PLACEHOLDERS.md")
                              .read_text(encoding="utf-8")))
    assert declared, "PLACEHOLDERS.md declared none — this assertion would pass vacuously"
    present = sorted(ph for ph in declared if ph in text)
    assert not present, f"template placeholder(s) returned to the skeleton validator: {present}"
    assert "PACK_NAME = ROOT.name" in text, "the runtime name derivation is gone"


def test_the_skeleton_validator_keeps_the_fixes_that_were_split_across_vintages():
    """Each of these lived in some copies and not others while all four claimed one vintage.
    Reconciling them is what made a single file possible; losing one silently re-forks it."""
    text = _skeleton_text()
    assert '"hourly", "2h", "4h", "6h", "12h", "daily", "weekly"' in text, "okengine#178 base check"
    assert "non-https URL skipped" in text, "https-only feed probing"
    assert "isinstance(schedule, str)" in text, "string-form schedule handling"
    assert "nosec B314" in text, "ET.parse bandit annotation"
