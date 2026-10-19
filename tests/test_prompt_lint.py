import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prompt_lint", ROOT / "scripts/prompt_lint.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.integration  # lint() walks the real prompt, selector and extension trees
def test_all_agent_prompts_have_clean_markdown_sources_and_metrics():
    errors, report = MODULE.lint()
    assert errors == []
    reported = "\n".join(report["reported"])
    for lane in ("schema-classify-drain", "broken-wikilinks-drain"):   # okengine#799
        assert f"{lane}: runner verifies a receipt its selector" in reported, reported
    assert len(report["lanes"]) >= 14   # the skeleton's 8 engine-template prompts + extension lanes (#929)
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
    expected = ("missing", "obsolete", "duplicated universal", "UNDETECTABLE",
                "lacks a Markdown reference", "unreferenced prompt")
    assert all(any(fragment in error for error in errors) for fragment in expected)
    assert report["lanes"]["bad"]["bytes"] > 0


def test_prompt_lint_cli_writes_artifact_and_returns_failure(tmp_path, monkeypatch, capsys):
    artifact = tmp_path / "report.json"
    monkeypatch.setattr(MODULE, "lint", lambda: (["bad prompt"], {
        "lanes": {}, "reported": ["known gap"], "total_bytes": 0, "total_estimated_tokens": 0}))
    assert MODULE.main(["--artifact", str(artifact)]) == 1
    assert json.loads(artifact.read_text())["total_bytes"] == 0
    out = capsys.readouterr().out
    assert "FAIL: bad prompt" in out
    assert "KNOWN: known gap" in out
    assert "1 reported, 1 error(s)" in out
    monkeypatch.setattr(MODULE, "lint", lambda: ([], {
        "lanes": {}, "total_bytes": 0, "total_estimated_tokens": 0}))
    assert MODULE.main([]) == 0


# --- okengine#916: the receipt requirement is derived from the lane, not the template file ---

RECEIPT_SELECTOR = 'print("```okengine-receipt")\n'
HELPER_SELECTOR = "print_receipt_template(manifest)\n"
SILENT_SELECTOR = 'print("digest only")\n'
PER_ITEM = {"api": 1, "allowed_namespaces": ["concepts"], "allowed_types": ["concept"],
            "operations": ["converge"], "required_fields": ["type"],
            "required_relationships": [], "unknown_fields": "reject",
            "unresolved_links": "review", "placeholder_links": "reject",
            "completion": "per-selected-item"}
RUN = {**PER_ITEM, "completion": "run"}
RECEIPTLESS = "{{DOMAIN}} concept backfill. Write only through converge_concept.\n"
WITH_RECEIPT = ("{{DOMAIN}} concept backfill. End with only the exact fenced okengine-receipt "
                "copied from the selector.\n")


def _repo(tmp_path, monkeypatch, jobs, templates, prompts, selectors=None):
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts/cron").mkdir(parents=True)
    (tmp_path / "scripts/cron").mkdir(parents=True)
    skeleton = tmp_path / "templates/pack/skeleton"
    (skeleton / "crons").mkdir(parents=True)
    (skeleton / "prompts").mkdir()
    (tmp_path / "config/engine-crons.json").write_text(json.dumps(jobs))
    (skeleton / "crons/engine-template-prompts.json").write_text(json.dumps(templates))
    for relative, text in prompts.items():
        (tmp_path / relative).write_text(text)
    for name, source in (selectors or {}).items():
        (tmp_path / "scripts/cron" / name).write_text(source)
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    return MODULE.lint()


def _template_lane(tmp_path, monkeypatch, prompt, *, contract=PER_ITEM,
                   selector=RECEIPT_SELECTOR, policy=None):
    template = {"prompt_file": "prompts/concept-backfill.md"}
    if policy is not None:
        template["output_contract"] = policy
    return _repo(tmp_path, monkeypatch,
                 [{"name": "concept-backfill", "script": "/opt/data/scripts/select_c.py",
                   "output_contract": contract}],
                 {"concept-backfill": template},
                 {"templates/pack/skeleton/prompts/concept-backfill.md": prompt},
                 {"select_c.py": selector})


def test_receiptless_template_prompt_for_a_receipt_lane_fails(tmp_path, monkeypatch):
    errors, report = _template_lane(tmp_path, monkeypatch, RECEIPTLESS)
    assert errors == [
        "concept-backfill: per-item judgment prompt lacks receipt requirement — selector "
        "select_c.py prints an okengine-receipt the runner verifies, but the prompt never "
        "tells the model to return it"], errors
    assert report["reported"] == []


def test_template_prompt_that_returns_the_receipt_passes(tmp_path, monkeypatch):
    errors, report = _template_lane(tmp_path, monkeypatch, WITH_RECEIPT)
    assert errors == []
    assert report["reported"] == []
    assert report["schema_version"] == 1
    assert report["lanes"]["concept-backfill"]["bytes"] == len(WITH_RECEIPT.rstrip())


