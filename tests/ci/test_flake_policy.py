import importlib.util
import json
import runpy
import sys
from datetime import date
from pathlib import Path


MODULE = Path(__file__).parents[2] / "ci" / "flake_policy.py"
SPEC = importlib.util.spec_from_file_location("flake_policy", MODULE)
flake_policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flake_policy)


def entry(**updates):
    value = {
        "nodeid": "tests/test_example.py::test_case",
        "owner": "maintainer",
        "issue": "https://tracker.example/1",
        "reason": "order-dependent failure",
        "first_seen": "2026-08-20",
        "expires_on": "2026-09-03",
        "evidence": ["https://ci.example/jobs/1"],
    }
    value.update(updates)
    return value


def test_empty_registry_is_valid_and_reports_a_measured_zero():
    report, errors = flake_policy.validate(
        {"api": 1, "quarantines": []}, today=date(2026, 8, 27))
    assert errors == []
    assert report["active"] == 0


def test_valid_bounded_quarantine_is_retained():
    item = entry()
    report, errors = flake_policy.validate(
        {"api": 1, "quarantines": [item]}, today=date(2026, 8, 27))
    assert errors == []
    assert report["quarantines"] == [item]


def test_missing_ownership_evidence_and_exact_nodeid_fail():
    _, errors = flake_policy.validate({"api": 1, "quarantines": [entry(
        nodeid="test_case", owner="", issue="issue-1", evidence=[]) ]},
        today=date(2026, 8, 27))
    assert any("exact tests/" in error for error in errors)
    assert any("owner is required" in error for error in errors)
    assert any("durable URL" in error for error in errors)
    assert any("non-empty durable" in error for error in errors)


def test_expired_overlong_duplicate_and_unknown_fields_fail():
    first = entry(expires_on="2026-09-04", surprise=True)
    second = entry()
    _, errors = flake_policy.validate(
        {"api": 1, "quarantines": [first, second]}, today=date(2026, 9, 5))
    assert any("unknown key" in error for error in errors)
    assert any("expired" in error for error in errors)
    assert any("within 14 days" in error for error in errors)
    assert any("duplicated" in error for error in errors)


def test_registry_shape_api_and_unknown_keys_fail_exactly():
    report, errors = flake_policy.validate([], today=date(2026, 8, 27))
    assert report == {"api": 1, "active": 0, "quarantines": []}
    assert errors == ["registry must be an object"]

    _, empty_errors = flake_policy.validate({}, today=date(2026, 8, 27))
    assert empty_errors == ["api must be 1", "quarantines must be a list"]

    report, errors = flake_policy.validate(
        {"api": 2, "quarantines": "bad", "extra": True},
        today=date(2026, 8, 27),
    )
    assert report == {"api": 1, "active": 0, "quarantines": []}
    assert errors == [
        "api must be 1",
        "registry has unknown key(s): ['extra']",
        "quarantines must be a list",
    ]
    _, zero_api_errors = flake_policy.validate(
        {"api": 0, "quarantines": []}, today=date(2026, 8, 27)
    )
    assert zero_api_errors == ["api must be 1"]
    for api in (False, 1.0):
        _, typed_api_errors = flake_policy.validate(
            {"api": api, "quarantines": []}, today=date(2026, 8, 27)
        )
        assert typed_api_errors == ["api must be 1"]


def test_scalar_entry_does_not_hide_later_entry_errors():
    _, errors = flake_policy.validate(
        {"api": 1, "quarantines": ["bad", entry(owner="")]},
        today=date(2026, 8, 27),
    )
    assert "quarantines[0] must be an object" in errors
    assert "quarantines[1].owner is required" in errors


def test_missing_extra_and_each_nodeid_boundary_are_reported():
    missing = entry()
    del missing["reason"]
    _, errors = flake_policy.validate(
        {"api": 1, "quarantines": [
            missing,
            entry(nodeid="other/test.py::test_case", surprise=True),
            entry(nodeid="tests/test_example.py"),
        ]},
        today=date(2026, 8, 27),
    )
    assert any("missing key(s): ['reason']" in error for error in errors)
    assert any("unknown key(s): ['surprise']" in error for error in errors)
    assert sum("exact tests/" in error for error in errors) == 2

    combined = entry(surprise=True)
    del combined["reason"]
    _, combined_errors = flake_policy.validate(
        {"api": 1, "quarantines": [combined]}, today=date(2026, 8, 27)
    )
    assert combined_errors[:2] == [
        "quarantines[0] missing key(s): ['reason']",
        "quarantines[0] has unknown key(s): ['surprise']",
    ]


