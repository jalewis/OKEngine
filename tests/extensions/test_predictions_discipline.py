"""okengine.predictions forecasting-discipline lanes (okengine#159 P1): calibration (Brier +
buckets) and date-audit, both deterministic no_agent ops."""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.predictions"


def _load(name):
    # pred_lib must import as a sibling
    sys.path.insert(0, str(EXT))
    spec = importlib.util.spec_from_file_location(name, EXT / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


def _pred(d: Path, slug, status, confidence=None, resolves_by=None):
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\ntype: prediction\nstatus: {status}\nsubject: x\n"
    if confidence is not None:
        fm += f"confidence: {confidence}\n"
    if resolves_by is not None:
        fm += f"resolves_by: {resolves_by}\n"
    (d / f"{slug}.md").write_text(fm + "---\n# p\n")


def test_confidence_prob():
    cal = _load("calibration_refresh")
    s = cal._scale()
    assert cal.confidence_prob("high", s) == 0.75
    assert cal.confidence_prob("medium-high", s) == 0.625
    assert cal.confidence_prob("0.8", s) == 0.8
    assert cal.confidence_prob("70%", s) == 0.7
    assert cal.confidence_prob("nonsense", s) is None


def test_calibration_brier(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred(pr, "hit-high", "confirmed", "high")      # prob .75, outcome 1 -> (.25)^2
    _pred(pr, "miss-high", "refuted", "high")        # prob .75, outcome 0 -> (.75)^2
    _pred(pr, "open-one", "open", "high")            # excluded (open)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    cal = _load("calibration_refresh")
    assert cal.main() == 0
    dash = (tmp_path / "wiki" / "dashboards" / "calibration.md").read_text()
    assert "resolved & scored: **2**" in dash
    # Brier = ((.25)^2 + (.75)^2)/2 = (0.0625+0.5625)/2 = 0.3125
    assert "Brier: **0.312**" in dash or "Brier: **0.313**" in dash


def test_date_audit_flags(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred(pr, "no-date", "open")                                  # missing
    _pred(pr, "overdue", "open", resolves_by="2020-01-01")        # open + overdue
    _pred(pr, "fine", "open", resolves_by="2099-01-01")           # > horizon (~5y) -> flagged too
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    da = _load("prediction_date_audit")
    assert da.main() == 0
    dash = (tmp_path / "wiki" / "dashboards" / "prediction-date-audit.md").read_text()
    assert "missing/unparseable" in dash and "overdue" in dash


def test_manifest_has_three_no_agent_ops():
    m = yaml.safe_load((EXT / "extension.yaml").read_text())
    ops = m["operations"]
    for name in ("calibration-refresh", "prediction-date-audit", "prediction-schema-audit"):
        assert name in ops
        assert "prompt_file" not in ops[name] and "prompt" not in ops[name]   # no_agent
        assert ops[name]["entrypoint"].endswith(".py")


def test_manifest_has_forecast_review_agent_op():
    m = yaml.safe_load((EXT / "extension.yaml").read_text())
    ops = m["operations"]
    assert "forecast-review" in ops
    assert ops["forecast-review"]["prompt_file"] == "prompts/forecast-review.md"


def _pred_full(d: Path, slug: str, **fm_fields):
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: prediction"]
    for k, v in fm_fields.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "# p", "", "## What would refute this", "- nothing yet"]
    (d / f"{slug}.md").write_text("\n".join(lines) + "\n")


def test_schema_audit_flags_missing_and_mismatched_fields(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred_full(pr, "good", status="open", subject="x", confidence="0.6",
               made_on="2026-01-01", resolves_by="2026-02-01", horizon="short")
    # 31 days: short. Tagged medium -> mismatch.
    _pred_full(pr, "wrong-horizon", status="open", subject="x", confidence="0.6",
               made_on="2026-01-01", resolves_by="2026-02-01", horizon="medium")
    # missing made_on entirely
    _pred_full(pr, "no-made-on", status="open", subject="x", confidence="0.6",
               resolves_by="2026-02-01")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sa = _load("prediction_schema_audit")
    assert sa.main() == 0
    dash = (tmp_path / "wiki" / "dashboards" / "prediction-schema-audit.md").read_text()
    assert "horizon='medium' should be 'short'" in dash
    assert "missing made_on" in dash
    assert "good" not in [ln.split("|")[2].strip() for ln in dash.splitlines() if ln.startswith("| ")]


def test_schema_audit_flags_missing_refutation_section(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    pr.mkdir(parents=True)
    (pr / "no-refutation.md").write_text(
        "---\ntype: prediction\nstatus: open\nsubject: x\nconfidence: 0.5\n"
        "made_on: 2026-01-01\nresolves_by: 2026-02-01\nhorizon: short\n---\n# p\nno section here\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sa = _load("prediction_schema_audit")
    assert sa.main() == 0
    dash = (tmp_path / "wiki" / "dashboards" / "prediction-schema-audit.md").read_text()
    assert "missing '## What would refute this'" in dash


def test_schema_drain_gate_and_scope(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    pr.mkdir(parents=True)
    # value drift: non-canonical horizon -> in scope, wakes
    (pr / "drift.md").write_text(
        "---\ntype: prediction\nstatus: open\nsubject: x\nconfidence: 0.6\n"
        "made_on: 2026-01-01\nresolves_by: 2026-02-01\nhorizon: medium-term\n---\n# p\nclaim\n")
    assert _run("select_prediction_schema_drain", tmp_path, monkeypatch) is True

    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _load("select_prediction_schema_drain").main()
    out = buf.getvalue()
    assert "horizon drift" in out and "medium-term" in out

    # `active` is canonical (base-schema open_values) — a fully-canonical active prediction is NOT
    # drift, and a batch-container does NOT drive the wake (human-review only).
    (pr / "drift.md").write_text(
        "---\ntype: prediction\nstatus: active\nsubject: x\nconfidence: high\n"
        "made_on: 2026-01-01\nresolves_by: 2026-02-01\nhorizon: short\n---\n# p\nclaim\n")
    (pr / "batch.md").write_text(
        "---\ntype: prediction\nstatus: open\nsubject: x\nconfidence: 0.5\nmade_on: 2026-01-01\n"
        "resolves_by: 2026-02-01\nhorizon: short\n---\n# p\n## Prediction 1\na\n## Prediction 2\nb\n")
    assert _run("select_prediction_schema_drain", tmp_path, monkeypatch) is False   # nothing fixable


def test_structural_backfill_gate_and_scope(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred(pr, "open-no-refute", "open", "0.6", "2026-09-01")   # gradable + no section -> in scope
    assert _run("select_prediction_structural_backfill", tmp_path, monkeypatch) is True

    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _load("select_prediction_structural_backfill").main()
    assert "open-no-refute" in buf.getvalue()                  # digest lists the flagged page

    # scope: resolved / archived / already-sectioned are ALL excluded. Give the only gradable one a
    # section and add a resolved + an archived page — the lane must go quiet.
    (pr / "open-no-refute.md").write_text(
        (pr / "open-no-refute.md").read_text() + "\n## What would refute this\n- x\n")
    _pred(pr, "resolved-no-refute", "confirmed", "0.9", "2026-01-01")
    _pred(pr / "_archive", "archived-open", "open", "0.5", "2026-01-01")
    assert _run("select_prediction_structural_backfill", tmp_path, monkeypatch) is False


def test_forecast_review_gate(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred(pr, "old-open", "open", "0.6", "2099-01-01")   # not touched recently -> no wake
    assert _run("select_forecast_review", tmp_path, monkeypatch) is False
    from datetime import date
    _pred_full(pr, "resolved-this-week", status="confirmed", subject="x", confidence="0.8",
               updated=date.today().isoformat())
    assert _run("select_forecast_review", tmp_path, monkeypatch) is True


# --- P2 wake-gates: defer when there's nothing to do, fire when there is ---
import io, contextlib, json as _json  # noqa: E402


def _run(mod_name, tmp, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _load(mod_name).main()
    return _json.loads(buf.getvalue().strip().splitlines()[-1])["wakeAgent"]


def _resolved(pr, n):
    for i in range(n):
        _pred(pr, f"r{i}", "confirmed", "high", "2026-01-01")


def test_base_rates_gate(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _resolved(pr, 3)
    assert _run("select_base_rates", tmp_path, monkeypatch) is False    # < MIN (8)
    _resolved(pr, 8)
    assert _run("select_base_rates", tmp_path, monkeypatch) is True


def test_falsification_gate(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred(pr, "hi-open", "open", "high", "2099-01-01")
    assert _run("select_falsification", tmp_path, monkeypatch) is False   # no recent sources
    src = tmp_path / "wiki" / "sources" / "2026" / "06"
    src.mkdir(parents=True)
    from datetime import date
    (src / "s.md").write_text(f"---\ntype: source\npublished: {date.today().isoformat()}\n---\n# s\n")
    assert _run("select_falsification", tmp_path, monkeypatch) is True


def test_output_outcome_gate(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    from datetime import date
    for i in range(3):
        _pred(pr, f"o{i}", "confirmed", "high", date.today().isoformat())
    assert _run("select_output_outcome", tmp_path, monkeypatch) is False  # no briefings
    b = tmp_path / "wiki" / "briefings"; b.mkdir(parents=True)
    (b / "2026-06-28.md").write_text("---\ntype: dashboard\ntitle: brief\n---\n# brief\n")
    assert _run("select_output_outcome", tmp_path, monkeypatch) is True
