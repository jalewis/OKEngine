"""The first-party okengine.predictions extension composes into 3 wake-gated agent jobs.

This is the proving case for the multi-op + agent-op + bundled-prompt work — the engine's
canonical example extension, migrated out of the cron fleet (extensions/okengine.predictions).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.predictions"
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"

pytestmark = pytest.mark.skipif(not EXT.is_dir() or not COMPOSE.is_file(),
                                reason="okengine.predictions extension or compose module absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _manifest():
    import yaml
    return yaml.safe_load((EXT / "extension.yaml").read_text(encoding="utf-8"))


@pytest.mark.contract
def test_manifest_is_valid_and_first_party():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.predictions"
    assert mod.is_reserved_id(m["id"])                 # first-party okengine.* namespace
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


@pytest.mark.contract
def test_composes_into_agent_and_no_agent_jobs():
    c = _load("extension_compose", COMPOSE)
    rec = {"id": "okengine.predictions", "tier": "engine", "dir": str(EXT), "manifest": _manifest()}
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors, errors
    by_name = {j["name"]: j for j in jobs}
    agent = ["okengine.predictions:candidate-watch", "okengine.predictions:grade",
             "okengine.predictions:regrade", "okengine.predictions:base-rates",
             "okengine.predictions:prediction-falsification-search",
             "okengine.predictions:output-outcome-eval",
             "okengine.predictions:prediction-structural-backfill",
             "okengine.predictions:prediction-schema-drain",
             "okengine.predictions:forecast-review"]
    no_agent = ["okengine.predictions:calibration-refresh", "okengine.predictions:prediction-date-audit",
                "okengine.predictions:prediction-schema-audit",
                "okengine.predictions:confidence-recommender"]
    assert sorted(by_name) == sorted(agent + no_agent)
    for n in agent:
        j = by_name[n]
        assert j["no_agent"] is False
        assert isinstance(j["prompt"], str) and j["prompt"].strip()   # bundled prompt loaded
        assert "select_" in j["script"]
        assert "okengine-write" in j["enabled_toolsets"]
    for n in no_agent:                                  # forecasting-discipline measurement lanes (#159)
        j = by_name[n]
        assert j["no_agent"] is True                    # script-only, no prompt
        assert not (j.get("prompt") or "").strip()
        assert j["script"].endswith(".py")


@pytest.mark.contract
def test_bundled_prompt_files_exist_and_nonempty():
    for op in ("candidate-watch", "grade", "regrade", "base-rates",
               "falsification-search", "output-outcome-eval", "structural-backfill", "schema-drain"):
        f = EXT / "prompts" / f"{op}.md"
        assert f.is_file() and f.read_text(encoding="utf-8").strip(), op


@pytest.mark.contract
def test_schema_drain_transcribes_deterministic_horizon_from_digest():
    prompt = (EXT / "prompts" / "schema-drain.md").read_text(encoding="utf-8")
    assert "transcribe" in prompt
    assert "authoritative" in prompt
    assert "Do not independently reclassify" in prompt


@pytest.mark.contract
def test_write_capabilities():
    m = _manifest()
    # predictions (the book) + dashboards (derived base-rates / outcome-eval) + briefings (the
    # forecast-review narrative its selector targets — see test_selector_target_in_contract).
    assert m["capabilities"]["write"] == ["predictions/**", "dashboards/**", "briefings/**"]
    # Only forecast-review may use briefings/: every other op keeps the predictions-only contract.
    for name, op in m["operations"].items():
        namespaces = (op.get("output_contract") or {}).get("allowed_namespaces") or []
        assert ("briefings" in namespaces) == (name == "forecast-review"), name
    # #217: the extension SHIPS a fragment — but only the evidence ITEM contract. The
    # prediction TYPE stays pack-owned: the fragment must never grow owns/extends (that
    # was this assertion's original point, now stated precisely instead of as "no schema").
    assert m.get("schema") == ["schema/predictions.schema.yaml"]
    import yaml as _y
    frag = _y.safe_load((EXT / "schema" / "predictions.schema.yaml").read_text(encoding="utf-8"))
    # still ONLY field contracts — the fragment must never grow owns/extends (the prediction TYPE
    # stays pack-owned). `field_enums` gave way to `field_shapes` in okengine#563.
    assert set(frag) == {"enums", "field_items", "field_shapes"}, (
        f"fragment grew beyond field contracts: {set(frag)}")
    # okengine#563: `confidence` on a prediction is the PROBABILITY this extension's calibration
    # lane feeds to a Brier score, so it is bound by SHAPE, not by a band vocabulary. The
    # prediction_confidence enum stays defined for a pack that carries a separate band field.
    assert "field_enums" not in frag, (
        "binding `confidence` to a band enum contradicts calibration_refresh's Brier score and 91% "
        "of the corpus; declare a numeric shape instead")
    assert frag["field_shapes"]["confidence"]["by_type"]["prediction"] == "number"
    assert frag["enums"]["prediction_confidence"] == [
        "very-low", "low", "medium-low", "medium", "medium-high", "high", "very-high",
    ]


def test_confidence_is_declared_numeric_for_predictions():
    """The contract moved from vocabulary to SHAPE. calibration_refresh computes
    `sum((confidence - outcome) ** 2) / n`; a band string cannot participate in that, and the
    corpus is 79-91% numeric on the two live vaults measured."""
    import yaml as _y
    frag = _y.safe_load((EXT / "schema" / "predictions.schema.yaml").read_text(encoding="utf-8"))
    assert frag["field_shapes"]["confidence"]["by_type"] == {"prediction": "number"}
    validator = _load("predictions_schema_validator", REPO / "tools" / "schema_validator.py")
    # and no enum governs it any more, on prediction or anywhere else
    assert validator._enum_reject_reason(frag, "prediction", {"confidence": 0.65}) is None
    assert validator._enum_reject_reason(frag, "assessment", {"confidence": 0.65}) is None


def test_structural_backfill_selection_is_bound_to_prewrite_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(EXT))
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    selector = _load(
        "prediction_structural_selector",
        EXT / "select_prediction_structural_backfill.py",
    )
    page = tmp_path / "wiki" / "predictions" / "one.md"
    page.parent.mkdir(parents=True)
    page.write_text("# One\n")

    key = selector._selection_key(page, tmp_path)

    assert key.startswith("wiki/predictions/one.md|sha256:")
    assert len(key.rsplit(":", 1)[1]) == 64


# --- okengine#878: batch-triage lanes report PER-ITEM dispositions -------------------------------
# candidate-watch, grade, regrade, prediction-schema-drain and prediction-falsification-search
# triage a batch where "defer" is routinely the correct answer. Under `completion: run` the runner
# failed every such run for writing nothing. They now declare `completion: per-selected-item`,
# which only works if each selector writes the selection manifest on EVERY path — a fresh EMPTY
# manifest is how the runner knows a wakeAgent=false run owes no receipt.

import json as _json
import re as _re

_TRIAGE_SELECTORS = {
    "candidate-watch": "select_prediction_candidates.py",
    "grade": "select_predictions_for_grading.py",
    "regrade": "select_regrade_batch.py",
    "prediction-schema-drain": "select_prediction_schema_drain.py",
    "prediction-falsification-search": "select_falsification.py",
}
_RECEIPT = _re.compile(r"```okengine-receipt\n(\{.*?\})\n```", _re.S)


def _triage_vault(root: Path) -> None:
    """One vault that gives every triage selector work: an overdue, high-confidence open
    prediction missing required fields, a recent source, and an entity that source cites."""
    wiki = root / "wiki"
    (wiki / "predictions").mkdir(parents=True)
    (wiki / "predictions" / "p1.md").write_text(
        "---\ntype: prediction\nstatus: open\nconfidence: 0.8\nresolves_by: 2026-09-01\n---\n"
        "# P1\n\nA dated claim.\n")
    (wiki / "sources").mkdir()
    (wiki / "sources" / "s1.md").write_text(
        "---\ntype: source\ntitle: S1\npublished: 2026-09-19\n---\nbody\n")
    (wiki / "entities").mkdir()
    (wiki / "entities" / "acme.md").write_text(
        "---\ntype: organization\nname: Acme\nsources: [sources/s1]\n---\nbody\n")


def _run_triage_selector(tmp_path, monkeypatch, op, *, with_work):
    vault = tmp_path / "vault"
    vault.mkdir()
    if with_work:
        _triage_vault(vault)
    else:
        (vault / "wiki").mkdir()
    manifest = tmp_path / "selection.json"
    # A stale, non-empty manifest from an earlier run must never survive this one.
    manifest.write_text(_json.dumps({"api": 1, "selected": ["stale-key"]}))
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(manifest))
    monkeypatch.setenv("OKENGINE_LANE_ID", f"okengine.predictions:{op}")
    monkeypatch.setenv("OKENGINE_CONTRACT_DIGEST", "sha256:contract")
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-09-20")
    monkeypatch.setenv("PREDICTION_CANDIDATE_MIN", "1")
    monkeypatch.setenv("HERMES_DATA", str(tmp_path / "hermes-data"))
    monkeypatch.syspath_prepend(str(EXT))
    selector = _load(f"triage_{op.replace('-', '_')}_{with_work}", EXT / _TRIAGE_SELECTORS[op])
    return selector, manifest, vault


@pytest.mark.parametrize("op", sorted(_TRIAGE_SELECTORS))
def test_triage_lane_declares_per_selected_item_completion(op):
    operation = _manifest()["operations"][op]
    assert operation["output_contract"]["completion"] == "per-selected-item", (
        f"{op} must be judged per selected item — under `completion: run` a correctly "
        "all-deferred batch fails for writing nothing (okengine#878)")
    assert operation["entrypoint"] == _TRIAGE_SELECTORS[op]


@pytest.mark.parametrize("op", ["forecast-review", "base-rates", "output-outcome-eval"])
def test_publishing_lanes_keep_whole_run_completion(op):
    """These owe a dashboard whenever they wake, so zero writes after waking stays a failure."""
    assert _manifest()["operations"][op]["output_contract"]["completion"] == "run"


@pytest.mark.parametrize("op", sorted(_TRIAGE_SELECTORS))
def test_triage_selector_writes_manifest_and_matching_receipt(tmp_path, monkeypatch, capsys, op):
    selector, manifest, vault = _run_triage_selector(tmp_path, monkeypatch, op, with_work=True)

    assert selector.main() == 0

    out = capsys.readouterr().out
    written = _json.loads(manifest.read_text())
    assert '"wakeAgent": true' in out, out
    assert written["selected"], f"{op} woke the agent but selected nothing"
    assert written["lane_id"] == f"okengine.predictions:{op}"
    assert written["contract_digest"] == "sha256:contract"
    for key in written["selected"]:
        assert key.startswith("wiki/"), key
        assert (vault / key.split("|", 1)[0]).is_file(), f"{op} key names no page: {key}"
    blocks = _RECEIPT.findall(out)
    assert len(blocks) == 1, f"{op} must print exactly one okengine-receipt template"
    receipt = _json.loads(blocks[0])
    assert [item["key"] for item in receipt["items"]] == written["selected"]
    assert receipt["input_digest"] == written["input_digest"]
    assert "deferred-for-review" in receipt["items"][0]["disposition"]


@pytest.mark.parametrize("op", sorted(_TRIAGE_SELECTORS))
def test_triage_selector_refreshes_an_empty_manifest_when_idle(tmp_path, monkeypatch, capsys, op):
    selector, manifest, _ = _run_triage_selector(tmp_path, monkeypatch, op, with_work=False)

    assert selector.main() == 0

    out = capsys.readouterr().out
    assert '"wakeAgent": false' in out, out
    assert _json.loads(manifest.read_text())["selected"] == [], (
        f"{op}: a no-work run must refresh the manifest EMPTY, or the runner demands a receipt "
        "for a stale selection")
    assert "okengine-receipt" not in out


def test_update_in_place_triage_keys_bind_the_prewrite_digest(tmp_path, monkeypatch, capsys):
    selector, manifest, vault = _run_triage_selector(tmp_path, monkeypatch, "grade", with_work=True)
    assert selector.main() == 0
    capsys.readouterr()
    page = vault / "wiki" / "predictions" / "p1.md"
    digest = __import__("hashlib").sha256(page.read_bytes()).hexdigest()
    assert _json.loads(manifest.read_text())["selected"] == [
        f"wiki/predictions/p1.md|sha256:{digest}"]


def test_candidate_watch_receipt_points_writes_at_a_new_prediction(tmp_path, monkeypatch, capsys):
    """The key is the ENTITY input; an accepted item writes a NEW predictions/ page."""
    selector, manifest, _ = _run_triage_selector(
        tmp_path, monkeypatch, "candidate-watch", with_work=True)
    assert selector.main() == 0
    receipt = _json.loads(_RECEIPT.findall(capsys.readouterr().out)[0])
    assert _json.loads(manifest.read_text())["selected"] == ["wiki/entities/acme.md"]
    assert receipt["items"][0]["writes"][0]["path"].startswith("predictions/")


def test_schema_drain_below_threshold_selects_nothing(tmp_path, monkeypatch, capsys):
    """wakeAgent=false with a non-empty manifest would make the runner demand a receipt."""
    monkeypatch.setenv("PSD_MIN_HITS", "5")
    selector, manifest, _ = _run_triage_selector(
        tmp_path, monkeypatch, "prediction-schema-drain", with_work=True)

    assert selector.main() == 0

    out = capsys.readouterr().out
    assert '"wakeAgent": false' in out
    assert _json.loads(manifest.read_text())["selected"] == []
    assert "okengine-receipt" not in out


@pytest.mark.parametrize("op", sorted(_TRIAGE_SELECTORS))
def test_triage_prompt_asks_for_the_printed_receipt(op):
    prompt = (EXT / _manifest()["operations"][op]["prompt_file"]).read_text(encoding="utf-8")
    assert "okengine-receipt" in prompt
    assert "deferred-for-review" in prompt
    assert "written | deferred | rejected" not in prompt, (
        "the free-text receipt line is not a vocabulary the runner accepts")


def _run_as_script(selector: Path, vault: Path, manifest: Path):
    """Run a selector the way cron-plus does: a fresh interpreter, no inherited PYTHONPATH, so
    every import must resolve from the selector's own sys.path bootstrap."""
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(WIKI_PATH=str(vault), OKENGINE_SELECTION_MANIFEST=str(manifest))
    return subprocess.run([sys.executable, str(selector)], env=env, cwd=str(vault),
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("op", sorted(_TRIAGE_SELECTORS))
def test_triage_selector_runs_standalone_in_the_repo_layout(tmp_path, op):
    """selection_manifest resolves from scripts/cron relative to the extension directory."""
    (tmp_path / "wiki").mkdir()
    manifest = tmp_path / "selection.json"

    proc = _run_as_script(EXT / _TRIAGE_SELECTORS[op], tmp_path, manifest)

    assert proc.returncode == 0, proc.stderr
    assert _json.loads(manifest.read_text())["selected"] == []


@pytest.mark.parametrize("op", sorted(_TRIAGE_SELECTORS))
def test_triage_selector_runs_standalone_in_the_staged_layout(tmp_path, op):
    """Deployed layout: /opt/data/scripts/<ext-id>/<selector> with shared libs one level up."""
    import shutil
    scripts = tmp_path / "scripts"
    shutil.copytree(EXT, scripts / "okengine.predictions")
    # The engine cron libs staged beside every extension dir (deploy-cron-scripts.sh), incl. the
    # generated-page identity helper the audit lanes write through (okengine#933).
    for lib in ("selection_manifest.py", "generated_page.py", "id_lib.py", "schema_lib.py"):
        shutil.copy(REPO / "scripts" / "cron" / lib, scripts)
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    manifest = tmp_path / "selection.json"

    proc = _run_as_script(scripts / "okengine.predictions" / _TRIAGE_SELECTORS[op], vault, manifest)

    assert proc.returncode == 0, proc.stderr
    assert _json.loads(manifest.read_text())["selected"] == []


def test_schema_drain_selects_every_batched_page_above_the_threshold(tmp_path, monkeypatch, capsys):
    """More drifted pages than PSD_MIN_HITS still wakes, selects and lists the whole batch."""
    selector, manifest, vault = _run_triage_selector(
        tmp_path, monkeypatch, "prediction-schema-drain", with_work=True)
    (vault / "wiki" / "predictions" / "p2.md").write_text(
        "---\ntype: prediction\nstatus: open\nconfidence: 0.4\n---\n# P2\n")

    assert selector.main() == 0

    out = capsys.readouterr().out
    assert '"wakeAgent": true' in out
    selected = _json.loads(manifest.read_text())["selected"]
    assert sorted(key.split("|", 1)[0] for key in selected) == [
        "wiki/predictions/p1.md", "wiki/predictions/p2.md"]
    listed = _re.findall(r"^(\d+)\. \[\[predictions/(p\d)\]\]$", out, _re.M)
    assert sorted(n for n, _ in listed) == ["1", "2"] and sorted(s for _, s in listed) == ["p1", "p2"]
    assert "   - missing required: made_on" in out
