"""Per-deployment cron SCHEDULE overrides — `<pack>/.okengine/cron-schedules.json`.

A deployment could already retune a lane's MODEL (`cron-models.json`) but not its SCHEDULE, and
the two are not independent: cron expressions are read in the deployment's TZ while a provider's
peak-price window is fixed in UTC, so whether a lane is expensive depends on a mapping only the
deployment knows. The engine's own defaults are right for what they are — the overnight
maintenance band exists so heavy lanes run while nobody is reading — and cannot also be right for
every TZ's billing. Before this, moving one cost-bearing lane out of a peak window meant editing
the engine for everyone.

The failure mode these tests mostly guard is the quiet one: an override that matches no lane, or
carries an expression the scheduler cannot parse, leaving the operator believing a lane moved
while it fires at the old time.
"""
from __future__ import annotations

import importlib.util
import builtins
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "cron_jitter.py"


def _load():
    spec = importlib.util.spec_from_file_location("cron_jitter_schedules", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cron_jitter_schedules"] = m
    spec.loader.exec_module(m)
    return m


cj = _load()


def test_concrete_cron_error_reports_missing_parser_and_runtime_failure(monkeypatch):
    real_import = builtins.__import__

    def no_croniter(name, *args, **kwargs):
        if name == "croniter":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_croniter)
    assert "unavailable on the host" in cj.concrete_cron_error("0 0 * * *")
    monkeypatch.setattr(builtins, "__import__", real_import)

    class BrokenCroniter:
        @staticmethod
        def is_valid(_expr):
            return True

        def __init__(self, *_args):
            raise KeyError("bad field")

    monkeypatch.setitem(sys.modules, "croniter", type("CroniterModule", (), {"croniter": BrokenCroniter})())
    assert "not schedulable" in cj.concrete_cron_error("0 0 * * *")


def jobs():
    """One of each schedule shape the loader accepts, so the writer is exercised on all three."""
    return [
        {"name": "dict-shape", "schedule": {"expr": "40 4 * * 1"}},
        {"name": "string-shape", "schedule": "0 13 * * SUN"},
        {"name": "top-level-shape", "expr": "30 3 * * *"},
    ]


# --- loading ---------------------------------------------------------------------------------

def test_an_absent_file_is_not_an_error(tmp_path):
    """Every deployment that has never needed an override must deploy unchanged."""
    assert cj.load_lane_schedules(tmp_path) == {}


def test_a_present_file_is_read(tmp_path):
    d = tmp_path / ".okengine"
    d.mkdir()
    (d / "cron-schedules.json").write_text(json.dumps({"trends-refresh": "40 11 * * 1"}))
    assert cj.load_lane_schedules(tmp_path) == {"trends-refresh": "40 11 * * 1"}


def test_malformed_json_raises_rather_than_deploying_the_old_schedule(tmp_path):
    """Silently ignoring a broken override file would deploy the unmodified schedule while the
    operator believes the lane moved — the exact confusion this feature exists to remove."""
    d = tmp_path / ".okengine"
    d.mkdir()
    (d / "cron-schedules.json").write_text("{not json")
    with pytest.raises(ValueError, match="cron-schedules.json"):
        cj.load_lane_schedules(tmp_path)


def test_a_non_mapping_file_raises(tmp_path):
    d = tmp_path / ".okengine"
    d.mkdir()
    (d / "cron-schedules.json").write_text(json.dumps(["40 11 * * 1"]))
    with pytest.raises(ValueError, match="must be a .*map"):
        cj.load_lane_schedules(tmp_path)


# --- applying --------------------------------------------------------------------------------

def test_the_override_replaces_the_expression(tmp_path):
    js = jobs()
    n, errors = cj.apply_lane_schedules(js, {"dict-shape": "40 11 * * 1"})
    assert (n, errors) == (1, [])
    assert cj._job_expr(js[0]) == "40 11 * * 1"


@pytest.mark.parametrize("lane", ["dict-shape", "string-shape", "top-level-shape"])
def test_every_schedule_shape_is_writable(lane):
    """`framework validate` accepts three shapes. An override that only handled the dict form
    would silently do nothing on the other two — and report success, since it counted the key."""
    js = jobs()
    n, errors = cj.apply_lane_schedules(js, {lane: "15 12 * * 3"})
    assert (n, errors) == (1, [])
    assert cj._job_expr({j["name"]: j for j in js}[lane]) == "15 12 * * 3"


def test_lanes_without_an_override_are_untouched():
    js = jobs()
    before = [cj._job_expr(j) for j in js[1:]]
    cj.apply_lane_schedules(js, {"dict-shape": "40 11 * * 1"})
    assert [cj._job_expr(j) for j in js[1:]] == before


