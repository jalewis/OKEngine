import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(tmp, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    spec = importlib.util.spec_from_file_location(
        "reference_integrity", REPO / "scripts/cron/reference_integrity.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["reference_integrity"] = module
    spec.loader.exec_module(module)
    return module


def _page(root, rel, fm="", body=""):
    path = root / "wiki" / f"{rel}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: entity\n{fm}---\n{body}", encoding="utf-8")
    return path


def test_exact_path_audit_classifies_unique_ambiguous_and_missing(tmp_path, monkeypatch):
    _page(tmp_path, "sources/2026/07/03/moved", "type: source\n")
    _page(tmp_path, "sources/a/dup", "type: source\n")
    _page(tmp_path, "sources/b/dup", "type: source\n")
    _page(
        tmp_path, "entities/x",
        "sources:\n- sources/2026/07/moved\n- sources/old/dup\n- sources/no/such\n",
        "[[sources/2026/07/moved]]",
    )
    module = _load(tmp_path, monkeypatch)
    assert module.main([]) == 0
    report = json.loads((tmp_path / "wiki/dashboards/reference-integrity.json").read_text())
    by_target = {item["target"]: item for item in report["findings"]}
    assert by_target["sources/2026/07/moved"]["classification"] == "unique-relocation"
    assert by_target["sources/old/dup"]["classification"] == "ambiguous-relocation"
    assert by_target["sources/no/such"]["classification"] == "no-candidate"
    assert len(by_target["sources/2026/07/moved"]["inbound"]) == 2


def test_repair_only_rewrites_unique_basename_relocations(tmp_path, monkeypatch):
    _page(tmp_path, "sources/2026/07/03/moved", "type: source\n")
    _page(tmp_path, "sources/2026/07/04/also-moved", "type: source\n")
    page = _page(
        tmp_path, "entities/x",
        "sources:\n- sources/2026/07/moved\n- sources/2026/07/also-moved\n- sources/no/such\n",
        "[[sources/2026/07/moved|label]]",
    )
    module = _load(tmp_path, monkeypatch)
    assert module.main(["--repair"]) == 0
    text = page.read_text()
    assert text.count("sources/2026/07/03/moved") == 2
    assert "sources/2026/07/04/also-moved" in text
    assert "sources/2026/07/moved" not in text
    assert "sources/2026/07/also-moved" not in text
    assert "sources/no/such" in text


def test_inventory_parse_read_and_non_source_edge_paths(tmp_path, monkeypatch):
    plain = tmp_path / "wiki/plain.md"; plain.parent.mkdir(parents=True); plain.write_text(
        "[[entities/not-source]] [[sources/missing]]")
    malformed = tmp_path / "wiki/malformed.md"
    malformed.write_text("---\n[bad\n---\n[[entities/nope]]")
    scalar = _page(tmp_path, "entities/scalar", "sources:\n- 3\n- entities/not-source\n")
    unreadable = tmp_path / "wiki/unreadable.md"; unreadable.write_text("text")
    module = _load(tmp_path, monkeypatch)
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs:
                        (_ for _ in ()).throw(OSError("race"))
                        if path == unreadable else original(path, *args, **kwargs))
    findings, _ = module._inventory()
    assert any(item["target"] == "sources/missing" for item in findings)
    assert scalar.is_file()


def test_repair_ignores_nonmatching_actions_and_missing_wiki(tmp_path, monkeypatch, capsys):
    module = _load(tmp_path, monkeypatch)
    plain = tmp_path / "plain.md"; plain.write_text("body")
    front = tmp_path / "front.md"; front.write_text("---\ntype: note\n---\nbody")
    pages, changes = module._repair({
        plain: [("frontmatter:sources", "old", "new"),
                ("other", "old", "new"),
                ("body:wikilink", "missing", "new")],
        front: [("frontmatter:sources", "missing", "new")],
    })
    assert (pages, changes) == (0, 0)
    monkeypatch.setattr(module, "WIKI", tmp_path / "missing")
    assert module.main([]) == 1
    assert "wiki missing" in capsys.readouterr().err
