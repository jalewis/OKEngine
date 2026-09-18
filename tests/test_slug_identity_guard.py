import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "patches" / "cron-plus" / "slug_identity_guard.py"
SPEC = importlib.util.spec_from_file_location("slug_identity_guard_test", MODULE)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

ID_SPEC = importlib.util.spec_from_file_location(
    "slug_identity_guard_id_lib", ROOT / "scripts" / "cron" / "id_lib.py"
)
id_lib = importlib.util.module_from_spec(ID_SPEC)
ID_SPEC.loader.exec_module(id_lib)
guard._ID_LIB = id_lib


def _page(wiki: Path, rel: str, *, status: str = "active", body: str = "body") -> Path:
    path = wiki / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: entity\nstatus: {status}\n---\n{body}\n")
    return path


def test_identity_library_loads_from_flat_deployed_scripts_directory(monkeypatch):
    monkeypatch.setenv("OKENGINE_CRON_SCRIPTS", str(ROOT / "scripts" / "cron"))
    monkeypatch.setattr(guard, "_ID_LIB", None)
    loaded = guard._id_lib()
    assert loaded.slug_identity("Agent-Tesla") == "agenttesla"
    assert '"/opt/data/scripts"' in MODULE.read_text()
    guard._ID_LIB = id_lib


def test_direct_writer_collision_fails_and_preserves_quarantined_content(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/a/agent-tesla.md", body="canonical")
    before = guard.snapshot(wiki)
    candidate = _page(wiki, "entities/a/agenttesla.md", body="candidate evidence")

    with pytest.raises(guard.SlugIdentityCollision, match="quarantined outside wiki"):
        guard.enforce(wiki, before, tmp_path / "quarantine", job_id="direct-writer")

    assert not candidate.exists()
    quarantined = list((tmp_path / "quarantine").glob("*/entities/a/agenttesla.md"))
    assert len(quarantined) == 1
    assert "candidate evidence" in quarantined[0].read_text()
    receipt = quarantined[0].parents[2] / "receipt.json"
    assert "direct-writer" in receipt.read_text()
    assert "entities/a/agent-tesla.md" in receipt.read_text()


def test_existing_collision_does_not_fail_a_noop_run(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/a/agent-tesla.md")
    _page(wiki, "entities/a/agenttesla.md")
    before = guard.snapshot(wiki)
    assert guard.enforce(wiki, before, tmp_path / "quarantine", job_id="noop") == []


def test_tombstone_numeric_and_cross_namespace_do_not_false_positive(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/a/agent-tesla.md", status="tombstoned")
    _page(wiki, "indicators/1/110-37-3-251.md")
    before = guard.snapshot(wiki)
    _page(wiki, "entities/a/agenttesla.md")
    _page(wiki, "concepts/a/agent-tesla.md")
    _page(wiki, "indicators/1/110373251.md")
    assert guard.enforce(wiki, before, tmp_path / "quarantine", job_id="valid") == []


def test_nested_pack_namespaces_are_isolated(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "acme").mkdir(parents=True)
    (wiki / "acme" / "schema.yaml").write_text("types: {}\n")
    _page(wiki, "entities/a/agent-tesla.md")
    before = guard.snapshot(wiki)
    _page(wiki, "acme/entities/a/agenttesla.md")
    assert guard.enforce(wiki, before, tmp_path / "quarantine", job_id="nested") == []


def test_runner_enforces_after_failed_as_well_as_successful_direct_writers():
    patch = (ROOT / "patches" / "cron-plus" / "slug-identity-guard.patch").read_text()
    assert "if slug_snapshot is not None:" in patch
    assert "if success and slug_snapshot is not None:" not in patch