def test_reason_issue_and_evidence_are_independently_required():
    _, errors = flake_policy.validate(
        {"api": 1, "quarantines": [entry(
            reason=" ", issue="ftp://tracker/1", evidence=["ftp://ci/1"]
        )]},
        today=date(2026, 8, 27),
    )
    assert "quarantines[0].reason is required" in errors
    assert "quarantines[0].issue must be a durable URL" in errors
    assert "quarantines[0].evidence must be a non-empty durable URL/artifact list" in errors

    for evidence in ("https://ci.example/1", [], ["artifacts/run.json"]):
        _, evidence_errors = flake_policy.validate(
            {"api": 1, "quarantines": [entry(evidence=evidence)]},
            today=date(2026, 8, 27),
        )
        if evidence == ["artifacts/run.json"]:
            assert evidence_errors == []
        else:
            assert any("non-empty durable" in error for error in evidence_errors)


def test_date_parse_order_and_exact_expiry_boundaries():
    _, errors = flake_policy.validate(
        {"api": 1, "quarantines": [entry(first_seen="bad", expires_on="also-bad")]},
        today=date(2026, 8, 27),
    )
    assert errors == ["quarantines[0].first_seen/expires_on must be ISO dates"]

    for expires_on, today, expected in [
        ("2026-09-03", date(2026, 9, 3), []),
        ("2026-08-30", date(2026, 8, 27), []),
        ("2026-08-20", date(2026, 8, 20), []),
        ("2026-09-04", date(2026, 8, 27), ["within 14 days"]),
        ("2026-08-19", date(2026, 8, 27), ["expired", "within 14 days"]),
    ]:
        report, boundary_errors = flake_policy.validate(
            {"api": 1, "quarantines": [entry(expires_on=expires_on)]},
            today=today,
        )
        assert report["active"] == 1
        for fragment in expected:
            assert any(fragment in error for error in boundary_errors)
        if not expected:
            assert boundary_errors == []


def test_main_publishes_success_and_failure_reports(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry.yaml"
    output = tmp_path / "nested" / "deep" / "report.json"
    registry.write_text("api: 1\nquarantines: []\n", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["flake_policy.py", "--registry", str(registry), "--output", str(output)]
    )
    assert flake_policy.main() == 0
    output_text = output.read_text()
    assert json.loads(output_text)["active"] == 0
    assert '\n  "api": 1,' in output_text
    assert "PASS (0 active quarantine(s))" in capsys.readouterr().out

    registry.write_text("api: 2\nquarantines: []\n", encoding="utf-8")
    assert flake_policy.main() == 1
    report = json.loads(output.read_text())
    assert report["errors"] == ["api must be 1"]
    assert "flake policy: FAIL" in capsys.readouterr().out


def test_main_fails_when_registry_is_missing_or_yaml_is_invalid(
        tmp_path, monkeypatch, capsys):
    output = tmp_path / "report.json"
    for registry in (tmp_path / "missing.yaml", tmp_path / "invalid.yaml"):
        if registry.name == "invalid.yaml":
            registry.write_text("quarantines: [\n", encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv",
            ["flake_policy.py", "--registry", str(registry), "--output", str(output)],
        )
        assert flake_policy.main() == 2
        assert "registry unreadable" in capsys.readouterr().out
        assert not output.exists()


def test_script_entrypoint_propagates_main_exit(tmp_path, monkeypatch):
    registry = tmp_path / "registry.yaml"
    output = tmp_path / "report.json"
    registry.write_text("api: 1\nquarantines: []\n", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", [str(MODULE), "--registry", str(registry), "--output", str(output)]
    )
    try:
        runpy.run_path(str(MODULE), run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("flake policy entrypoint did not exit")
