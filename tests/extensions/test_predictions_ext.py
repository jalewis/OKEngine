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


def test_manifest_is_valid_and_first_party():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.predictions"
    assert mod.is_reserved_id(m["id"])                 # first-party okengine.* namespace
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


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
                "okengine.predictions:prediction-schema-audit"]
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


def test_bundled_prompt_files_exist_and_nonempty():
    for op in ("candidate-watch", "grade", "regrade", "base-rates",
               "falsification-search", "output-outcome-eval", "structural-backfill", "schema-drain"):
        f = EXT / "prompts" / f"{op}.md"
        assert f.is_file() and f.read_text(encoding="utf-8").strip(), op


def test_write_capabilities():
    m = _manifest()
    # predictions (the book) + dashboards (derived base-rates / outcome-eval). No schema fragment.
    assert m["capabilities"]["write"] == ["predictions/**", "dashboards/**"]
    assert "schema" not in m                            # reuses the pack-owned prediction type
