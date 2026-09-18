"""okengine.grounding (Tier-2 semantic grounding): manifest valid + wake-gate fires only on
grounded, recent, unchecked entities."""
import importlib.util, sys
from pathlib import Path
import pytest
yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.grounding"


def test_manifest_valid():
    man = importlib.util.spec_from_file_location("extension_manifest", REPO / "scripts/extension_manifest.py")
    m = importlib.util.module_from_spec(man); sys.modules["extension_manifest"] = m; man.loader.exec_module(m)
    mani = yaml.safe_load((EXT / "extension.yaml").read_text())
    errors, _ = m.validate_manifest(mani)
    assert not errors, errors
    assert mani["operations"]["grounding-check"]["prompt_file"]   # agent lane


def _run(tmp, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp)); monkeypatch.setenv("GROUNDING_NAMESPACES", "entities")
    monkeypatch.setenv("GROUNDING_MIN", "1")
    spec = importlib.util.spec_from_file_location("sgc", EXT / "select_grounding_check.py")
    m = importlib.util.module_from_spec(spec); sys.modules["sgc"] = m
    import io, contextlib, json
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        spec.loader.exec_module(m); m.main()
    return json.loads(buf.getvalue().strip().splitlines()[-1])["wakeAgent"], buf.getvalue()


def test_wake_gate(tmp_path, monkeypatch):
    from datetime import date
    w = tmp_path / "wiki"
    (w / "sources/2026/06").mkdir(parents=True)
    (w / "sources/2026/06/rep.md").write_text("---\ntype: source\n---\n# r\n")
    e = w / "entities/a"; e.mkdir(parents=True)
    today = date.today().isoformat()
    # grounded + recent + unchecked -> candidate
    (e / "fresh.md").write_text(f"---\ntype: entity\nsources:\n- sources/2026/06/rep\nlast_updated: {today}\n---\n# f\n")
    # grounded but already checked -> excluded
    (e / "done.md").write_text(f"---\ntype: entity\nsources:\n- sources/2026/06/rep\nlast_updated: {today}\ngrounding_checked: {today}\n---\n# d\n")
    # ungrounded -> excluded (Tier-1's job)
    (e / "ungrounded.md").write_text(f"---\ntype: entity\nsources:\n- Vendor advisory\nlast_updated: {today}\n---\n# u\n")
    wake, out = _run(tmp_path, monkeypatch)
    assert wake is True and "fresh" in out
    assert "done" not in out.split("entities to verify")[-1] and "ungrounded" not in out.split("entities to verify")[-1]


def test_helpers_and_dry_gate_cover_defensive_shapes(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("GROUNDING_NAMESPACES", "missing,entities")
    monkeypatch.setenv("GROUNDING_MIN", "2")
    spec = importlib.util.spec_from_file_location("sgc_edges", EXT / "select_grounding_check.py")
    module = importlib.util.module_from_spec(spec); sys.modules["sgc_edges"] = module
    spec.loader.exec_module(module)
    assert module._d("2026-08-04T01:02:03Z") == "2026-08-04"
    assert module._d(None) == ""
    assert module._stem("[[sources/a/report.md]]") == "report"
    assert module._is_pageref("sources/a/report")
    assert module._is_pageref("report.md")
    assert not module._is_pageref("Vendor advisory")
    assert module._fm(tmp_path / "missing.md") == {}

    entities = tmp_path / "wiki/entities"
    entities.mkdir(parents=True)
    (entities / "_ignored.md").write_text("ignored")
    (entities / "INDEX.md").write_text("ignored")
    (entities / "plain.md").write_text("body")
    (entities / "sequence.md").write_text("---\n- list\n---\n")
    import contextlib, io, json
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        assert module.main() == 0
    output = stream.getvalue()
    assert json.loads(output.strip().splitlines()[-1])["wakeAgent"] is False
    assert "threshold" in output


def test_old_scalar_source_and_name_fallback_selection(tmp_path, monkeypatch):
    from datetime import date
    wiki = tmp_path / "wiki"
    (wiki / "sources").mkdir(parents=True)
    (wiki / "sources/report.md").write_text("---\ntype: source\n---\n")
    entities = wiki / "entities"
    entities.mkdir()
    (entities / "old.md").write_text(
        "---\ntype: entity\nsources: sources/report\nlast_updated: 2000-01-01\n---\n"
    )
    (entities / "fresh.md").write_text(
        f"---\ntype: entity\nsources: sources/report\ncreated: {date.today().isoformat()}\n---\n"
    )
    wake, output = _run(tmp_path, monkeypatch)
    assert wake is True and "## fresh" in output and "old" not in output.split("entities to verify")[-1]
