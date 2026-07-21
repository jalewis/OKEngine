import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cron" / "select_unscored_sources.py"
PROMPTS = (Path(__file__).resolve().parents[2] / "templates" / "pack" / "skeleton" /
           "crons" / "engine-template-prompts.json")


def _load():
    spec = importlib.util.spec_from_file_location("select_unscored_sources_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_digest_limits_agent_to_rating_fields(tmp_path, monkeypatch, capsys):
    mod = _load()
    page = tmp_path / "wiki" / "sources" / "2026" / "07" / "report.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: source\ntitle: Report\npublished: 2026-07-13\n"
        "publisher: Example\nurl: https://example.test/report\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    assert mod.main() == 0
    output = capsys.readouterr().out
    assert "Update ONLY `reliability` and `credibility`" in output
    assert "never write `undefined`" in output
    assert "Do not send or change type, id, version" in output
    assert "Do not edit wiki/log.md or any other page" in output


def test_source_quality_prompt_requires_a_two_field_patch():
    prompt = json.loads(PROMPTS.read_text(encoding="utf-8"))["source-quality-backfill"]
    assert "PATCH containing ONLY reliability and credibility" in prompt
    assert "Never resend or alter identity, provenance, publication" in prompt
