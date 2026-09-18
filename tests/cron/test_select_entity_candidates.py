"""Regression: the entity-backfill digest must not crash when an entity page
carries a non-string `tags` member (e.g. a bare YAML year `2024`) or a scalar
`tags` value. The write path coerces a scalar STRING but not numeric scalars or
list members (okengine#196), so such a page reaches storage verbatim and the
naive `", ".join(e["tags"][:5])` would raise TypeError, aborting the whole
digest for the entire vault.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "select_entity_candidates.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path, home: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["HERMES_HOME"] = str(home)
    if str(MOD.parent) not in sys.path:
        sys.path.insert(0, str(MOD.parent))
    sys.modules.pop("select_entity_candidates", None)
    spec = importlib.util.spec_from_file_location("select_entity_candidates", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write(vault: Path, rel: str, body: str) -> None:
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_digest_does_not_inline_entity_inventory(tmp_path, capsys):
    vault = tmp_path / "vault"
    home = tmp_path / "home"
    # A source makes the wake-gate emit a digest.
    _write(vault, "sources/s1.md",
           "---\ntype: source\npublisher: Acme\n---\n# A source\n\nCovers Acme and Beta.\n")
    # Legacy inventory may contain malformed tags, but the digest no longer
    # exposes entity paths: governed converge owns identity/alias dedupe.
    _write(vault, "entities/a/acme.md", "---\ntype: vendor\ntags: [2024, ai-labs]\n---\n# Acme\n")
    _write(vault, "entities/b/beta.md", "---\ntype: vendor\ntags: 2024\n---\n# Beta\n")

    m = _load(vault, home)
    rc = m.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "entities/acme" not in out
    assert "entities/beta" not in out
    assert "Do not search, list, or read entity pages" in out


def test_repaired_source_revision_wakes_entity_backfill(tmp_path, capsys):
    vault, home = tmp_path / "vault", tmp_path / "home"
    _write(vault, "sources/2026/07/qilin.md",
           "---\ntype: source\npublisher: CCB\n---\n# Qilin\n")
    state = home / "scripts/entity-backfill-state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"sources":["qilin.md"],"entities":[]}')
    m = _load(vault, home)
    assert m.main() == 0
    capsys.readouterr()

    _write(vault, "sources/2026/07/qilin.md",
           "---\ntype: source\npublisher: CCB\n---\n# Qilin\n\nRussian-aligned assessment.\n")
    m = _load(vault, home)
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "sources/2026/07/qilin" in out
    assert "REPAIRED/REVISED SINCE LAST RUN" in out


def test_derived_indexes_and_control_character_paths_are_not_candidates(
        tmp_path, capsys):
    vault, home = tmp_path / "vault", tmp_path / "home"
    _write(vault, "sources/2026/07/good.md",
           "---\ntype: source\n---\n# Good source\n")
    _write(vault, "sources/2026/07/INDEX.md", "# Derived index\n")
    _write(vault, "sources/2026/07/bad\nfragment.md", "# Corrupt path\n")

    m = _load(vault, home)
    assert m.main() == 0
    out = capsys.readouterr().out
    selection = __import__("json").loads(
        (vault / ".okengine/entity-reconciliation-selection.json").read_text())

    assert "sources/2026/07/good" in out
    assert len(selection["selected"]) == 1
    assert selection["selected"][0].startswith("2026/07/good.md|sha256:")


def test_source_revision_overflow_remains_pending(tmp_path, capsys, monkeypatch):
    vault, home = tmp_path / "vault", tmp_path / "home"
    monkeypatch.setenv("ENTITY_RECENT_SOURCES", "1")
    monkeypatch.setenv("OKENGINE_LANE_ID", "entity")
    _write(vault, "sources/a.md", "---\ntype: source\n---\n# A\n")
    _write(vault, "sources/b.md", "---\ntype: source\n---\n# B\n")
    m = _load(vault, home)
    assert m.main() == 0
    first = capsys.readouterr().out
    assert "Source revisions left pending after this window:** 1" in first

    selection = __import__("json").loads(
        (vault / ".okengine/entity-reconciliation-selection.json").read_text())
    receipt_dir = home / "cron-plus/receipts/entity"
    receipt_dir.mkdir(parents=True)
    receipt_dir.joinpath("run.json").write_text(__import__("json").dumps({
        "valid": True, "receipt": {"items": [{
            "key": selection["selected"][0], "disposition": "skipped",
            "reason": "no durable entity evidence", "writes": []}]}}))

    m = _load(vault, home)
    assert m.main() == 0
    second = capsys.readouterr().out
    assert "Pending source revisions in this window:** 1" in second
    assert "Source revisions left pending after this window:** 0" in second


def test_failed_or_deferred_receipt_does_not_acknowledge_revision(tmp_path, capsys, monkeypatch):
    vault, home = tmp_path / "vault", tmp_path / "home"
    monkeypatch.setenv("OKENGINE_LANE_ID", "entity")
    _write(vault, "sources/a.md", "---\ntype: source\n---\n# A\n")
    m = _load(vault, home)
    assert m.main() == 0
    selection = __import__("json").loads(
        (vault / ".okengine/entity-reconciliation-selection.json").read_text())
    capsys.readouterr()
    receipt_dir = home / "cron-plus/receipts/entity"
    receipt_dir.mkdir(parents=True)
    receipt_dir.joinpath("run.json").write_text(__import__("json").dumps({
        "valid": True, "receipt": {"items": [{
            "key": selection["selected"][0], "disposition": "deferred",
            "reason": "transient model failure", "writes": []}]}}))
    m = _load(vault, home)
    assert m.main() == 0
    assert "sources/a" in capsys.readouterr().out


def test_frontmatter_title_state_and_related_entity_edge_paths(tmp_path):
    m=_load(tmp_path,tmp_path/"home")
    assert m.parse_frontmatter("plain")=={}
    assert m.parse_frontmatter("---\n[bad\n---\n")=={}
    assert m.parse_frontmatter("---\n- x\n---\n")=={}
    assert m.first_h1("---\ntype: x\n---\n\n# Title\n")=="Title"
    assert m.first_h1("- bullet\n> quote\nFirst prose\n")=="First prose"
    assert m.first_h1("## only")==""
    assert m.related_entities([{"title":"Acme Corp","slug":"acme"}],[])==[]
    assert m.related_entities([
      {"title":"Acme Corp","slug":"acme"},
      {"title":"","slug":"other"}],["Acme Corp report"])[0]["slug"]=="acme"
    m.STATE_PATH=tmp_path/"missing.json"
    assert m.load_state()["source_revisions"]=={}
    m.STATE_PATH.write_text("{bad")
    assert m.load_state()["sources"]==[]


def test_receipt_import_malformed_invalid_and_valid(tmp_path,monkeypatch):
    home=tmp_path/"home";m=_load(tmp_path,home)
    assert m.import_receipts({})==0
    monkeypatch.setenv("OKENGINE_LANE_ID","lane")
    receipt_dir=home/"cron-plus/receipts/lane";receipt_dir.mkdir(parents=True)
    (receipt_dir/"bad.json").write_text("{")
    (receipt_dir/"invalid.json").write_text('{"valid":false,"receipt":{"items":[]}}')
    (receipt_dir/"valid.json").write_text(
      '{"items":[null,{"key":"bad","disposition":"accepted"},'
      '{"key":"a.md|sha256:abc","disposition":"accepted"}]}')
    state={}
    assert m.import_receipts(state)==1
    assert state["source_revisions"]=={"a.md":"sha256:abc"}
    assert m.import_receipts(state)==0


def test_main_missing_vault_sources_and_controlled_target(tmp_path,monkeypatch,capsys):
    m=_load(tmp_path/"missing",tmp_path/"home");assert m.main()==1
    vault=tmp_path/"vault";(vault/"wiki").mkdir(parents=True)
    m=_load(vault,tmp_path/"home2");assert m.main()==1
    _write(vault,"sources/a.md","---\ntype: source\n---\n# A\n")
    m=_load(vault,tmp_path/"home3");monkeypatch.setattr(m,"CONTROLLED_TARGET","sources/nope")
    assert m.main()==0 and "controlled target is not pending" in capsys.readouterr().out


def test_candidate_outside_root_and_controlled_pending_with_truncated_evidence(
        tmp_path, monkeypatch, capsys):
    vault, home = tmp_path / "vault", tmp_path / "home"
    _write(vault, "sources/a.md", "---\ntype: source\n---\n# A\n" + "evidence " * 20)
    m = _load(vault, home)
    assert not m.is_candidate_source(tmp_path / "outside.md", vault / "wiki/sources")
    monkeypatch.setattr(m, "CONTROLLED_TARGET", "sources/a.md")
    monkeypatch.setattr(m, "SOURCE_EVIDENCE_MAX_CHARS", 20)
    assert m.main() == 0
    output = capsys.readouterr().out
    assert "controlled target is not pending" not in output
    assert "Evidence truncated at 20" in output


def test_nonmapping_revision_state_stat_and_read_races(tmp_path, monkeypatch, capsys):
    vault, home = tmp_path / "vault", tmp_path / "home"
    source = vault / "wiki/sources/a.md"
    _write(vault, "sources/a.md", "---\ntype: source\n---\n# A\n")
    state = home / "scripts/entity-backfill-state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"source_revisions": [], "entities": []}')
    m = _load(vault, home)
    monkeypatch.setattr(m, "load_state", lambda: {"source_revisions": ["invalid"], "entities": []})
    monkeypatch.setattr(m, "import_receipts", lambda _state: 0)
    original_stat = Path.stat
    original_read = Path.read_text

    def flaky_stat(path, *args, **kwargs):
        if path == source:
            raise OSError("vanished during sort")
        return original_stat(path, *args, **kwargs)

    reads = {source: 0}
    def flaky_read(path, *args, **kwargs):
        if path == source:
            reads[source] += 1
            raise OSError("vanished during digest")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    monkeypatch.setattr(Path, "read_text", flaky_read)
    assert m.main() == 0
    assert "Pending source revisions in this window:** 0" in capsys.readouterr().out
