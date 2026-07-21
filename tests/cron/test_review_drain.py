import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "select_review_drain.py"


def _load():
    spec = importlib.util.spec_from_file_location("select_review_drain", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _page(vault, rel, fm, body):
    path = vault / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")


def test_candidates_are_substantive_grounded_flagged_and_oldest_first(tmp_path):
    mod = _load()
    _page(tmp_path, "sources/2026/01/a.md", "type: source", "evidence")
    _page(tmp_path, "sources/2026/06/b.md", "type: source", "evidence")
    _page(tmp_path, "sources/x.md", "type: source", "evidence")
    _page(tmp_path, "entities/a/old.md",
          "type: entity\nneeds_review: true\ncreated: 2026-01-01\nsources: [sources/2026/01/a]",
          "grounded body " * 30)
    _page(tmp_path, "entities/b/new.md",
          "type: entity\nneeds_review: true\ncreated: 2026-06-01\nsources: [sources/2026/06/b]",
          "grounded body " * 30)
    _page(tmp_path, "entities/c/thin.md",
          "type: entity\nneeds_review: true\ncreated: 2025-01-01\nsources: [sources/x]",
          "thin")
    _page(tmp_path, "entities/d/ungrounded.md",
          "type: entity\nneeds_review: true\ncreated: 2025-01-01",
          "body " * 100)
    assert [c["path"] for c in mod.candidates(tmp_path)] == [
        "entities/a/old", "entities/b/new"
    ]


def test_stale_source_ref_resolves_only_by_unique_basename(tmp_path):
    mod = _load()
    _page(tmp_path, "sources/2026/07/real.md", "type: source", "evidence")
    assert mod._existing_source_refs(
        tmp_path / "wiki", ["sources/2026-07-01/real"]
    ) == ["sources/2026/07/real"]
    _page(tmp_path, "sources/2026/06/real.md", "type: source", "other")
    assert mod._existing_source_refs(
        tmp_path / "wiki", ["sources/old/real"]
    ) == []


def test_candidates_preserve_complete_evidence_set(tmp_path):
    mod = _load()
    refs = []
    for i in range(7):
        ref = f"sources/2026/07/s{i}"
        refs.append(ref)
        _page(tmp_path, f"{ref}.md", "type: source", "evidence")
    _page(tmp_path, "entities/a/seven.md",
          "type: entity\nneeds_review: true\ncreated: 2026-01-01\nsources: [" +
          ", ".join(refs) + "]", "grounded body " * 30)
    assert mod.candidates(tmp_path)[0]["sources"] == refs


def test_prompt_forbids_machine_generated_human_approval(tmp_path, monkeypatch, capsys):
    mod = _load()
    _page(tmp_path, "sources/2026/07/a.md", "type: source", "evidence")
    _page(tmp_path, "entities/a/page.md",
          "type: entity\nneeds_review: true\ncreated: 2026-01-01\n"
          "sources: [sources/2026/07/a]", "grounded body " * 30)
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    assert mod.main() == 0
    prompt = capsys.readouterr().out
    assert "record_machine_review" in prompt
    assert "never set reviewed_by" in prompt
    assert "never clear needs_review" in prompt
    assert "update_entity to set needs_review=false" not in prompt


def test_no_work_path_emits_valid_json_wake_sentinel(tmp_path, capsys):
    """HIGH #8: the Hermes cron wake-gate parses ONLY the last stdout line as JSON and FAILS OPEN
    (non-JSON → wake the agent). The no-work path must emit the JSON sentinel, not a bare
    'wakeAgent=false' string that json.loads rejects — else the no-work run wakes a WRITE-capable
    agent anyway."""
    import json
    mod = _load()
    mod.VAULT = tmp_path
    mod.WIKI = tmp_path / "wiki"
    (tmp_path / "wiki").mkdir()
    rc = mod.main()
    assert rc == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"wakeAgent": False}, "last line must be parseable no-wake JSON"
