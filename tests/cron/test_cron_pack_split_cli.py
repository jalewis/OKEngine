"""CLI workflow coverage for cron pack generation (#471)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location(
        "cron_pack_split_cli", REPO / "scripts/cron_pack_split.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def jobs():
    return [
        {"id": "eng-id", "name": "engine", "schedule": {"expr": "0 * * * *"},
         "max_iterations": 8},
        {"id": "dom-id", "name": "domain", "pack": "pack",
         "schedule": {"expr": "5 * * * *"}, "max_iterations": 8},
    ]


def configure(module, tmp_path):
    module.JOBS = tmp_path / "cron-plus-jobs.json"
    module.JOBS.write_text(json.dumps({"jobs": jobs()}))
    module.TIERS = tmp_path / "cron-tiers.yaml"
    module.TIERS.write_text("engine: [engine]\nengine-template: []\ndomain: []\n")
    return module.JOBS


def test_cli_split_merge_and_check(tmp_path, capsys):
    module = load()
    configure(module, tmp_path)
    out = tmp_path / "parts"
    assert module.main(["split", "--out", str(out)]) == 0
    assert (out / module.ENGINE_CRONS).is_file()
    assert "split 2 jobs" in capsys.readouterr().out

    merged = tmp_path / "merged.json"
    assert module.main(["merge", "--in", str(out), "--out", str(merged)]) == 0
    assert len(json.loads(merged.read_text())["jobs"]) == 2
    assert module.main(["check"]) == 0
    assert "lossless" in capsys.readouterr().out


def test_cli_merge_to_stdout_and_dump(tmp_path, monkeypatch, capsys):
    module = load()
    configure(module, tmp_path)
    parts = module.split(jobs(), module._tier_map(module.TIERS))
    source = tmp_path / "parts"
    module._write_split(parts, source)
    assert module.main(["merge", "--in", str(source)]) == 0
    assert '"jobs"' in capsys.readouterr().out

    called = []
    monkeypatch.setattr(module, "dump_from_live", lambda live: called.append(live))
    assert module.main(["dump", "--live", "live.json"]) == 0
    assert called == ["live.json"]


def test_cli_regen_and_compose_success_and_failure(tmp_path, monkeypatch, capsys):
    module = load()
    configure(module, tmp_path)
    called = []
    monkeypatch.setattr(module, "regen", lambda: called.append("regen") or [])
    assert module.main(["regen"]) == 0 and called == ["regen"]

    monkeypatch.setattr(module, "compose", lambda _path: (jobs(), []))
    monkeypatch.setattr(module, "validate_ordering", lambda _jobs: (_jobs, []))
    monkeypatch.setattr(module, "validate_unique_ids", lambda _jobs: [])
    assert module.main(["compose", "--packs", str(tmp_path)]) == 0
    assert "composed 2 jobs" in capsys.readouterr().out

    monkeypatch.setattr(module, "compose", lambda _path: (jobs(), ["collision"]))
    assert module.main(["compose", "--packs", str(tmp_path)]) == 1
    assert "collision" in capsys.readouterr().out


def test_cli_check_ordering_and_mismatch_failures(tmp_path, monkeypatch, capsys):
    module = load()
    configure(module, tmp_path)
    monkeypatch.setattr(module, "validate_ordering", lambda values: (values, ["cycle"]))
    assert module.main(["check"]) == 1
    assert "ORDERING" in capsys.readouterr().err

    monkeypatch.setattr(module, "validate_ordering", lambda values: (values, []))
    original_merge = module.merge
    monkeypatch.setattr(module, "merge", lambda *_a, **_k: [])
    assert module.main(["check"]) == 1
    assert "MISMATCH" in capsys.readouterr().err
    monkeypatch.setattr(module, "merge", original_merge)


def test_split_prompt_and_merge_pack_error_paths():
    module = load()
    tiers = {"eng": "engine", "tmpl": "engine-template", "domain": "domain"}
    parts = module.split([
        {"name": "eng"},
        {"name": "tmpl", "prompt": "do it"},
        {"name": "domain"},
    ], tiers)
    assert parts[module.DOMAIN_PROMPTS] == {"tmpl": "do it"}
    assert "prompt" not in parts[module.ENGINE_CRONS][1]
    for value in ([], {"prompt": 1}, {"prompt": "x", "unknown": True}):
        try:
            module._prompt_parts(value, "tmpl")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid prompt shape must fail")

    packs = [{"name": "pack", "prompts": {"unknown": "x"}, "domain": [
        {"name": "same"}, {"name": "same"}]}]
    _, errors = module.merge_packs([{"name": "tmpl"}], packs, {"tmpl": "engine-template"})
    assert any("not an engine-template" in e for e in errors)
    assert any("job-id collision" in e for e in errors)


def test_discover_compose_and_regen_composed_gates(tmp_path, monkeypatch):
    module = load()
    packs = tmp_path / "packs"
    good = packs / "good"
    (good / "crons").mkdir(parents=True)
    (good / "pack.yaml").write_text("name: good\n")
    (good / "crons" / module.DOMAIN_CRONS).write_text('[{"name":"job"}]')
    (good / "crons" / module.DOMAIN_PROMPTS).write_text('{"tmpl":"prompt"}')
    (packs / "plain").mkdir()
    meta = SimpleMeta({"good": {"name": "good"}, "plain": None})
    monkeypatch.setattr(module, "_pack_meta", lambda: meta)
    found = module.discover_packs(packs)
    assert found[0]["name"] == "good" and found[0]["domain"][0]["name"] == "job"

    monkeypatch.setattr(module, "compose", lambda _p: ([], ["bad composition"]))
    try:
        module.regen_composed(packs)
    except SystemExit as exc:
        assert "composition errors" in str(exc)
    else:
        raise AssertionError("composition errors must block writes")

    monkeypatch.setattr(module, "compose", lambda _p: ([{"id": "x", "name": "x"}], []))
    monkeypatch.setattr(module, "validate_ordering", lambda _j: (_j, ["cycle"]))
    try:
        module.regen_composed(packs)
    except SystemExit as exc:
        assert "ordering errors" in str(exc)
    else:
        raise AssertionError("ordering errors must block writes")

    monkeypatch.setattr(module, "validate_ordering", lambda _j: (_j, []))
    monkeypatch.setattr(module, "validate_unique_ids", lambda _j: ["duplicate"])
    try:
        module.regen_composed(packs)
    except SystemExit as exc:
        assert "collisions" in str(exc)
    else:
        raise AssertionError("id errors must block writes")

    monkeypatch.setattr(module, "validate_unique_ids", lambda _j: [])
    monkeypatch.setattr(module, "validate_output_contracts", lambda _j: ["bad contract"])
    try:
        module.regen_composed(packs)
    except SystemExit as exc:
        assert "output-contract errors" in str(exc)
    else:
        raise AssertionError("contract errors must block writes")

    monkeypatch.setattr(module, "validate_output_contracts", lambda _j: [])
    monkeypatch.setattr(module, "validate_agent_bounds", lambda _j: ["bad bound"])
    with pytest.raises(SystemExit) as raised:
        module.regen_composed(packs)
    assert str(raised.value) == "cron agent-bound errors (not deploying):\n  bad bound"


def test_regen_fail_loud_gates(tmp_path, monkeypatch):
    module = load()
    configure(module, tmp_path)
    module.ENGINE_CRONS_FILE = tmp_path / "engine.json"
    module.ENGINE_CRONS_FILE.write_text('[{"name":"engine","no_agent":true}]')
    module.PACK_DIR = tmp_path / "pack"
    module.PACK_DIR.mkdir()
    monkeypatch.setattr(module, "_extension_pass", lambda *_: ([], ["extension bad"]))
    try:
        module.regen()
    except SystemExit as exc:
        assert "extension composition errors" in str(exc)
    else:
        raise AssertionError("extension errors must block regen")

    monkeypatch.setattr(module, "_extension_pass", lambda *_: ([], []))
    monkeypatch.setattr(module, "validate_ordering", lambda values: (values, ["cycle"]))
    try:
        module.regen()
    except SystemExit as exc:
        assert "ordering errors" in str(exc)
    else:
        raise AssertionError("ordering errors must block regen")

    monkeypatch.setattr(module, "validate_ordering", lambda values: (values, []))
    monkeypatch.setattr(module, "validate_unique_ids", lambda _values: ["duplicate"])
    try:
        module.regen()
    except SystemExit as exc:
        assert "collisions" in str(exc)
    else:
        raise AssertionError("collisions must block regen")

    monkeypatch.setattr(module, "validate_unique_ids", lambda _values: [])
    monkeypatch.setattr(module, "validate_agent_bounds", lambda _j: ["bad bound"])
    with pytest.raises(SystemExit) as raised:
        module.regen()
    assert str(raised.value) == "cron agent-bound errors (not deploying):\n  bad bound"


class SimpleMeta:
    def __init__(self, values):
        self.values = values

    def load_pack_meta(self, path):
        return self.values.get(Path(path).name)

    def validate_composition(self, _metas):
        return []


def test_remaining_split_pack_discovery_regen_and_cli_edges(tmp_path, monkeypatch, capsys):
    module = load()
    # Template without a prompt takes the loop-through branch; invalid prompt policy
    # and repeated template instances surface composition errors.
    parts = module.split([
        {"name": "tmpl"}, {"name": "engine"},
    ], {"tmpl": "engine-template", "engine": "engine"})
    assert parts[module.DOMAIN_PROMPTS] == {}
    packs = [
        {"name": "a", "prompts": {"tmpl": {"prompt": 1}}, "domain": []},
        {"name": "a", "prompts": {"tmpl": "valid"}, "domain": []},
        {"name": "a", "prompts": {"tmpl": "again"}, "domain": []},
    ]
    _, errors = module.merge_packs(
        [{"name": "tmpl"}], packs, {"tmpl": "engine-template"},
    )
    assert any("prompt" in error for error in errors)
    assert any("job-id collision" in error for error in errors)

    monkeypatch.setattr(
        module, "_pack_meta", lambda: (_ for _ in ()).throw(RuntimeError("metadata")),
    )
    pack = tmp_path / "okpack-fallback"
    pack.mkdir()
    assert module._pack_name(pack) == "okpack-fallback"

    monkeypatch.setattr(module, "_pack_meta", lambda: SimpleMeta({}))
    assert module.discover_packs(tmp_path / "absent") == []
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    (packs_dir / "file").write_text("not a directory")
    assert module.discover_packs(packs_dir) == []

    module.JOBS = tmp_path / "jobs.json"
    monkeypatch.setattr(module, "compose", lambda _p: ([{"name": "ok", "no_agent": True}], []))
    monkeypatch.setattr(module, "validate_ordering", lambda values: (values, []))
    monkeypatch.setattr(module, "validate_unique_ids", lambda _values: [])
    monkeypatch.setattr(module, "validate_output_contracts", lambda _values: [])
    assert module.regen_composed(packs_dir) == [{"name": "ok", "no_agent": True}]
    assert module.JOBS.is_file()

    extension_script = Path(module.__file__).resolve().parent / "extension_compose.py"
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path, "is_file",
        lambda self: False if self == extension_script else original_is_file(self),
    )
    assert module._extension_pass(pack, []) == ([], [])
    monkeypatch.setattr(Path, "is_file", original_is_file)

    # Successful regen marks every domain job with pack provenance.
    configure(module, tmp_path)
    module.ENGINE_CRONS_FILE = tmp_path / "engine.json"
    module.ENGINE_CRONS_FILE.write_text('[{"name":"engine","no_agent":true}]')
    module.PACK_DIR = pack
    (pack / "crons").mkdir()
    (pack / "crons" / module.DOMAIN_CRONS).write_text(
        '[{"name":"one","no_agent":true},{"name":"two","pack":"existing","no_agent":true}]'
    )
    monkeypatch.setattr(module, "_pack_name", lambda _p: "fallback")
    monkeypatch.setattr(module, "merge", lambda engine, domain, prompts, tier_of: engine + domain)
    monkeypatch.setattr(module, "_extension_pass", lambda *_a: ([], []))
    monkeypatch.setattr(module, "bind_contract_writers", lambda values: values)
    monkeypatch.setattr(module, "validate_ordering", lambda values: (values, []))
    monkeypatch.setattr(module, "validate_unique_ids", lambda _values: [])
    monkeypatch.setattr(module, "validate_output_contracts", lambda _values: [])
    result = module.regen()
    assert [job["pack"] for job in result if job["name"] in {"one", "two"}] == [
        "fallback", "existing",
    ]

    with pytest.raises(SystemExit):
        module.main(["compose"])
    with pytest.raises(SystemExit):
        module.main(["dump"])

    # Mismatch diagnostics include jobs introduced only by the merged side.
    configure(module, tmp_path)
    monkeypatch.setattr(module, "merge", lambda *_a, **_k: jobs() + [{"name": "extra"}])
    assert module.main(["check"]) == 1
    assert "only in merged" in capsys.readouterr().err


def test_remaining_contract_prompt_and_dump_branches(tmp_path, monkeypatch, capsys):
    module = load()

    # A non-per-item contract is still digested and lane-bound, but must not
    # acquire the selector receipt environment used by per-selected-item jobs.
    contract = {"completion": "terminal"}
    stamped = module._stamp_output_contract({
        "id": "lane-id", "name": "lane", "output_contract": contract,
    })
    assert stamped["env"]["OKENGINE_LANE_ID"] == "lane-id"
    assert "OKENGINE_SELECTION_MANIFEST" not in stamped["env"]

    discovery = module._MCP_DISCOVERY_CONTRACT
    rubric_only = module._stamp_mcp_discovery_contract({
        "name": "source-quality-backfill",
        "prompt": "Legacy instructions." + discovery,
    })
    assert "SOURCE QUALITY RUBRIC:" in rubric_only["prompt"]
    complete_prompt = "A=completely reliable" + discovery
    unchanged = {"name": "source-quality-backfill", "prompt": complete_prompt}
    assert module._stamp_mcp_discovery_contract(unchanged) is unchanged

    module.TIERS = tmp_path / "tiers.yaml"
    module.TIERS.write_text("engine: [engine]\nengine-template: []\ndomain: []\n")
    module.ENGINE_CRONS_FILE = tmp_path / "engine-crons.json"
    module.PACK_DIR = tmp_path / "pack"
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"jobs": [{"name": "engine", "runtime": True}]}))
    monkeypatch.setattr(module, "sanitize", lambda values: [
        {key: value for key, value in item.items() if key != "runtime"} for item in values
    ])
    monkeypatch.setattr(module, "_restore_source_reprs", lambda values: values)
    regenerated = []
    monkeypatch.setattr(module, "regen", lambda: regenerated.append(True))
    module.dump_from_live(str(live))
    assert json.loads(module.ENGINE_CRONS_FILE.read_text()) == [{"name": "engine"}]
    assert json.loads((module.PACK_DIR / "crons" / module.DOMAIN_CRONS).read_text()) == []
    assert json.loads((module.PACK_DIR / "crons" / module.DOMAIN_PROMPTS).read_text()) == {}
    assert regenerated == [True]
    assert "dump: live" in capsys.readouterr().out


def test_restore_deploy_transforms_import_failure_returns_verbatim(monkeypatch):
    module = load()
    values = [{"name": "x", "schedule": {"expr": "0 * * * *"}}]
    original_import = __import__
    def importing(name, *args, **kwargs):
        if name == "cron_jitter":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", importing)
    assert module._restore_source_reprs(values) is values
