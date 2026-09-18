"""Smoke test for scripts/audit/deterministic_audit.py (okengine#334).

scripts/audit/ is EXCLUDED from the public snapshot, so this test SKIPS (not errors) when the script
is absent — the publish-tree-divergence convention the script itself detects. Runs the non-network
checks directly (no git ls-remote) and asserts the finding contract.
"""
import importlib.util
import json
import sys
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


def test_importorskip_dependencies_are_bound_to_the_full_suite():
    """Every conditional test import must be covered by the loud full-dependency guard."""
    m = _load()
    m.check_test_skip_blindspots()
    hits = [f for f in m._findings if f["dimension"] == "test-skip-blindspots"]
    assert not hits, f"unbound importorskip dependencies: {[h['detail'] for h in hits]}"


def test_importorskip_audit_handles_malformed_and_unbound_dynamic_tests(tmp_path, monkeypatch):
    m = _load()
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_conformance_full_deps.py").write_text('REQUIRED_FULL_DEPS = {"yaml"}\n')
    (tests / "test_bad.py").write_text("def broken(:\n")
    (tests / "test_dynamic.py").write_text(
        "import pytest\nname = 'new_optional_dep'\npytest.importorskip(name)\n"
        "pytest.importorskip(resolve_dependency())\n")
    monkeypatch.setattr(m, "REPO", tmp_path)
    m.check_test_skip_blindspots()
    hits = [f for f in m._findings if f["dimension"] == "test-skip-blindspots"]
    assert len(hits) == 1 and "new_optional_dep" in hits[0]["detail"]


def test_constant_drift_default_token_agrees():
    """The built-in default MCP token must agree across server.py / hardening_lib / write_server /
    compose (the fail-closed guards compare against it)."""
    m = _load()
    m.check_constant_drift()
    drift = [f for f in m._findings if f["dimension"] == "constant-drift" and "TOKEN" in f["detail"]]
    assert not drift, drift


def test_record_run_tracks_recurrence(tmp_path):  # okengine#352
    """Persist audit findings run-over-run so 'same bug rediscovered' is a NUMBER: record_run appends
    the run's findings to a tracked history and returns each finding's recurrence (# of PRIOR runs
    that surfaced the same dimension|file|detail key). Idempotent per run_id."""
    m = _load()
    hist = tmp_path / "findings-history.jsonl"
    m._findings.clear()
    m.add("high", "scrub-parity", ".scrub-patterns", "token X scrubbed at publish but not pre-commit")
    m.add("medium", "pin-lag", "engine-manifest.yaml", "cron-plus pin behind upstream")
    rec1 = m.record_run("run1", hist)
    assert set(rec1.values()) == {0}, f"first run: nothing recurs — {rec1}"

    m._findings.clear()
    m.add("high", "scrub-parity", ".scrub-patterns", "token X scrubbed at publish but not pre-commit")  # REPEAT
    m.add("high", "constant-drift", "okengine-mcp/server.py", "default token disagreement")             # NEW
    rec2 = m.record_run("run2", hist)
    scrub_key = next(k for k in rec2 if k.startswith("scrub-parity|"))
    drift_key = next(k for k in rec2 if k.startswith("constant-drift|"))
    assert rec2[scrub_key] == 1, f"repeated finding must show 1 prior run — {rec2}"
    assert rec2[drift_key] == 0, f"new finding recurs 0 — {rec2}"

    # idempotent: re-recording run2 does not double-count, and history stays at 2 runs (not 3)
    rec2b = m.record_run("run2", hist)
    assert rec2b[scrub_key] == 1, rec2b
    assert len(m._load_history(hist)) == 2

    # the raw detail is NOT persisted (leak-safe for a tracked file)
    assert "token X scrubbed" not in hist.read_text()


def test_pin_lag_reports_unverifiable_and_behind(tmp_path, monkeypatch):
    m = _load()
    m.MANIFEST = tmp_path / "engine-manifest.yaml"
    sha = "a" * 40
    m.MANIFEST.write_text(
        f"one:\n  upstream: https://one.invalid/repo\n  pinned_sha: {sha}\n"
        f"two:\n  upstream: https://two.invalid/repo\n  pinned_sha: {sha}\n")
    replies = iter([(1, "offline"), (0, f"{'b' * 40}\\tHEAD\n")])
    monkeypatch.setattr(m, "_git", lambda *a, **kw: next(replies))

    m.check_pin_lag()

    details = [f["detail"] for f in m._findings]
    assert any("UNVERIFIABLE" in d for d in details)
    assert any("BEHIND" in d for d in details)


def test_scrub_parity_reports_missing_pattern_file_and_token(tmp_path):
    m = _load()
    m.PUBLISH = tmp_path / "publish.sh"
    m.SCRUB = tmp_path / ".scrub-patterns"
    m.PUBLISH.write_text("grep -riIlE 'secret-token|192[.]168' .\n")
    m.check_scrub_parity()
    assert any("absent" in f["detail"] for f in m._findings)

    m._findings.clear()
    m.SCRUB.write_text("something-else\n")
    m.check_scrub_parity()
    assert any("'secret-token'" in f["detail"] for f in m._findings)


def test_history_loader_ignores_blank_and_malformed_lines(tmp_path):
    m = _load()
    history = tmp_path / "history.jsonl"
    history.write_text('\n{broken}\n{"run":"ok","findings":[]}\n')
    assert m._load_history(history) == [{"run": "ok", "findings": []}]


