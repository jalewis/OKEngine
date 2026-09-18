import importlib.util
import json
import hashlib
import runpy
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "select_unscored_sources.py"
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
    monkeypatch.setenv("OKENGINE_LANE_ID", "source-quality-backfill")
    monkeypatch.setenv("OKENGINE_CONTRACT_DIGEST", "sha256:contract")
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(tmp_path / "selection.json"))
    assert mod.main() == 0
    output = capsys.readouterr().out
    assert "Update ONLY `reliability` and `credibility`" in output
    assert "never write `undefined`" in output
    assert "Do not send or change type, id, version" in output
    assert "Do not edit wiki/log.md or any other page" in output
    assert "```okengine-receipt" in output
    manifest = json.loads((tmp_path / "selection.json").read_text())
    assert manifest["selected"] == ["wiki/sources/2026/07/report.md"]


def test_source_quality_prompt_requires_the_narrow_scoring_tool():
    spec = json.loads(PROMPTS.read_text(encoding="utf-8"))["source-quality-backfill"]
    prompt = (PROMPTS.parents[1] / spec["prompt_file"]).read_text(encoding="utf-8")
    assert "ONLY mcp__okengine_write_source_quality__score_source" in prompt
    assert "accepts no other fields and NO body" in prompt
    assert "Do not use update_entity, patch_entity, or converge_entity" in prompt
    assert "exact fenced okengine-receipt template" in prompt
    assert "A=completely reliable" in prompt
    assert "6=truth cannot be judged" in prompt
    assert "do not look up another rubric" in prompt
    assert "do not call list_prompts/get_prompt/read_resource" in prompt
    assert "never guess `source-scoring-rubric`" in prompt


def test_tombstoned_sources_are_not_selected():
    mod = _load()
    assert not mod.is_unscored({"type": "source", "status": "tombstoned"})
    assert mod.is_unscored({"type": "source", "status": "live"})


def test_selected_quality_patch_has_a_verifiable_run_receipt(tmp_path, monkeypatch, capsys):
    mod = _load()
    page = tmp_path / "wiki/sources/report.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: source\npublisher: Example\npublished: 2026-07-13\n---\nbody\n"
    )
    manifest_path = tmp_path / "selection.json"
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setenv("OKENGINE_LANE_ID", "source-quality-backfill")
    monkeypatch.setenv("OKENGINE_CONTRACT_DIGEST", "sha256:contract")
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(manifest_path))
    assert mod.main() == 0
    output = capsys.readouterr().out
    template_text = output.split("```okengine-receipt\n", 1)[1].split("\n```", 1)[0]
    receipt = json.loads(template_text)
    key = "wiki/sources/report.md"
    assert receipt["items"][0]["key"] == key

    page.write_text(
        "---\ntype: source\npublisher: Example\npublished: 2026-07-13\n"
        "reliability: B\ncredibility: 3\n---\nbody\n"
    )
    receipt["items"][0].update(disposition="accepted", reason=None)
    receipts_path = REPO / "patches" / "cron-plus" / "run_receipts.py"
    spec = importlib.util.spec_from_file_location("quality_receipts", receipts_path)
    receipts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receipts)
    parsed, result = receipts.verify_response(
        {
            "id": "source-quality-backfill",
            "output_contract_digest": "sha256:contract",
            "selection_manifest": str(manifest_path),
            "receipt_hash_mode": "readback",
        },
        "```okengine-receipt\n" + json.dumps(receipt) + "\n```",
        tmp_path / "wiki",
    )
    observed = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    assert result["valid"] and result["counts"]["undisposed"] == 0
    assert parsed["items"][0]["writes"][0]["sha256"] == observed


def test_no_work_clears_stale_selection_manifest(tmp_path, monkeypatch):
    mod = _load()
    sources = tmp_path / "wiki/sources"
    sources.mkdir(parents=True)
    (sources / "scored.md").write_text(
        "---\ntype: source\nreliability: A\ncredibility: 5\n---\nbody\n"
    )
    manifest = tmp_path / "selection.json"
    manifest.write_text('{"selected":["stale"]}')
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setenv("OKENGINE_LANE_ID", "source-quality-backfill")
    monkeypatch.setenv("OKENGINE_CONTRACT_DIGEST", "sha256:contract")
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(manifest))
    assert mod.main() == 0
    assert not manifest.exists()


def test_frontmatter_and_scoring_edge_cases(tmp_path, monkeypatch):
    mod = _load()
    missing = tmp_path / "missing.md"
    assert mod.read_frontmatter(missing) is None
    plain = tmp_path / "plain.md"
    plain.write_text("plain")
    assert mod.read_frontmatter(plain) is None
    broken = tmp_path / "broken.md"
    broken.write_text("---\na: [broken\n---\n")
    assert mod.read_frontmatter(broken) is None
    scalar = tmp_path / "scalar.md"
    scalar.write_text("---\n- x\n---\n")
    assert mod.read_frontmatter(scalar) is None

    assert not mod.is_unscored({"type": "entity"})
    assert not mod.is_unscored({
        "type": "source", "reliability": "A", "credibility": 5})
    assert mod.is_unscored({
        "type": "source", "reliability": "A", "credibility": ""})
    assert mod.is_unscored({
        "type": "source", "reliability": "", "credibility": 5})


def test_main_requires_receipt_identity_and_sources_directory(
        tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.delenv("OKENGINE_LANE_ID", raising=False)
    monkeypatch.delenv("OKENGINE_CONTRACT_DIGEST", raising=False)
    assert mod.main() == 1
    assert "receipt identity unavailable" in capsys.readouterr().err

    monkeypatch.setenv("OKENGINE_LANE_ID", "lane")
    monkeypatch.setenv("OKENGINE_CONTRACT_DIGEST", "digest")
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(tmp_path / "selection.json"))
    assert mod.main() == 1
    assert "sources dir not found" in capsys.readouterr().err


def test_controlled_target_skip_and_selection_metadata(tmp_path, monkeypatch, capsys):
    mod = _load()
    sources = tmp_path / "wiki/sources"
    sources.mkdir(parents=True)
    (sources / "_ignored.md").write_text("---\ntype: source\n---\n")
    (sources / "not-source.md").write_text("---\ntype: entity\n---\n")
    page = sources / "pending.md"
    page.write_text(
        "---\ntype: source\npublisher: Example\npublished: 2026-07-01\n"
        "source_kind: report\nraw: raw/report.txt\n---\nbody\n")
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "CONTROLLED_TARGET", "wiki/sources/other.md")
    monkeypatch.setenv("OKENGINE_LANE_ID", "lane")
    monkeypatch.setenv("OKENGINE_CONTRACT_DIGEST", "digest")
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(tmp_path / "selection.json"))
    assert mod.main() == 0
    assert "controlled target is not pending" in capsys.readouterr().out

    monkeypatch.setattr(mod, "CONTROLLED_TARGET", "wiki/sources/pending.md")
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "publisher=Example" in out
    assert "kind=report" in out
    assert "raw=raw/report.txt" in out


def test_select_unscored_entrypoint_without_identity(monkeypatch):
    monkeypatch.delenv("OKENGINE_LANE_ID", raising=False)
    monkeypatch.delenv("OKENGINE_CONTRACT_DIGEST", raising=False)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    try:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 1
