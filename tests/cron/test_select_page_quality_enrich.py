import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "select_page_quality_enrich.py"


def _load(vault: Path, home: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["OKENGINE_LANE_ID"] = "page-lane"
    os.environ.pop("PQ_ENRICH_QUEUE", None)
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("select_page_quality_enrich", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_with_queue(vault: Path, home: Path, queue: str):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["OKENGINE_LANE_ID"] = "page-lane"
    os.environ["PQ_ENRICH_QUEUE"] = queue
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("select_page_quality_enrich", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path):
    vault, home = tmp_path / "vault", tmp_path / "home"
    queue = vault / "wiki" / "operational" / "page-quality-queue.json"
    queue.parent.mkdir(parents=True)
    queue.write_text(json.dumps([{
        "page": "entities/acme", "tier": "thin", "words": 20,
        "sections": 0, "sources": 0, "inbound": 2,
    }]))
    source = vault / "wiki" / "sources" / "acme-report.md"
    source.parent.mkdir(parents=True)
    source.write_text("Acme operates the [[entities/acme]] platform.")
    page = vault / "wiki" / "entities" / "acme.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# Acme\n")
    return vault, home


def test_selector_emits_exact_receipt_contract(tmp_path, capsys):
    vault, home = _fixture(tmp_path)
    module = _load(vault, home)
    assert module.main() == 0
    output = capsys.readouterr().out
    manifest = json.loads(
        (home / "cron-plus/selections/page-quality-enrich.json").read_text())

    assert "```okengine-receipt" in output
    assert '"key": "wiki/entities/acme.md|sha256:' in output
    assert manifest["selected"][0].startswith("wiki/entities/acme.md|sha256:")
    state = json.loads((home / "scripts/page-quality-enrich-state.json").read_text())
    assert "entities/acme" not in state


def test_selector_supports_vault_relative_queue_override(tmp_path, capsys):
    vault, home = _fixture(tmp_path)
    alternate = vault / "wiki" / "operational" / "qualification-queue.json"
    alternate.write_text(json.dumps([{
        "page": "entities/alternate", "tier": "thin", "words": 10,
        "sections": 0, "sources": 0, "inbound": 1,
    }]))
    source = vault / "wiki" / "sources" / "alternate-report.md"
    source.write_text("The [[entities/alternate]] page has controlled evidence.")
    page = vault / "wiki" / "entities" / "alternate.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# Alternate\n")

    module = _load_with_queue(
        vault, home, "wiki/operational/qualification-queue.json")
    assert module.main() == 0
    capsys.readouterr()
    manifest = json.loads(
        (home / "cron-plus/selections/page-quality-enrich.json").read_text())
    assert manifest["selected"][0].startswith("wiki/entities/alternate.md|sha256:")


def test_selector_deduplicates_repeated_queue_pages(tmp_path, capsys):
    vault, home = _fixture(tmp_path)
    queue = vault / "wiki" / "operational" / "page-quality-queue.json"
    row = json.loads(queue.read_text())[0]
    queue.write_text(json.dumps([row, dict(row)]))
    module = _load(vault, home)
    assert module.main() == 0
    output = capsys.readouterr().out
    manifest = json.loads(
        (home / "cron-plus/selections/page-quality-enrich.json").read_text())
    assert manifest["selected"][0].startswith("wiki/entities/acme.md|sha256:")
    assert output.count('"key": "wiki/entities/acme.md|sha256:') == 1


def test_default_batch_is_one_for_attributable_receipts(tmp_path, capsys):
    vault, home = _fixture(tmp_path)
    queue = vault / "wiki" / "operational" / "page-quality-queue.json"
    rows = json.loads(queue.read_text())
    rows.append({
        "page": "entities/other", "tier": "thin", "words": 10,
        "sections": 0, "sources": 0, "inbound": 1,
    })
    queue.write_text(json.dumps(rows))
    source = vault / "wiki" / "sources" / "other-report.md"
    source.write_text("The [[entities/other]] platform has local evidence.")
    page = vault / "wiki" / "entities" / "other.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# Other\n")
    module = _load(vault, home)
    assert module.main() == 0
    capsys.readouterr()
    manifest = json.loads(
        (home / "cron-plus/selections/page-quality-enrich.json").read_text())
    assert manifest["selected"][0].startswith("wiki/entities/acme.md|sha256:")


def test_selector_excludes_index_pages(tmp_path, capsys):
    vault, home = _fixture(tmp_path)
    queue = vault / "wiki" / "operational" / "page-quality-queue.json"
    rows = json.loads(queue.read_text())
    rows.insert(0, {
        "page": "sources/INDEX", "tier": "thin", "words": 5,
        "sections": 0, "sources": 0, "inbound": 100,
    })
    queue.write_text(json.dumps(rows))
    index_ref = vault / "wiki" / "sources" / "index-ref.md"
    index_ref.write_text("See [[sources/INDEX]] for navigation.")
    module = _load(vault, home)
    assert module.main() == 0
    capsys.readouterr()
    manifest = json.loads(
        (home / "cron-plus/selections/page-quality-enrich.json").read_text())
    assert manifest["selected"][0].startswith("wiki/entities/acme.md|sha256:")