def test_main_json_record_and_human_modes(monkeypatch, tmp_path, capsys):
    m = _load()
    checks = ["check_pin_lag", "check_scrub_parity", "check_publish_tree_divergence",
              "check_test_skip_blindspots", "check_constant_drift"]
    for name in checks:
        monkeypatch.setattr(m, name, lambda: None)

    monkeypatch.setattr(sys, "argv", ["audit", "--json"])
    assert m.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"] == [] and payload["count"] == 0
    assert "sha" in payload["source_context"]

    m.add("high", "dimension", "file", "detail")
    monkeypatch.setattr(sys, "argv", ["audit"])
    assert m.main() == 0
    assert "[HIGH] dimension" in capsys.readouterr().out

    m.REPO = tmp_path
    m.HISTORY = tmp_path / "history.jsonl"
    monkeypatch.setattr(sys, "argv", ["audit", "--record", "--run-id", "run-x"])
    assert m.main() == 0
    assert "recorded 1 finding" in capsys.readouterr().out


def test_git_and_pin_lag_remaining_edges(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(
        m.subprocess, "run",
        lambda *_a, **_k: type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})(),
    )
    assert m._git("status") == (0, "ok")
    monkeypatch.setattr(
        m.subprocess, "run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("git absent")),
    )
    assert m._git("status") == (1, "git absent")

    sha = "a" * 40
    m.MANIFEST = tmp_path / "manifest.yaml"
    m.MANIFEST.write_text(
        "one:\n  upstream: u1\n  no_pin: true\n"
        f"two:\n  upstream: same\n  pinned_sha: {sha}\n"
        f"three:\n  upstream: same\n  pinned_sha: {sha}\n"
    )
    monkeypatch.setattr(m, "_git", lambda *_a, **_k: (0, f"{sha} HEAD\n"))
    m.check_pin_lag()
    assert m._findings == []


def test_scrub_and_publish_divergence_unverifiable_and_finding(tmp_path):
    m = _load()
    m.PUBLISH = tmp_path / "publish.sh"
    m.PUBLISH.write_text("no scrub expression here\n")
    m.check_scrub_parity()
    assert any("could not find" in f["detail"] for f in m._findings)

    m._findings.clear()
    m.check_publish_tree_divergence()
    assert m._findings == []

    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "tests" / "test_read.py").write_text('Path("private/secret.md").read_text()\n')
    m.REPO = repo
    m.PUBLISH.write_text('EXCLUDE=("private/secret.md")\n')
    m.check_publish_tree_divergence()
    assert any(f["dimension"] == "publish-tree-divergence" for f in m._findings)


def test_constant_drift_reports_token_and_supply_chain_mismatch(tmp_path):
    m = _load()
    m.REPO = tmp_path
    m.MANIFEST = tmp_path / "engine-manifest.yaml"
    sha = "1" * 40
    m.MANIFEST.write_text(f"cron-plus:\n  pinned_sha: {sha}\n")
    files = {
        "okengine-mcp/server.py": 'DEFAULT_LOCAL_TOKEN = "one"\n',
        "scripts/cron/hardening_lib.py": 'DEFAULT_LOCAL_TOKEN = "two"\n',
        "okengine-mcp/write_server.py": 'DEFAULT_LOCAL_TOKEN = "one"\n',
        "templates/pack/skeleton/docker-compose.yml": "OKENGINE_MCP_TOKEN=${OKENGINE_MCP_TOKEN:-one}\n",
        "docs/supply-chain.md": "different pin\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    m.check_constant_drift()
    details = "\n".join(f["detail"] for f in m._findings)
    assert "disagree across surfaces" in details
    assert "not cited" in details

    (tmp_path / "okengine-mcp" / "server.py").write_text("no token here\n")
    m.MANIFEST.write_text("no cron pin\n")
    m._findings.clear()
    m.check_constant_drift()


def test_history_run_id_and_record_output_edges(tmp_path, monkeypatch, capsys):
    m = _load()
    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps({"run": "old", "findings": [{}, {"key": "known"}]}) + "\n")
    m._findings.clear()
    assert m.record_run("new", history) == {}

    monkeypatch.setattr(sys, "argv", ["audit", "--run-id"])
    monkeypatch.setattr(m, "_git", lambda *_a: (0, "abc123\n"))
    assert m._run_id() == "abc123"
    monkeypatch.setattr(m, "_git", lambda *_a: (1, ""))
    assert m._run_id() == "unknown"
    monkeypatch.setattr(sys, "argv", ["audit"])
    assert m._run_id() == "unknown"

    checks = ["check_pin_lag", "check_scrub_parity", "check_publish_tree_divergence",
              "check_test_skip_blindspots", "check_constant_drift"]
    for name in checks:
        monkeypatch.setattr(m, name, lambda: None)
    m._findings.clear()
    monkeypatch.setattr(sys, "argv", ["audit"])
    assert m.main() == 0
    assert "mechanical dimensions are clean" in capsys.readouterr().out

    m.add("high", "dim", "file", "detail")
    key = m._finding_key(m._findings[0])
    monkeypatch.setattr(m, "record_run", lambda _run: {key: 2})
    monkeypatch.setattr(sys, "argv", ["audit", "--record", "--run-id", "now"])
    assert m.main() == 0
    assert "×3" in capsys.readouterr().out
