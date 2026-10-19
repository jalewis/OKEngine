"""okengine#1034: corpus_audit.py must parse its arguments before touching the vault.

A ``--help`` probe on a live deployment ran the full audit as root and rewrote
``wiki/dashboards/corpus-audit.md``, the artifact the scheduled lane owns. These tests run the
real script as a subprocess (the way an operator or cron-plus invokes it) against a disposable
vault and assert that ``--help`` and an unknown argument leave the vault byte-for-byte unchanged,
and that ``--report-only`` / ``--dry-run`` compute the metrics without writing the dashboard.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "corpus_audit.py"
DASHBOARD = Path("wiki") / "dashboards" / "corpus-audit.md"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    page = vault / "wiki" / "entities" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: entity\nname: Alpha\n---\n\nx\n", encoding="utf-8")
    (vault / "schema.yaml").write_text("types:\n  entity:\n    required: [type]\n", encoding="utf-8")
    return vault


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _run(vault: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WIKI_PATH": str(vault),
           "PYTHONPATH": os.pathsep.join(str(REPO / d) for d in (".", "src", "scripts/cron"))}
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=vault, env=env,
                          capture_output=True, text=True, timeout=120)


def test_help_exits_zero_and_writes_nothing(tmp_path):
    vault = _vault(tmp_path)
    before = _snapshot(vault)
    proc = _run(vault, "--help")
    assert proc.returncode == 0, proc.stderr
    assert "--report-only" in proc.stdout
    assert _snapshot(vault) == before, "--help must not run the audit or write the dashboard"


@pytest.mark.parametrize("bad", ["--bogus", "positional", "--report-only=yes"])
def test_unknown_argument_exits_two_and_writes_nothing(tmp_path, bad):
    vault = _vault(tmp_path)
    before = _snapshot(vault)
    proc = _run(vault, bad)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert _snapshot(vault) == before, f"{bad!r} must be rejected before the audit runs"


@pytest.mark.parametrize("flag", ["--report-only", "--dry-run"])
def test_report_only_prints_metrics_without_writing_the_dashboard(tmp_path, flag):
    vault = _vault(tmp_path)
    before = _snapshot(vault)
    proc = _run(vault, flag)
    assert proc.returncode == 0, proc.stderr
    assert "corpus-audit | 1 pages" in proc.stdout
    assert "report-only: dashboard NOT written" in proc.stdout
    assert not (vault / DASHBOARD).exists()
    assert _snapshot(vault) == before


def test_no_argument_still_writes_the_dashboard(tmp_path):
    """The scheduled lane passes no arguments; the default behavior is unchanged."""
    vault = _vault(tmp_path)
    proc = _run(vault)
    assert proc.returncode == 0, proc.stderr
    assert (vault / DASHBOARD).is_file()
    assert "report-only" not in proc.stdout


def test_report_only_in_process_skips_the_dashboard_write(tmp_path, capsys):
    """The same contract through main(argv), as an importing consumer calls it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("corpus_audit_cli_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    vault = _vault(tmp_path)
    mod.VAULT, mod.WIKI, mod.DASH_DIR = vault, vault / "wiki", vault / "wiki" / "dashboards"
    assert mod.main(["--report-only"]) == 0
    assert not (vault / DASHBOARD).exists()
    assert "report-only: dashboard NOT written" in capsys.readouterr().out
    assert mod.main([]) == 0
    assert (vault / DASHBOARD).is_file()
