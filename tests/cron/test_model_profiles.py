"""Regression tests for named model profiles + deploy-time `@<profile>` expansion (okengine#151).

Covers the resolver (`scripts/model_profiles.py`), the operator extension model-override map
(`extension_compose._apply_model_overrides`), the pre-deploy validation
(`framework_validate.check_model_profiles`), and that an `@`-ref survives the cron split/merge
round-trip unchanged (expansion is a DEPLOY-only transform, never baked into the source).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


mp = _mod("model_profiles")

PROFILES = {
    "reasoning": {"provider": "custom", "base_url": "http://a:11436/v1",
                  "model": "qwen3.5:27b", "ollama_num_ctx": 65536},
    "bulk":      {"provider": "custom", "base_url": "http://b:11436/v1",
                  "model": "qwen3.5:9b", "ollama_num_ctx": 65536},
}


# --- the resolver ----------------------------------------------------------

def test_is_ref_distinguishes_sigil_from_literal():
    assert mp.is_ref("@reasoning") and mp.ref_name("@reasoning") == "reasoning"
    assert not mp.is_ref("qwen3.5:9b")        # bare = literal model
    assert not mp.is_ref("@") and not mp.is_ref(None) and not mp.is_ref(123)


def test_expand_resolves_ref_into_full_endpoint():
    jobs = [{"name": "lacuna", "model": "@reasoning"}]
    n, errs = mp.expand_jobs(jobs, PROFILES)
    assert n == 1 and not errs
    assert jobs[0] == {"name": "lacuna", "model": "qwen3.5:27b", "provider": "custom",
                       "base_url": "http://a:11436/v1", "ollama_num_ctx": 65536}


def test_expand_leaves_literal_and_missing_model_untouched():
    jobs = [{"name": "lit", "model": "openai/gpt-oss-120b:free"}, {"name": "none"}]
    n, errs = mp.expand_jobs(jobs, PROFILES)
    assert n == 0 and not errs
    assert jobs == [{"name": "lit", "model": "openai/gpt-oss-120b:free"}, {"name": "none"}]


def test_expand_unknown_ref_is_fail_loud_and_leaves_job_unchanged():
    jobs = [{"name": "oops", "model": "@ghost"}]
    n, errs = mp.expand_jobs(jobs, PROFILES)
    assert n == 0
    assert errs and "ghost" in errs[0]
    assert jobs[0] == {"name": "oops", "model": "@ghost"}   # not silently dropped to default


def test_validate_profiles_flags_shape_errors():
    bad = {"x": {"provider": "custom"},          # missing model + custom needs base_url
           "y": {"model": "m", "junk": 1},       # unknown field
           "z": "nope"}                          # not a mapping
    errs = mp.validate_profiles(bad)
    assert any("missing required 'model'" in e for e in errs)
    assert any("requires 'base_url'" in e for e in errs)
    assert any("unknown field" in e for e in errs)
    assert any("must be a mapping" in e for e in errs)
    assert mp.validate_profiles(PROFILES) == []


def test_load_profiles_absent_and_shape(tmp_path):
    assert mp.load_profiles(tmp_path) == {}      # no file -> empty, zero-impact
    ok = tmp_path / ".okengine"
    ok.mkdir()
    (ok / "model-profiles.yaml").write_text(yaml.safe_dump({"profiles": PROFILES}))
    assert mp.load_profiles(tmp_path) == PROFILES
    (ok / "model-profiles.yaml").write_text(yaml.safe_dump(["not", "a", "map"]))
    with pytest.raises(ValueError):
        mp.load_profiles(tmp_path)


# --- operator extension model-override map ---------------------------------

def test_extension_model_override_map(tmp_path):
    ec = _mod("extension_compose")
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "extension-models.json").write_text(json.dumps({
        "okengine.lacuna": "@reasoning",
        "okengine.glossary": "qwen3.5:4b",
    }))
    jobs = [{"name": "okengine.lacuna"}, {"name": "okengine.glossary"}, {"name": "okengine.dedupe"}]
    errs = ec._apply_model_overrides(jobs, tmp_path)
    assert not errs
    assert jobs[0]["model"] == "@reasoning"       # stays a ref -> resolved later at deploy
    assert jobs[1]["model"] == "qwen3.5:4b"       # literal
    assert "model" not in jobs[2]                 # untouched


def test_extension_model_override_unknown_job_is_fail_loud(tmp_path):
    ec = _mod("extension_compose")
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "extension-models.json").write_text(
        json.dumps({"okengine.nope": "@reasoning"}))
    errs = ec._apply_model_overrides([{"name": "okengine.lacuna"}], tmp_path)
    assert errs and "okengine.nope" in errs[0]


# --- pre-deploy validation -------------------------------------------------

def _pack(tmp_path, profiles=None, domain_crons=None, ext_models=None):
    ok = tmp_path / ".okengine"
    ok.mkdir(exist_ok=True)
    if profiles is not None:
        (ok / "model-profiles.yaml").write_text(yaml.safe_dump({"profiles": profiles}))
    if ext_models is not None:
        (ok / "extension-models.json").write_text(json.dumps(ext_models))
    cdir = tmp_path / "crons"
    cdir.mkdir(exist_ok=True)
    (cdir / "domain-crons.json").write_text(json.dumps(domain_crons or []))
    return tmp_path


def _run_check(pack):
    fv = _mod("framework_validate")
    r = fv.Report()
    fv.check_model_profiles(pack, r)
    return r


def test_validate_ok_when_refs_resolve(tmp_path):
    pack = _pack(tmp_path, profiles=PROFILES,
                 domain_crons=[{"name": "imp", "model": "@bulk"}],
                 ext_models={"okengine.lacuna": "@reasoning"})
    r = _run_check(pack)
    assert r.n_fail == 0
    assert any(sev == "OK" and "model profiles" in c for sev, c, _ in r.rows)


def test_validate_fails_on_undefined_ref(tmp_path):
    pack = _pack(tmp_path, profiles=PROFILES,
                 domain_crons=[{"name": "imp", "model": "@ghost"}])
    r = _run_check(pack)
    assert r.n_fail == 1
    assert any("ghost" in d for _, _, d in r.rows)


def test_validate_fails_on_ref_with_no_registry(tmp_path):
    pack = _pack(tmp_path, profiles=None,
                 domain_crons=[{"name": "imp", "model": "@bulk"}])
    r = _run_check(pack)
    assert r.n_fail == 1
    assert any("absent" in d for _, _, d in r.rows)


def test_validate_fails_on_malformed_registry(tmp_path):
    pack = _pack(tmp_path, profiles={"x": {"provider": "custom"}})  # missing model + base_url
    r = _run_check(pack)
    assert r.n_fail >= 1


def test_validate_info_when_no_profiles_and_no_refs(tmp_path):
    pack = _pack(tmp_path, profiles=None, domain_crons=[{"name": "imp", "model": "qwen3.5:9b"}])
    r = _run_check(pack)
    assert r.n_fail == 0


# --- round-trip: @-ref is NOT baked into the source ------------------------

def test_at_ref_survives_cron_split_merge_roundtrip():
    cps = _mod("cron_pack_split")
    jobs = [{"name": "okengine.lacuna", "model": "@reasoning", "extension": "okengine.lacuna",
             "schedule": "0 6 * * 1", "prompt": "x"}]
    tier_of = {}
    parts = cps.split(jobs, tier_of)
    merged = cps.merge(parts[cps.ENGINE_CRONS], parts[cps.DOMAIN_CRONS],
                       parts[cps.DOMAIN_PROMPTS], tier_of=tier_of,
                       extensions=parts[cps.EXTENSIONS])
    assert merged[0]["model"] == "@reasoning"     # ref preserved in source; expanded only at deploy


# --- per-lane model overrides for non-extension lanes (.okengine/cron-models.json) ---

def test_apply_lane_models_sets_and_failsloud():
    jobs = [{"name": "entity-backfill"}, {"name": "raw-backfill", "model": "qwen3.5:9b"}]
    n, errors = mp.apply_lane_models(jobs, {"entity-backfill": "@reasoning"})
    assert n == 1 and not errors
    assert jobs[0]["model"] == "@reasoning"          # set on the named lane
    assert jobs[1]["model"] == "qwen3.5:9b"          # untouched
    # fail-loud on a name that matches no lane, and on a non-string model
    _, e1 = mp.apply_lane_models(jobs, {"nope": "@reasoning"})
    assert e1 and "no cron lane named" in e1[0]
    _, e2 = mp.apply_lane_models(jobs, {"entity-backfill": ""})
    assert e2 and "non-empty string" in e2[0]


def test_load_lane_models(tmp_path):
    ok = tmp_path / ".okengine"
    ok.mkdir()
    (ok / "cron-models.json").write_text('{"entity-backfill": "@reasoning"}')
    assert mp.load_lane_models(tmp_path) == {"entity-backfill": "@reasoning"}
    assert mp.load_lane_models(tmp_path / "missing") == {}     # absent -> {}


def test_lane_override_then_profile_expansion(tmp_path):
    """End-to-end: a lane override of @reasoning resolves to the profile's concrete model."""
    jobs = [{"name": "entity-backfill"}]
    mp.apply_lane_models(jobs, {"entity-backfill": "@reasoning"})
    profiles = {"reasoning": {"provider": "custom", "base_url": "http://h:1/v1", "model": "qwen3.5:27b"}}
    n, errors = mp.expand_jobs(jobs, profiles)
    assert n == 1 and not errors
    assert jobs[0]["model"] == "qwen3.5:27b" and jobs[0]["provider"] == "custom"
