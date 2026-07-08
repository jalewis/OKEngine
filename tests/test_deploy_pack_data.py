"""okengine invariant-audit #9: deploy-cron-scripts.sh staged pack domain data from a hardcoded
2-file allowlist, silently dropping any other table a pack ships under data/. The documented
contract (docs/deploy-a-new-domain.md) is `data/*` — the WHOLE data/ tree. This guards the
enumeration (scripts/lib/pack_data.sh) AND that the deploy script actually uses it instead of a
curated list.

A pack that ships a THIRD data table must have it staged, or its domain cron FileNotFoundErrors at
the scheduled tick — a silent no-op lane the deploy reports as success.
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "pack_data.sh"
DEPLOY = REPO / "scripts" / "deploy-cron-scripts.sh"


def _enumerate(pack_data):
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; enumerate_pack_data_files "{pack_data}"'],
        capture_output=True, text=True, env=dict(os.environ),
    )
    assert r.returncode == 0, r.stderr
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def test_lib_exists_and_sources_clean():
    assert LIB.is_file(), "scripts/lib/pack_data.sh missing"
    r = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_deploy_script_parses():
    r = subprocess.run(["bash", "-n", str(DEPLOY)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_third_data_table_is_enumerated(tmp_path):
    """The core regression: a pack shipping a THIRD table beyond the two once-hardcoded names has it
    staged. Fails before the fix (the allowlist never named it); passes after (whole data/ tree)."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "pack-table-a.yaml").write_text("x\n")
    (data / "curated-entity-fields.json").write_text("{}\n")
    (data / "my-new-table.yaml").write_text("rows: []\n")  # the previously-dropped file
    got = _enumerate(data)
    assert got == ["curated-entity-fields.json", "my-new-table.yaml", "pack-table-a.yaml"], got


def test_gitkeep_placeholder_is_skipped(tmp_path):
    """A data-less pack ships only .gitkeep; it must not be staged as a bogus config file."""
    data = tmp_path / "data"
    data.mkdir()
    (data / ".gitkeep").write_text("")
    assert _enumerate(data) == []


def test_subdirs_are_not_descended(tmp_path):
    """maxdepth 1: only top-level tables stage (the tar streams from within data/, flat)."""
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "sub" / "nested.yaml").write_text("x\n")
    (data / "top.yaml").write_text("x\n")
    assert _enumerate(data) == ["top.yaml"]


def test_missing_data_dir_is_empty(tmp_path):
    assert _enumerate(tmp_path / "nope") == []


def test_deploy_script_uses_enumeration_not_allowlist():
    """The script must call the enumeration and must NOT carry the hardcoded allowlist loop (which
    also embedded a pack-specific data filename — a domain leak — in engine code)."""
    dp = DEPLOY.read_text()
    assert "enumerate_pack_data_files" in dp, "deploy script no longer enumerates the whole data/ tree"
    assert "for cfg in " not in dp, "deploy script still hardcodes a data-file allowlist loop"
