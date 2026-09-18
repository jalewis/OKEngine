import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE = Path(__file__).parent / "performance" / "test_release_performance.py"


@pytest.fixture(scope="module")
def performance_module():
    spec = importlib.util.spec_from_file_location("performance_environment_subject", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text,expected", [
    ("some avg10=7.25 avg60=3.00 avg300=1.00 total=4\nfull avg10=0.0", 7.25),
    ("full avg10=1.0", None),
    ("some avg10=invalid", None),
    ("", None),
])
def test_cpu_pressure_parser_is_explicit_and_total(performance_module, text, expected):
    assert performance_module._parse_cpu_pressure(text) == expected


def test_pressure_threshold_has_a_declared_nonvacuous_bound(performance_module):
    assert 0 < performance_module.CPU_PRESSURE_AVG10_MAX < 100


@pytest.mark.parametrize("start,end,expected", [
    (0.0, 20.0, "controlled"),
    (None, 0.0, "environment_indeterminate"),
    (0.0, None, "environment_indeterminate"),
    (20.01, 0.0, "environment_indeterminate"),
    (0.0, 20.01, "environment_indeterminate"),
])
def test_pressure_classification_fails_closed(performance_module, start, end, expected):
    assert performance_module._classify_cpu_pressure(start, end) == expected


def test_metrics_fixture_retains_evidence_and_fails_indeterminate_run(
        performance_module, tmp_path, monkeypatch):
    samples = iter((0.0, 20.01))
    monkeypatch.setattr(performance_module, "ARTIFACTS", tmp_path)
    monkeypatch.setattr(performance_module, "_cpu_pressure", lambda: next(samples))
    monkeypatch.setattr(performance_module.shutil, "which", lambda _name: None)

    fixture = performance_module.metrics.__wrapped__()
    next(fixture)
    with pytest.raises(pytest.fail.Exception, match="environment is indeterminate"):
        next(fixture)

    evidence = json.loads((tmp_path / "performance-environment.json").read_text())
    assert evidence["measurement_classification"] == "environment_indeterminate"
    assert evidence["cpu_pressure_some_avg10"]["maximum"] == 20.01