def test_receipt_helper_call_in_the_selector_also_creates_the_obligation(tmp_path, monkeypatch):
    errors, _ = _template_lane(tmp_path, monkeypatch, RECEIPTLESS, selector=HELPER_SELECTOR)
    assert len(errors) == 1 and "lacks receipt requirement" in errors[0], errors


def test_run_completion_lane_owes_no_receipt(tmp_path, monkeypatch):
    errors, report = _template_lane(tmp_path, monkeypatch, RECEIPTLESS, contract=RUN)
    assert errors == []
    assert report["reported"] == []


def test_pack_policy_tightening_to_per_item_creates_the_obligation(tmp_path, monkeypatch):
    errors, _ = _template_lane(tmp_path, monkeypatch, RECEIPTLESS, contract=RUN,
                               policy=PER_ITEM)
    assert len(errors) == 1 and "lacks receipt requirement" in errors[0], errors


def test_template_policy_that_cannot_compose_is_an_error(tmp_path, monkeypatch):
    errors, _ = _template_lane(tmp_path, monkeypatch, RECEIPTLESS, policy=RUN)
    assert errors == [e for e in errors if e.startswith("concept-backfill: template "
                                                        "output_contract does not compose")]
    assert len(errors) == 1, errors


def test_per_item_lane_whose_selector_prints_no_receipt_is_reported_not_failed(
    tmp_path, monkeypatch
):
    errors, report = _template_lane(tmp_path, monkeypatch, RECEIPTLESS,
                                    selector=SILENT_SELECTOR)
    assert errors == []
    assert report["reported"] == [
        "concept-backfill: runner verifies a receipt its selector select_c.py never prints "
        "(engine-side, okengine#799)"]


def test_unreadable_selector_is_undetectable_not_a_pass(tmp_path, monkeypatch):
    errors, report = _repo(
        tmp_path, monkeypatch,
        [{"name": "concept-backfill", "script": "select_gone.py",
          "output_contract": PER_ITEM}],
        {"concept-backfill": {"prompt_file": "prompts/concept-backfill.md"}},
        {"templates/pack/skeleton/prompts/concept-backfill.md": WITH_RECEIPT})
    assert errors == [
        "concept-backfill: per-item lane but its selector select_gone.py is unreadable — "
        "receipt requirement UNDETECTABLE, not a pass"], errors
    assert report["reported"] == []


def test_per_item_lane_without_any_selector_is_undetectable(tmp_path, monkeypatch):
    errors, _ = _repo(
        tmp_path, monkeypatch,
        [{"name": "drain", "prompt_file": "prompts/cron/drain.md", "output_contract": PER_ITEM}],
        {}, {"prompts/cron/drain.md": WITH_RECEIPT})
    assert errors == ["drain: per-item lane but its selector (none declared) is unreadable — "
                      "receipt requirement UNDETECTABLE, not a pass"], errors


def test_template_prompt_bound_to_no_model_lane_is_reported(tmp_path, monkeypatch):
    errors, report = _repo(
        tmp_path, monkeypatch,
        [{"name": "staleness", "script": "select_s.py", "no_agent": True}],
        {"staleness": {"prompt_file": "prompts/staleness.md"},
         "orphan-template": {"prompt_file": "prompts/orphan.md"}},
        {"templates/pack/skeleton/prompts/staleness.md": RECEIPTLESS,
         "templates/pack/skeleton/prompts/orphan.md": RECEIPTLESS})
    assert errors == []
    assert report["reported"] == [
        f"{name}: template prompt binds no engine model lane — its receipt requirement is "
        "UNDETECTABLE" for name in ("staleness", "orphan-template")]


def test_selector_prints_receipt_reads_the_source(tmp_path):
    receipt, helper, silent = (tmp_path / n for n in ("a.py", "b.py", "c.py"))
    receipt.write_text(RECEIPT_SELECTOR)
    helper.write_text(HELPER_SELECTOR)
    silent.write_text(SILENT_SELECTOR)
    assert MODULE.selector_prints_receipt(receipt) is True
    assert MODULE.selector_prints_receipt(helper) is True
    assert MODULE.selector_prints_receipt(silent) is False
    assert MODULE.selector_prints_receipt(tmp_path / "missing.py") is None
    assert MODULE.selector_prints_receipt(tmp_path) is None
    assert MODULE.selector_prints_receipt(None) is None


def test_receipt_verdict_ignores_non_dict_and_run_contracts(tmp_path):
    selector = tmp_path / "s.py"
    selector.write_text(RECEIPT_SELECTOR)
    assert MODULE.receipt_verdict("x", None, selector, "") == ([], [])
    assert MODULE.receipt_verdict("x", "per-selected-item", selector, "") == ([], [])
    assert MODULE.receipt_verdict("x", RUN, selector, "") == ([], [])
    assert MODULE.receipt_verdict("x", {"api": 1}, selector, "") == ([], [])  # no completion
    assert MODULE.receipt_verdict("x", PER_ITEM, selector, "Return the OKENGINE-RECEIPT.") \
        == ([], [])