def test_only_verified_terminal_receipt_starts_cooldown(tmp_path, capsys):
    vault, home = _fixture(tmp_path)
    module = _load(vault, home)
    assert module.main() == 0
    manifest = json.loads(
        (home / "cron-plus/selections/page-quality-enrich.json").read_text())
    capsys.readouterr()
    receipt_dir = home / "cron-plus/receipts/page-lane"
    receipt_dir.mkdir(parents=True)
    receipt_dir.joinpath("failed.json").write_text(json.dumps({
        "valid": True, "receipt": {"items": [{
            "key": "entities/acme", "disposition": "failed",
            "reason": "provider unavailable", "writes": [],
        }]},
    }))
    module = _load(vault, home)
    assert module.main() == 0
    assert '"wakeAgent": true' in capsys.readouterr().out

    receipt_dir.joinpath("accepted.json").write_text(json.dumps({
        "valid": True, "receipt": {"items": [{
            "key": manifest["selected"][0], "disposition": "accepted",
            "writes": [{"path": "wiki/entities/acme.md", "sha256": "sha256:x"}],
        }]},
    }))
    module = _load(vault, home)
    assert module.main() == 0
    assert '"wakeAgent": false' in capsys.readouterr().out
    state = json.loads((home / "scripts/page-quality-enrich-state.json").read_text())
    assert state["entities/acme"] == datetime.now(timezone.utc).date().isoformat()


def test_state_receipt_date_and_excerpt_edge_paths(tmp_path, monkeypatch):
    vault, home = _fixture(tmp_path)
    module = _load(vault, home)
    monkeypatch.setattr(module, "STATE", tmp_path / "state.json")
    original_write = Path.write_text
    monkeypatch.setattr(
        Path, "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("readonly"))
        if self == module.STATE else original_write(self, *a, **k),
    )
    module._save_state({"x": 1})

    monkeypatch.delenv("OKENGINE_LANE_ID", raising=False)
    assert module._import_receipts({}, datetime.now(timezone.utc).date()) == 0
    monkeypatch.setenv("OKENGINE_LANE_ID", "page-lane")
    receipt_dir = home / "cron-plus/receipts/page-lane"
    receipt_dir.mkdir(parents=True)
    malformed = receipt_dir / "a.json"
    malformed.write_text("{")
    invalid = receipt_dir / "b.json"
    invalid.write_text(json.dumps({"valid": False}))
    accepted = receipt_dir / "c.json"
    accepted.write_text(json.dumps({"items": [
        {"key": "wiki/entities/acme.md|sha256:x", "disposition": "duplicate"},
        {"key": "entities/ignored", "disposition": "failed"},
    ]}))
    already = receipt_dir / "d.json"
    already.write_text("{}")
    state = {"_imported_receipts": [str(already.resolve())]}
    assert module._import_receipts(state, datetime.now(timezone.utc).date()) == 1
    assert "entities/acme" in state
    assert module._days_since("bad", datetime.now(timezone.utc).date()) == 10_000
    assert module._excerpt_around("no relevant target", "missing-stem") == ""


def test_main_missing_bad_queue_source_edges_and_empty_batch(tmp_path, monkeypatch, capsys):
    vault, home = _fixture(tmp_path)
    module = _load(vault, home)
    monkeypatch.setattr(module, "QUEUE", tmp_path / "missing.json")
    assert module.main() == 0
    assert "no page-quality queue" in capsys.readouterr().out
    bad_queue = tmp_path / "bad.json"
    bad_queue.write_text("{")
    monkeypatch.setattr(module, "QUEUE", bad_queue)
    assert module.main() == 0
    assert '"wakeAgent": false' in capsys.readouterr().out

    queue = vault / "wiki/operational/page-quality-queue.json"
    monkeypatch.setattr(module, "QUEUE", queue)
    source_dir = vault / "wiki/sources"
    archived = source_dir / "_archive/old.md"
    archived.parent.mkdir()
    archived.write_text("[[entities/acme]]")
    hidden = source_dir / "_skip.md"
    hidden.write_text("[[entities/acme]]")
    unreadable = source_dir / "unreadable.md"
    unreadable.write_text("[[entities/acme]]")
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == unreadable else original_read(self, *a, **k),
    )
    # Remove the target, leaving an eligible queue row with inbound context but no page.
    (vault / "wiki/entities/acme.md").unlink()
    assert module.main() == 0
    assert "nothing to enrich" in capsys.readouterr().out


def test_context_cap_and_batch_loop_exhaustion(tmp_path, monkeypatch):
    vault, home = _fixture(tmp_path)
    source2 = vault / "wiki/sources/acme-second.md"
    source2.write_text("Another sentence about [[entities/acme]].")
    module = _load(vault, home)
    monkeypatch.setattr(module, "CTX", 1)
    monkeypatch.setattr(module, "BATCH", 2)
    assert module.main() == 0
    manifest = json.loads((home / "cron-plus/selections/page-quality-enrich.json").read_text())
    assert len(manifest["selected"]) == 1
