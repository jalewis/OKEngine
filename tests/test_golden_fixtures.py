"""Golden conformance fixtures (okengine#75).

A durable, checked-in reference vault (tests/fixtures/golden/) that pins the conformance
validator's behavior: every `valid/` page must pass, and every `invalid/` page must be
rejected for its DECLARED reason (tests/fixtures/golden/invalid/EXPECTED.yaml). Unlike the
ad-hoc inline schemas scattered across the validator tests, this is one stable golden vault —
a regression here is a deliberate conformance-contract change, and the diff shows exactly what
moved.

Adding a case:
  - a new valid page → drop it in valid/ (must return None)
  - a new invalid page → drop it in invalid/ AND add its expected-reason substring to
    EXPECTED.yaml (that edit is the acknowledgement of the new rejection contract)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"


def _validator():
    """Load tools.schema_validator by path (no package install needed), pointed at the
    golden schema so schema_reject_reason resolves it as the governing schema."""
    spec = importlib.util.spec_from_file_location(
        "schema_validator_golden", REPO / "tools" / "schema_validator.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _place(tmp_path: Path, page: Path) -> Path:
    """Copy the golden schema + one fixture page into a temp vault so the validator's
    walk-up schema resolution finds the golden schema.yaml above wiki/."""
    (tmp_path / "wiki").mkdir(exist_ok=True)
    (tmp_path / "schema.yaml").write_text(GOLDEN.joinpath("schema.yaml").read_text(encoding="utf-8"),
                                          encoding="utf-8")
    dest = tmp_path / "wiki" / "entities" / page.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _valid_pages():
    return sorted((GOLDEN / "valid").glob("*.md"))


def _invalid_cases():
    expected = yaml.safe_load((GOLDEN / "invalid" / "EXPECTED.yaml").read_text(encoding="utf-8"))
    return [(GOLDEN / "invalid" / name, substr) for name, substr in expected.items()]


@pytest.mark.parametrize("page", _valid_pages(), ids=lambda p: p.name)
def test_golden_valid_pages_pass(tmp_path, page):
    m = _validator()
    dest = _place(tmp_path, page)
    reason = m.schema_reject_reason(str(dest.resolve()), dest.read_text(encoding="utf-8"))
    assert reason is None, f"golden VALID page {page.name} was rejected: {reason}"


@pytest.mark.parametrize("page,substr", _invalid_cases(), ids=lambda v: getattr(v, "name", v))
def test_golden_invalid_pages_rejected_for_declared_reason(tmp_path, page, substr):
    m = _validator()
    dest = _place(tmp_path, page)
    reason = m.schema_reject_reason(str(dest.resolve()), dest.read_text(encoding="utf-8"))
    assert reason is not None, f"golden INVALID page {page.name} was NOT rejected (expected {substr!r})"
    assert substr.lower() in reason.lower(), \
        f"{page.name} rejected for {reason!r}, expected to contain {substr!r}"


def test_every_invalid_fixture_has_an_expected_reason():
    """No orphan invalid fixtures: every invalid/*.md (except the manifest) must be declared in
    EXPECTED.yaml, so a new invalid case can't be added without pinning its rejection reason."""
    declared = set(yaml.safe_load((GOLDEN / "invalid" / "EXPECTED.yaml").read_text(encoding="utf-8")))
    on_disk = {p.name for p in (GOLDEN / "invalid").glob("*.md")}
    assert on_disk == declared, (
        f"invalid fixtures out of sync with EXPECTED.yaml — on-disk-not-declared: "
        f"{on_disk - declared}; declared-not-on-disk: {declared - on_disk}")