def test_engine_selector_resolves_the_staged_basename(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    assert MODULE.engine_selector({"script": "/opt/data/scripts/x.py"}) \
        == tmp_path / "scripts/cron/x.py"
    assert MODULE.engine_selector({"script": ""}) is None
    assert MODULE.engine_selector({"script": 7}) is None
    assert MODULE.engine_selector({}) is None


def _extension(root, ext_id, operations=None, dropins=None, files=None):
    ext = root / "extensions" / ext_id
    ext.mkdir(parents=True)
    manifest = {"id": ext_id, "kind": "operation", "version": "0.1.0",
                "requires": {"engine": ">=0.1.0"}, "trust": "in-gateway",
                "capabilities": {"read": ["wiki"], "write": []}}
    if operations is not None:
        manifest.update(operations)
    import yaml
    (ext / "extension.yaml").write_text(yaml.safe_dump(manifest))
    for name, block in (dropins or {}).items():
        (ext / "crons").mkdir(exist_ok=True)
        (ext / "crons" / f"{name}.cron.json").write_text(json.dumps(block))
    for relative, text in (files or {}).items():
        (ext / relative).parent.mkdir(parents=True, exist_ok=True)
        (ext / relative).write_text(text)
    return ext


def _op(prompt_file=None, entrypoint="select.py", contract=PER_ITEM, prompt=None):
    op = {"schedule": {"kind": "cron", "expr": "0 1 * * *"}, "output_contract": contract,
          "max_iterations": 4}
    if entrypoint is not None:
        op["entrypoint"] = entrypoint
    if prompt_file is not None:
        op["prompt_file"] = prompt_file
    if prompt is not None:
        op["prompt"] = prompt
    return op


def test_extension_receipt_lanes_are_linted_from_their_operation_contract(tmp_path, monkeypatch):
    _extension(tmp_path, "okengine.demo",
               {"operations": {
                   "bare": _op("prompts/bare.md"),
                   "fixed": _op("prompts/fixed.md", entrypoint={"script": "select.py"}),
                   "summary": _op("prompts/bare.md", contract=RUN),
                   "inline": _op(prompt="Inline prompt with no fence."),
                   "silent": _op("prompts/bare.md", entrypoint="quiet.py"),
                   "deterministic": {"schedule": {"kind": "cron", "expr": "0 1 * * *"},
                                     "entrypoint": "select.py"}}},
               files={"select.py": RECEIPT_SELECTOR, "quiet.py": SILENT_SELECTOR,
                      "prompts/bare.md": RECEIPTLESS, "prompts/fixed.md": WITH_RECEIPT})
    _extension(tmp_path, "okengine.single", {"operation": _op("prompts/p.md")},
               files={"select.py": HELPER_SELECTOR, "prompts/p.md": RECEIPTLESS})
    _extension(tmp_path, "okengine.dropin", dropins={"sweep": _op("prompts/gone.md")},
               files={"select.py": RECEIPT_SELECTOR})
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    errors, reported = MODULE.lint_extensions()
    lacks = ("per-item judgment prompt lacks receipt requirement — selector select.py prints an "
             "okengine-receipt the runner verifies, but the prompt never tells the model to "
             "return it")
    assert sorted(errors) == sorted([
        f"okengine.demo:bare: {lacks}", f"okengine.demo:inline: {lacks}",
        f"okengine.single: {lacks}",
        f"okengine.dropin:sweep: prompt file is missing: "
        f"{tmp_path / 'extensions/okengine.dropin/prompts/gone.md'}"]), errors
    assert reported == ["okengine.demo:silent: runner verifies a receipt its selector quiet.py "
                        "never prints (engine-side, okengine#799)"]


def test_extension_discovery_and_operation_faults_are_errors(tmp_path, monkeypatch):
    (tmp_path / "extensions/okengine.broken").mkdir(parents=True)
    (tmp_path / "extensions/okengine.broken/extension.yaml").write_text("- not a mapping\n")
    _extension(tmp_path, "okengine.empty")
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    errors, reported = MODULE.lint_extensions()
    assert reported == []
    assert any("okengine.broken" in e and "mapping" in e for e in errors), errors
    _extension(tmp_path, "okengine.odd", {"operations": {"bad": _op("p.md", entrypoint=7)}},
               files={"p.md": WITH_RECEIPT})
    errors, _ = MODULE.lint_extensions()
    assert ("okengine.odd:bad: per-item lane but its selector (none declared) is unreadable — "
            "receipt requirement UNDETECTABLE, not a pass") in errors, errors
    assert any(e.startswith("okengine.empty: kind=operation requires") for e in errors), errors


def test_real_skeleton_concept_backfill_owes_and_returns_its_receipt():
    """The #916 gap itself: the skeleton lane's contract + selector demand the receipt."""
    jobs = {j["name"]: j for j in json.loads((ROOT / "config/engine-crons.json").read_text())}
    lane = jobs["concept-backfill"]
    assert lane["output_contract"]["completion"] == "per-selected-item"
    assert MODULE.selector_prints_receipt(ROOT / "scripts/cron" / lane["script"]) is True
    prompt = (ROOT / "templates/pack/skeleton/prompts/concept-backfill.md").read_text()
    assert "okengine-receipt" in prompt