def test_an_unknown_lane_name_fails_loudly():
    """The quiet failure this feature is most likely to have: a renamed or typo'd lane. Applying
    nothing and reporting success would leave the lane firing at the old, expensive time."""
    n, errors = cj.apply_lane_schedules(jobs(), {"lane-that-moved": "40 11 * * 1"})
    assert n == 0 and len(errors) == 1
    assert "no cron lane named 'lane-that-moved'" in errors[0]


@pytest.mark.parametrize("bad", ["", "   ", "40 11 * *", "40 11 * * 1 extra",
                                  "every monday", "99 5 * * *", "5 99 * * *",
                                  "5 9 * * 99"])
def test_an_unparseable_expression_fails_rather_than_shipping(bad):
    """cron-plus cannot parse a malformed expr, so the lane would never fire again — worse than
    the peak-price run it was meant to avoid. Reject at deploy, not at the next tick."""
    n, errors = cj.apply_lane_schedules(jobs(), {"dict-shape": bad})
    assert n == 0 and len(errors) == 1
    assert "dict-shape" in errors[0]


def test_invalid_five_field_override_leaves_original_schedule_untouched():
    js = jobs()
    before = cj._job_expr(js[0])
    count, errors = cj.apply_lane_schedules(js, {"dict-shape": "99 99 * * *"})
    assert count == 0 and errors, "out-of-range fields must fail before deploy"
    assert cj._job_expr(js[0]) == before


def test_a_non_string_expression_fails():
    n, errors = cj.apply_lane_schedules(jobs(), {"dict-shape": 1140})
    assert n == 0 and len(errors) == 1 and "non-empty string" in errors[0]


@pytest.mark.parametrize("sentinel", ["@jitter:daily", "@jitter:weekly", "@morning", "@morning:30"])
def test_a_sentinel_override_is_accepted_and_left_for_the_expanders(sentinel):
    """Overrides are applied BEFORE the sentinel expanders, so an operator may hand back a
    sentinel and get the same per-install spread the engine default would have had."""
    js = jobs()
    n, errors = cj.apply_lane_schedules(js, {"dict-shape": sentinel})
    assert (n, errors) == (1, [])
    assert cj._job_expr(js[0]) == sentinel


def test_an_overridden_sentinel_still_expands():
    """The ordering claim, asserted end to end rather than described: override -> expand leaves a
    concrete expression, so a sentinel override cannot reach cron-plus unexpanded."""
    import random
    js = jobs()
    cj.apply_lane_schedules(js, {"dict-shape": "@jitter:daily"})
    cj.expand_jobs(js, random.Random(0))
    expr = cj._job_expr(js[0])
    assert not cj.is_sentinel(expr) and len(expr.split()) == 5


def test_a_concrete_override_survives_the_expanders():
    """The other half: an operator's explicit time must not be re-jittered into a different one."""
    import random
    js = jobs()
    cj.apply_lane_schedules(js, {"dict-shape": "40 11 * * 1"})
    cj.expand_brief_jobs(js, 7)
    cj.expand_jobs(js, random.Random(0))
    assert cj._job_expr(js[0]) == "40 11 * * 1"


def test_several_errors_are_all_reported():
    """One deploy attempt should surface every bad key, not the first — otherwise fixing an
    override file becomes one failed deploy per typo."""
    n, errors = cj.apply_lane_schedules(jobs(), {"ghost": "40 11 * * 1", "dict-shape": "nonsense"})
    assert n == 0 and len(errors) == 2


# --- the deploy path actually calls it -------------------------------------------------------

def test_the_deploy_script_applies_overrides_before_the_expanders():
    """A gate wired in the wrong order is not wired: if overrides were applied after expansion, a
    sentinel override would reach cron-plus unexpanded and the lane would never fire."""
    text = (REPO / "scripts" / "deploy-cron-plus-jobs.sh").read_text(encoding="utf-8")
    assert "load_lane_schedules" in text and "apply_lane_schedules" in text
    assert text.index("apply_lane_schedules") < text.index("expand_brief_jobs(jobs, brief_hour)")
    assert text.index("apply_lane_schedules") < text.index("cron_jitter.expand_jobs(jobs,")


def test_the_deploy_script_refuses_to_deploy_on_an_override_error():
    """CANNOT DETECT: that the exit actually runs — this reads the script's text, not a run of it.
    It detects only that the error branch still exits non-zero rather than warning and continuing,
    which is the difference between a rejected deploy and a lane silently left where it was."""
    text = (REPO / "scripts" / "deploy-cron-plus-jobs.sh").read_text(encoding="utf-8")
    block = text[text.index("sn, serr = cron_jitter.apply_lane_schedules"):]
    block = block[:block.index("bn = cron_jitter.expand_brief_jobs")]
    assert "sys.exit(1)" in block and "not deploying" in block
