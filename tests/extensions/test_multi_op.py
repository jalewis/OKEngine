"""Multi-operation extensions: an extension declares a plural ``operations:`` map
and the composer synthesizes one namespaced job per operation (``<id>:<op>``),
realizing the discovery-spec §3.5 ``<id>:<local>`` namespacing. The singular
``operation:`` form stays back-compatible (one job named ``<id>``).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"

pytestmark = pytest.mark.skipif(not COMPOSE.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _record(ext_id, manifest):
    return {"id": ext_id, "tier": "engine", "dir": "/x", "manifest": manifest}


def _multi_manifest(ext_id):
    return {
        "id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
        "requires": {"engine": ">=0.4.0"},
        "capabilities": {"read": ["wiki/**"], "write": ["predictions/**"]},
        "operations": {
            "candidate-watch": {"schedule": {"kind": "cron", "expr": "17 6 * * *"},
                                "entrypoint": "select_candidates.py"},
            "grade": {"schedule": {"kind": "cron", "expr": "23 6 * * *"},
                      "entrypoint": {"script": "grade.py"}},
            "regrade": {"schedule": {"kind": "cron", "expr": "29 */6 * * *"},
                        "entrypoint": "regrade.py"},
        },
    }


def _single_manifest(ext_id):
    return {
        "id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
        "requires": {"engine": ">=0.4.0"},
        "capabilities": {"read": ["wiki/**"], "write": ["dashboards/**"]},
        "operation": {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                      "entrypoint": "run.py"},
    }


# --- composer: one job per operation, namespaced ---------------------------

def test_multi_op_yields_one_job_per_operation():
    c = _load("extension_compose", COMPOSE)
    jobs, errors, _ = c.synthesize_ops(_record("okengine.predictions", _multi_manifest("okengine.predictions")))
    assert not errors, errors
    names = sorted(j["name"] for j in jobs)
    assert names == ["okengine.predictions:candidate-watch",
                     "okengine.predictions:grade",
                     "okengine.predictions:regrade"]
    # each carries its own schedule + a namespaced staged script path + a unique id
    by = {j["name"]: j for j in jobs}
    assert by["okengine.predictions:grade"]["schedule"]["expr"] == "23 6 * * *"
    assert by["okengine.predictions:grade"]["script"] == \
        "/opt/data/scripts/okengine.predictions/grade.py"
    assert len({j["id"] for j in jobs}) == 3


def test_single_operation_stays_backcompat():
    c = _load("extension_compose", COMPOSE)
    jobs, errors, _ = c.synthesize_ops(_record("okengine.contradictions", _single_manifest("okengine.contradictions")))
    assert not errors, errors
    assert [j["name"] for j in jobs] == ["okengine.contradictions"]   # no :op suffix
    assert jobs[0]["script"] == "/opt/data/scripts/okengine.contradictions/run.py"


def test_synthesize_jobs_flattens_multi_op_across_extensions():
    c = _load("extension_compose", COMPOSE)
    resolved = {
        "okengine.predictions": _record("okengine.predictions", _multi_manifest("okengine.predictions")),
        "okengine.contradictions": _record("okengine.contradictions", _single_manifest("okengine.contradictions")),
    }
    jobs, errors, _ = c.synthesize_jobs(resolved)
    assert not errors, errors
    assert len(jobs) == 4                                  # 3 + 1
    assert len({j["name"] for j in jobs}) == 4             # all uniquely namespaced


def test_both_forms_is_an_error():
    c = _load("extension_compose", COMPOSE)
    m = _single_manifest("x.y")
    m["operations"] = _multi_manifest("x.y")["operations"]
    _, errors, _ = c.synthesize_ops(_record("x.y", m))
    assert any("both" in e for e in errors), errors


def test_empty_operations_map_is_an_error():
    c = _load("extension_compose", COMPOSE)
    m = _multi_manifest("x.y")
    m["operations"] = {}
    _, errors, _ = c.synthesize_ops(_record("x.y", m))
    assert any("empty" in e for e in errors), errors


# --- manifest validation accepts the plural form ---------------------------

def test_manifest_validates_operations_map():
    mod = _load("extension_manifest", MANIFEST)
    errors, _ = mod.validate_manifest(_multi_manifest("okengine.predictions"))
    assert not errors, errors


def test_manifest_rejects_unknown_key_under_an_operation():
    mod = _load("extension_manifest", MANIFEST)
    m = _multi_manifest("okengine.predictions")
    m["operations"]["grade"]["bogus"] = 1
    errors, _ = mod.validate_manifest(m)
    assert any("operations.grade" in e and "bogus" in e for e in errors), errors


def test_manifest_rejects_both_forms():
    mod = _load("extension_manifest", MANIFEST)
    m = _single_manifest("x.y")
    m["operations"] = _multi_manifest("x.y")["operations"]
    errors, _ = mod.validate_manifest(m)
    assert any("either 'operation' or 'operations'" in e for e in errors), errors
