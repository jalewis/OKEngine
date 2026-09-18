import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prompt_lint", ROOT / "scripts/prompt_lint.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_all_agent_prompts_have_clean_markdown_sources_and_metrics():
    errors, report = MODULE.lint()
    assert errors == []
    assert len(report["lanes"]) >= 16
    assert report["total_bytes"] > 20_000
    assert all(item["estimated_tokens"] > 0 for item in report["lanes"].values())


def test_lint_detects_conflicting_write_authority(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "templates/pack/skeleton/crons").mkdir(parents=True)
    (tmp_path / "templates/pack/skeleton/prompts").mkdir(parents=True)
    (tmp_path / "prompts/cron").mkdir(parents=True)
    prompt = "Use `file_write` now.\nUse mcp__okengine_write__update_entity.\n"
    (tmp_path / "prompts/cron/bad.md").write_text(prompt)
    (tmp_path / "config/engine-crons.json").write_text(
        '[{"name":"bad","prompt":"Use `file_write` now.\\nUse mcp__okengine_write__update_entity.",'
        '"prompt_file":"prompts/cron/bad.md"}]')
    (tmp_path / "templates/pack/skeleton/crons/engine-template-prompts.json").write_text("{}")
    (tmp_path / "templates/pack/skeleton/crons/engine-template-prompt-files.json").write_text("{}")
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    errors, _ = MODULE.lint()
    assert any("contradictory" in error for error in errors)


def test_lint_reports_missing_obsolete_duplicated_and_unreferenced_prompts(
    tmp_path, monkeypatch
):
    (tmp_path / "config").mkdir()
    cron_prompts = tmp_path / "prompts/cron"
    cron_prompts.mkdir(parents=True)
    template = tmp_path / "templates/pack/skeleton"
    (template / "crons").mkdir(parents=True)
    (template / "prompts").mkdir()
    (cron_prompts / "bad.md").write_text(
        "/opt/vault/wiki/raw is writable\nMCP DISCOVERY CONTRACT:\n")
    (cron_prompts / "orphan.md").write_text("unused")
    (tmp_path / "config/engine-crons.json").write_text(json.dumps([
        {"name": "skip", "no_agent": True},
        {"name": "bad", "prompt_file": "prompts/cron/bad.md",
         "output_contract": {"completion": "per-selected-item"}},
        {"name": "missing", "prompt_file": "prompts/cron/missing.md"},
        {"name": "unbound"},
        {"name": "orphan-agent"},
    ]))
    (template / "crons/engine-template-prompts.json").write_text(json.dumps({
        "unbound": {"prompt_file": "prompts/template.md"},
        "legacy": "inline",
    }))
    (template / "prompts/template.md").write_text("template")
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    errors, report = MODULE.lint()
    expected = ("missing", "obsolete", "duplicated universal", "lacks receipt",
                "lacks a Markdown reference", "unreferenced prompt")
    assert all(any(fragment in error for error in errors) for fragment in expected)
    assert report["lanes"]["bad"]["bytes"] > 0


def test_prompt_lint_cli_writes_artifact_and_returns_failure(tmp_path, monkeypatch, capsys):
    artifact = tmp_path / "report.json"
    monkeypatch.setattr(MODULE, "lint", lambda: (["bad prompt"], {
        "lanes": {}, "total_bytes": 0, "total_estimated_tokens": 0}))
    assert MODULE.main(["--artifact", str(artifact)]) == 1
    assert json.loads(artifact.read_text())["total_bytes"] == 0
    assert "FAIL: bad prompt" in capsys.readouterr().out
    monkeypatch.setattr(MODULE, "lint", lambda: ([], {
        "lanes": {}, "total_bytes": 0, "total_estimated_tokens": 0}))
    assert MODULE.main([]) == 0
