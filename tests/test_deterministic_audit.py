"""Smoke test for scripts/audit/deterministic_audit.py (okengine#334).

scripts/audit/ is EXCLUDED from the public snapshot, so this test SKIPS (not errors) when the script
is absent — the publish-tree-divergence convention the script itself detects. Runs the non-network
checks directly (no git ls-remote) and asserts the finding contract.
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "audit" / "deterministic_audit.py"
pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(), reason="scripts/audit excluded from the public snapshot — runs in the source repo")


def _load():
    spec = importlib.util.spec_from_file_location("deterministic_audit", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m._findings.clear()
    return m


def test_findings_have_the_contract_shape():
    m = _load()
    # non-network checks against the real repo — must run without raising and emit well-formed findings
    m.check_scrub_parity()
    m.check_publish_tree_divergence()
    m.check_test_skip_blindspots()
    m.check_constant_drift()
    assert isinstance(m._findings, list)
    for f in m._findings:
        assert set(f) == {"severity", "dimension", "file", "detail"}, f
        assert f["severity"] in ("critical", "high", "medium", "low")
        assert f["detail"]


def test_publish_tree_divergence_is_clean_on_source():
    """The known publish-EXCLUDED-file reads are all guarded (test_issue204 skipif, etc.), so this
    dimension should report nothing on the current tree — a regression here means a new unguarded
    read of an excluded path (green locally, red in public CI)."""
    m = _load()
    m.check_publish_tree_divergence()
    hits = [f for f in m._findings if f["dimension"] == "publish-tree-divergence"]
    assert not hits, f"unguarded reads of publish-EXCLUDED paths: {[h['file'] for h in hits]}"


def test_constant_drift_default_token_agrees():
    """The built-in default MCP token must agree across server.py / hardening_lib / write_server /
    compose (the fail-closed guards compare against it)."""
    m = _load()
    m.check_constant_drift()
    drift = [f for f in m._findings if f["dimension"] == "constant-drift" and "TOKEN" in f["detail"]]
    assert not drift, drift
