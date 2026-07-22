import importlib.util
from pathlib import Path


MOD = Path(__file__).parents[2] / "scripts" / "cron" / "relationship_propagation.py"
spec = importlib.util.spec_from_file_location("relationship_propagation", MOD)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def test_relationships_propagate_deterministically_from_either_side():
    pages = {
        "sources/qilin-report": {"mentions": ["entities/qilin"]},
        "entities/qilin": {},
        "sources/agenda-report": {},
        "entities/agenda": {"sources": ["sources/agenda-report"]},
    }
    rules = [{"left_field": "mentions", "right_field": "sources"}]
    updates = rp.reconcile(pages, rules)
    assert updates["entities/qilin"]["sources"] == ["sources/qilin-report"]
    assert updates["sources/agenda-report"]["mentions"] == ["entities/agenda"]


def test_raw_and_entity_lane_contracts_are_separated():
    import json
    repo = Path(__file__).parents[2]
    jobs = json.loads((repo / "config" / "engine-crons.json").read_text())
    raw = next(j for j in jobs if j["name"] == "raw-backfill")
    assert raw["output_contract"]["allowed_namespaces"] == ["sources"]
    assert raw["output_contract"]["allowed_types"] == ["source"]
    assert raw["output_contract"]["completion"] == "per-selected-item"
    prompts = json.loads((repo / "templates" / "pack" / "skeleton" / "crons" /
                          "engine-template-prompts.json").read_text())
    raw_prompt = prompts["raw-backfill"]
    assert "SOURCE-ONLY" in raw_prompt["prompt"]
    assert "MUST NOT create or update entities" in raw_prompt["prompt"]
    assert "lane_id, contract_digest, and input_digest" in raw_prompt["prompt"]
    assert "wiki-relative path and sha256" in raw_prompt["prompt"]
    assert raw_prompt["output_contract"] == raw["output_contract"]
    assert "resolving `sources` relationship" in prompts["entity-backfill"]
    assert "Consume only accepted" in prompts["entity-backfill"]


def test_framework_init_renders_staged_contract_and_persona(tmp_path):
    import importlib.util
    import json

    repo = Path(__file__).parents[2]
    module_path = repo / "scripts" / "framework_init.py"
    spec = importlib.util.spec_from_file_location("staged_framework_init", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pack = tmp_path / "okpack-rendered"
    assert module.main([str(pack), "--domain", "Rendered Domain", "--no-compose"]) == 0

    prompts = json.loads((pack / "crons" / "engine-template-prompts.json").read_text())
    raw = prompts["raw-backfill"]
    assert raw["output_contract"]["allowed_namespaces"] == ["sources"]
    assert raw["output_contract"]["completion"] == "per-selected-item"
    assert "lane_id, contract_digest, and input_digest" in raw["prompt"]
    persona = (pack / "CLAUDE.md").read_text()
    assert "Staged ingest workflow (sources, then entities)" in persona
    assert "source lane must not create or update entities" in persona


def test_shell_quickstart_renders_staged_contract(tmp_path):
    import json
    import subprocess

    repo = Path(__file__).parents[2]
    pack = tmp_path / "okpack-shell-rendered"
    result = subprocess.run(
        ["bash", str(repo / "templates" / "pack" / "new-pack.sh"),
         "okpack-shell-rendered", "Shell Rendered Domain", "--out", str(pack)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    prompts = json.loads((pack / "crons" / "engine-template-prompts.json").read_text())
    raw = prompts["raw-backfill"]
    assert raw["output_contract"]["allowed_types"] == ["source"]
    assert raw["output_contract"]["completion"] == "per-selected-item"
    assert "exact lane_id, contract_digest, and input_digest" in raw["prompt"]
