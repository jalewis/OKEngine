"""okengine.dedupe — name/alias duplicate detection + wake-gate."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "select_dup", REPO / "extensions" / "okengine.dedupe" / "select_dup_candidates.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ent(wiki, slug, **fm):
    p = wiki / "entities" / (slug + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: [{', '.join(v)}]" if isinstance(v, list) else f"{k}: {v}")
    p.write_text("\n".join(lines) + "\n---\nbody\n", encoding="utf-8")


def test_norm_collapses_case_and_punct():
    m = _mod()
    assert m._norm("OpenAI GPT-5") == m._norm("openai gpt 5") == "openaigpt5"


def test_find_groups_name_and_alias_collision(tmp_path):
    m = _mod(); wiki = tmp_path / "wiki"
    _ent(wiki, "g/gpt-5", title="GPT-5", type="model")
    _ent(wiki, "g/gpt5-variant", title="GPT 5", type="model")            # name collides
    _ent(wiki, "o/openai-thing", title="Some Thing", aliases=["GPT-5"])  # alias collides → joins
    _ent(wiki, "a/acme", title="Acme", type="org")                       # singleton
    _ent(wiki, "d/dead", title="GPT-5", status="tombstoned")             # skipped
    pages = m.scan(wiki / "entities", tmp_path)
    assert "entities/d/dead" not in pages
    groups = m.find_groups(pages)
    gpt5 = [set(members) for key, members in groups if "gpt5" in key]
    assert gpt5 and {"entities/g/gpt-5", "entities/g/gpt5-variant", "entities/o/openai-thing"} <= gpt5[0]
    assert all("entities/a/acme" not in members for _, members in groups)         # singleton not grouped


def test_wakegate_false_when_no_candidates(tmp_path, monkeypatch, capsys):
    m = _mod()
    monkeypatch.setattr(m, "ENTITIES", tmp_path / "none"); monkeypatch.setattr(m, "VAULT", tmp_path)
    assert m.main() == 0 and '"wakeAgent": false' in capsys.readouterr().out


def test_wakegate_true_with_candidates(tmp_path, monkeypatch, capsys):
    m = _mod(); wiki = tmp_path / "wiki"
    _ent(wiki, "g/gpt-5", title="GPT-5"); _ent(wiki, "g/gpt5-dup", title="GPT 5")
    monkeypatch.setattr(m, "ENTITIES", wiki / "entities"); monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(m, "MANIFEST", tmp_path / "selection.json")
    assert m.main() == 0 and '"wakeAgent": true' in capsys.readouterr().out


def test_scan_tolerates_non_string_scalar_aliases(tmp_path):  # invariant-audit #28
    """The write path only coerces a scalar STRING aliases -> list; a bare int/bool/date lands as a
    scalar and `{_norm(a) for a in 8220}` raised TypeError, killing the whole dedupe wake-gate."""
    m = _mod()
    wiki = tmp_path / "wiki"
    p = wiki / "entities" / "n" / "numeric.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: entity\nname: Numeric\naliases: 8220\n---\nbody\n")   # bare YAML int
    pages = m.scan(wiki / "entities", tmp_path)                                    # must not raise
    assert "entities/n/numeric" in pages
    assert pages["entities/n/numeric"]["aliases"] == {"8220"}


def test_scan_excludes_generated_indexes(tmp_path):
    m = _mod(); wiki = tmp_path / "wiki"
    _ent(wiki, "a/INDEX", title="Index: entities/a")
    _ent(wiki, "a/_/INDEX", title="Index: entities/a")
    assert m.scan(wiki / "entities", tmp_path) == {}


def test_frontmatter_scan_and_group_defensive_edges(tmp_path, monkeypatch):
    m = _mod()
    assert m._frontmatter("plain") == {}
    assert m._frontmatter("---\n[broken\n---\n") == {}
    monkeypatch.setattr(m, "yaml", None)
    assert m._frontmatter("---\na: b\n---\n") == {}

    m = _mod()
    wiki = tmp_path / "wiki"
    page = wiki / "entities/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: A\n---\n")
    original = Path.read_text

    def raced(path, *args, **kwargs):
        if path == page:
            raise OSError("vanished")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", raced)
    assert m.scan(wiki / "entities", tmp_path) == {}
    assert m.find_groups({"empty": {"norm": "", "aliases": set()}}) == []
    assert m._select_rotating([], 2, tmp_path / "state.json") == []
    assert m._select_rotating([("x", ["a", "b"])], 0, tmp_path / "state.json") == []


def test_rotating_batches_eventually_surface_every_group(tmp_path):
    m = _mod()
    groups = [(str(i), [f"entities/{i}/a", f"entities/{i}/b"]) for i in range(5)]
    state = tmp_path / "state.json"
    first = m._select_rotating(groups, 2, state)
    second = m._select_rotating(groups, 2, state)
    third = m._select_rotating(groups, 2, state)
    assert {key for batch in (first, second, third) for key, _members in batch} == {
        "0", "1", "2", "3", "4"}


def test_wakegate_writes_exact_selection_manifest(tmp_path, monkeypatch, capsys):
    m = _mod(); wiki = tmp_path / "wiki"
    _ent(wiki, "g/gpt-5", title="GPT-5"); _ent(wiki, "g/gpt5-dup", title="GPT 5")
    manifest = tmp_path / "selection.json"
    monkeypatch.setattr(m, "ENTITIES", wiki / "entities")
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(m, "MANIFEST", manifest)
    assert m.main() == 0
    selected = __import__("json").loads(manifest.read_text())["selected"]
    assert len(selected) == 1 and selected[0].startswith("dedupe-group:")
    assert selected[0] in capsys.readouterr().out
