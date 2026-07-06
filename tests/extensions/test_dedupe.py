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
    assert m.main() == 0 and '"wakeAgent": true' in capsys.readouterr().out
