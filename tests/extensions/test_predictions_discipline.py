"""okengine.predictions forecasting-discipline lanes (okengine#159 P1): calibration (Brier +
buckets) and date-audit, both deterministic no_agent ops."""
import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.predictions"


def _load(name, source=None):
    # pred_lib must import as a sibling
    sys.path.insert(0, str(EXT))
    spec = importlib.util.spec_from_file_location(name, EXT / f"{source or name}.py")
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
    cockpit_source = (
        REPO / "src" / "okengine" / "cockpit_services" / "state.py"
    ).read_text(encoding="utf-8")
    for label, probability in cal._DEFAULT_SCALE.items():
        assert f'"{label}": {probability}' in cockpit_source


def test_pred_lib_defensive_page_reference_and_date_edges(tmp_path):
    p = _load("pred_lib_edges", "pred_lib")
    assert p.read_fm(tmp_path / "missing.md") == {}
    plain = tmp_path / "plain.md"
    plain.write_text("body")
    assert p.read_fm(plain) == {}
    plain.write_text("---\n[broken\n---\n")
    assert p.read_fm(plain) == {}
    plain.write_text("---\n- list\n---\n")
    assert p.read_fm(plain) == {}
    assert list(p.iter_pages(tmp_path, "predictions")) == []

    pred_dir = tmp_path / "wiki/predictions"
    pred_dir.mkdir(parents=True)
    for name in ("_hidden.md", ".dot.md", "INDEX.md", "item.bak.md"):
        (pred_dir / name).write_text("---\ntype: prediction\n---\n")
    (pred_dir / "other.md").write_text("---\ntype: note\n---\n")
    (pred_dir / "live.md").write_text("---\ntype: prediction\nstatus: open\n---\n")
    assert [path.name for path, _fm in p.predictions(tmp_path)] == ["live.md"]
    assert p.fm_date({}, "missing") == ""
    assert p._date_from_path(tmp_path / "sources/undated.md") == ""
    assert p.is_resolved({"status": "confirmed"})

    sources = tmp_path / "wiki/sources/2026/08/04"
    sources.mkdir(parents=True)
    (sources / "path-dated.md").write_text("---\ntype: source\n---\n")
    (sources / "old.md").write_text("---\ntype: source\ncreated: 2020-01-01\n---\n")
    assert p.recent_source_slugs(tmp_path, "2026-08-01") == {"path-dated"}
    assert p.entity_source_slugs({
        "sources": "sources/a.md", "related": ["entities/x", "sources/b.md", ""],
    }) == {"a", "b"}
    assert p.subject_slugs({"subject": [None, "", "///", "[[entities/a/Alpha|A]]", "Bare"]}) == {
        "alpha", "bare",
    }


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
    assert "Brier: **0.3125**" in dash


def test_calibration_portfolio_watch_bias_and_history(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    pr.mkdir(parents=True)

    def write(slug, fields):
        (pr / f"{slug}.md").write_text(
            "---\n" + yaml.safe_dump({"type": "prediction", **fields}, sort_keys=False) +
            "---\n# prediction\n", encoding="utf-8")

    write("stale-near-due", {
        "status": "open", "subject": "[[entities/acme]]", "confidence": 0.6,
        "made_on": "2026-01-01", "resolves_by": "2026-08-01", "horizon": "long",
        "basis": ["[[sources/reinforcing-source]]"],
        "evidence": [{"date": "2026-02-01", "direction": "reinforces"}],
    })
    write("recent-hit", {
        "status": "confirmed", "subject": "[[entities/acme]]", "confidence": 0.8,
        "made_on": "2026-01-01", "resolves_by": "2026-07-01", "updated": "2026-07-10",
        "horizon": "medium", "basis": ["[[sources/reinforcing-source]]"],
        "evidence": [{"date": "2026-06-01", "direction": "reinforces"}],
    })
    write("recent-miss", {
        "status": "refuted", "subject": "[[entities/beta]]", "confidence": 0.8,
        "made_on": "2026-01-01", "resolves_by": "2026-07-01", "updated": "2026-07-11",
        "horizon": "medium", "basis": ["[[sources/contrary-source]]"],
        "evidence": [{"date": "2026-06-02", "direction": "contradicts"},
                     {"date": "2026-06-03", "direction": "bespoke-drift"}],
    })

    src = tmp_path / "wiki" / "sources"
    src.mkdir(parents=True)
    (src / "reinforcing-source.md").write_text(
        "---\ntype: source\nsignal_class: leading\n---\n# source\n", encoding="utf-8")
    (src / "contrary-source.md").write_text(
        "---\ntype: source\nsignal_class: lagging\n---\n# source\n", encoding="utf-8")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "competitive-watchlist.yaml").write_text(
        "segments:\n  market:\n    competitors: [acme, missing-co]\n", encoding="utf-8")

    state = tmp_path / "data"
    history_path = state / "state" / "okengine.predictions" / "calibration-history.jsonl"
    history_path.parent.mkdir(parents=True)
    history_path.write_text(
        '{"date":"2026-07-14","resolved":1,"brier":0.3}\n', encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("HERMES_DATA", str(state))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-15")

    cal = _load("calibration_refresh")
    assert cal.main() == 0
    first = (tmp_path / "wiki" / "dashboards" / "calibration.md").read_text()
    assert "## Near-due unresolved (1)" in first
    assert "## Recent resolutions (2 in 30d)" in first
    assert "## High-confidence misses (1)" in first
    assert "2 positive / 1 negative" in first
    assert "unknown/unmapped: **1**" in first and "`bespoke-drift`×1" in first
    assert "### Calibration by horizon" in first
    assert "### Calibration by dominant basis signal class" in first
    assert "Watchlist gaps: **1 / 2**" in first and "[[entities/missing-co]]" in first
    assert "Window delta:" in first

    # Daily history and the dashboard are idempotent for a fixed effective date.
    assert cal.main() == 0
    assert (tmp_path / "wiki" / "dashboards" / "calibration.md").read_text() == first
    history = [_json.loads(line) for line in history_path.read_text().splitlines()]
    assert [row["date"] for row in history] == ["2026-07-14", "2026-07-15"]


def test_date_audit_flags(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred(pr, "no-date", "open")                                  # missing
    _pred(pr, "overdue", "open", resolves_by="2020-01-01")        # open + overdue
    _pred(pr, "fine", "open", resolves_by="2099-01-01")           # > horizon (~5y) -> flagged too
    _pred(pr, "healthy-closed", "confirmed", resolves_by="2026-08-04")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    da = _load("prediction_date_audit")
    assert da.main() == 0
    dash = (tmp_path / "wiki" / "dashboards" / "prediction-date-audit.md").read_text()
    assert "missing/unparseable" in dash and "overdue" in dash
    assert da._parse("2026-99-99") is None
    monkeypatch.setattr(da.P, "predictions", lambda vault: [])
    assert da.main() == 0
    assert "flagged: **0**" in (tmp_path / "wiki/dashboards/prediction-date-audit.md").read_text()


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


def test_schema_drain_derives_horizon_from_dates_not_judgment():  # okengine#326 [24]
    """The remediation drain must DERIVE the horizon bucket from (resolves_by - made_on) via the
    sibling audit's `_horizon_for`, not hand the agent a judgment call. Covers missing, drifted, and
    canonical-but-wrong horizons; falls back to a fuzzy hint only when the dates are unavailable."""
    d = _load("select_prediction_schema_drain")
    base = {"made_on": "2026-01-01", "resolves_by": "2026-02-01",   # 31 days -> short
            "confidence": "0.6", "status": "open", "subject": "x"}
    issues, _ = d.classify(dict(base), "# body\n")                  # horizon missing but computable
    assert any("horizon missing -> 'short' (from dates)" in i for i in issues), issues
    issues, _ = d.classify({**base, "horizon": "medium"}, "# body\n")   # canonical BUT wrong per dates
    assert any("horizon 'medium' -> 'short' (from dates)" in i for i in issues), issues
    issues, _ = d.classify({**base, "horizon": "short"}, "# body\n")    # correct -> no horizon issue
    assert not any("horizon" in i for i in issues), issues
    issues, _ = d.classify({"confidence": "0.6", "status": "open", "subject": "x",
                            "horizon": "medium-term"}, "# body\n")      # no dates -> fuzzy hint fallback
    assert any("horizon drift: 'medium-term' -> 'medium'" in i for i in issues), issues


def test_schema_drain_defensive_confidence_body_and_drift_edges(tmp_path):
    d = _load("select_prediction_schema_drain_edges", "select_prediction_schema_drain")
    assert not d._confidence_valid(None)
    assert not d._confidence_valid("not-a-confidence")
    assert d._body(tmp_path / "missing.md") == ""
    issues, is_batch = d.classify(
        {"status": "bespoke", "subject": "x", "confidence": "bad"}, "# body\n"
    )
    assert not is_batch
    assert "status drift: 'bespoke' (read body)" in issues
    assert "missing horizon (no made_on/resolves_by to derive it)" in issues
    assert "unparseable confidence: 'bad'" in issues
    issues, _ = d.classify(
        {"status": "resolved", "subject": "x", "confidence": "high", "horizon": "unknown"},
        "# body\n",
    )
    assert "status drift: 'resolved' -> 'confirmed'" in issues
    assert "horizon drift: 'unknown'" in issues
    issues, _ = d.classify(
        {"status": "open", "subject": "x", "confidence": "high", "horizon": "short"},
        "# body\n",
    )
    assert not any("horizon" in issue for issue in issues)


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


def test_schema_audit_helper_boundaries_and_clean_dashboard(tmp_path, monkeypatch):
    sa = _load("prediction_schema_audit")
    from datetime import date
    assert sa._parse_date(None) is None
    assert sa._parse_date("bad") is None
    assert sa._parse_date("2026-02-31") is None
    assert sa._parse_date("2026-02-28T12:00:00Z") == date(2026, 2, 28)
    assert [sa._horizon_for(days) for days in (90, 91, 365, 366, 1825, 1826)] == [
        "short", "medium", "medium", "long", "long", "strategic"
    ]
    assert not sa._confidence_valid(None)
    assert sa._confidence_valid("VERY-HIGH")
    assert sa._confidence_valid("75%") and sa._confidence_valid("0.25")
    assert not sa._confidence_valid("101") and not sa._confidence_valid("unknown")
    assert not sa._has_refutation_section(tmp_path / "missing.md")

    _pred_full(
        tmp_path / "wiki/predictions", "clean", status="open", subject="x", confidence="medium",
        made_on="2026-01-01", resolves_by="2026-02-01", horizon="short",
    )
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    clean = _load("prediction_schema_audit")
    assert clean.main() == 0
    assert "_No schema issues found._" in (
        tmp_path / "wiki/dashboards/prediction-schema-audit.md"
    ).read_text()


def test_schema_audit_flags_missing_subject_confidence_and_horizon(tmp_path, monkeypatch):
    pr = tmp_path / "wiki/predictions"
    _pred_full(pr, "missing", status="open", made_on="2026-01-01", resolves_by="2027-01-02")
    _pred_full(pr, "bad-confidence", status="open", subject="x", confidence="many",
               made_on="2026-01-01", resolves_by="2032-01-01", horizon="strategic")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    module = _load("prediction_schema_audit")
    assert module.main() == 0
    dash = (tmp_path / "wiki/dashboards/prediction-schema-audit.md").read_text()
    assert "missing subject" in dash and "missing confidence" in dash
    assert "missing horizon (should be 'long')" in dash
    assert "unparseable confidence='many'" in dash


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
    # #326 [24]: the dates are present, so the drain surfaces the DETERMINISTIC correction
    # (medium-term -> short, 31 days) rather than a fuzzy string hint.
    assert "medium-term" in out and "'short' (from dates)" in out

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


def test_structural_backfill_read_race_and_controlled_target(tmp_path, monkeypatch):
    pr = tmp_path / "wiki/predictions"
    _pred(pr, "target", "open", "0.6", "2026-09-01")
    _pred(pr, "other", "open", "0.6", "2026-10-01")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    mod = _load("select_prediction_structural_backfill_edges",
                "select_prediction_structural_backfill")
    assert mod._has_refutation(tmp_path / "missing.md") is False
    mod.CONTROLLED_TARGET = "wiki/predictions/target.md"
    mod.SELECTION_MANIFEST = tmp_path / "selection.json"
    assert mod.main() == 0
    output = (tmp_path / "selection.json").read_text()
    assert "predictions/target.md" in output and "predictions/other.md" not in output


def test_forecast_review_gate(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred(pr, "old-open", "open", "0.6", "2099-01-01")   # not touched recently -> no wake
    assert _run("select_forecast_review", tmp_path, monkeypatch) is False
    from datetime import date
    _pred_full(pr, "resolved-this-week", status="confirmed", subject="x", confidence="0.8",
               updated=date.today().isoformat())
    assert _run("select_forecast_review", tmp_path, monkeypatch) is True


def test_forecast_review_surfaces_open_reevaluation_and_dashboard_context(tmp_path, monkeypatch, capsys):
    from datetime import date
    pr = tmp_path / "wiki/predictions"
    _pred_full(pr, "open-touched", status="open", subject="Market", confidence="0.7",
               updated=date.today().isoformat())
    _pred_full(pr, "old-resolved", status="confirmed", subject="Old", confidence="0.8",
               updated="2000-01-01")
    _pred_full(pr, "ignored-status", status="draft", subject="Draft", confidence="0.5",
               updated=date.today().isoformat())
    dashboards = tmp_path / "wiki/dashboards"
    dashboards.mkdir(parents=True)
    for name in ("calibration.md", "prediction-date-audit.md", "prediction-schema-audit.md"):
        (dashboards / name).write_text(f"context from {name}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    module = _load("select_forecast_review")
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "0 resolved, 1 re-evaluated" in output
    assert "[[predictions/open-touched]]" in output
    assert "context from calibration.md" in output
    assert "old-resolved" not in output
    assert '"wakeAgent": true' in output


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


def test_base_rates_reports_event_coverage_without_overall_row(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    module = _load("select_base_rates")
    monkeypatch.setattr(module, "compute_base_rates", lambda _vault: ([{
        "rate_kind": "event-coverage", "class_label": "incident", "value": 0.25,
        "n_observations": 4,
    }], tmp_path / "state.json", tmp_path / "dashboard.md"))
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "incident=25.0% (N=4)" in output
    assert "overall resolved hit rate" not in output


def test_falsification_gate(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    _pred(pr, "hi-open", "open", "high", "2099-01-01")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    helper = _load("select_falsification")
    assert helper._is_high({"confidence": "60%"})
    assert helper._is_high({"confidence": "0.6"})
    assert not helper._is_high({"confidence": "0.59"})
    assert not helper._is_high({"confidence": "unknown"})
    assert _run("select_falsification", tmp_path, monkeypatch) is False   # no recent sources
    src = tmp_path / "wiki" / "sources" / "2026" / "06"
    src.mkdir(parents=True)
    from datetime import date
    (src / "undated.md").write_text("---\ntype: source\n---\n# old\n")
    (src / "old.md").write_text("---\ntype: source\npublished: 2000-01-01\n---\n# old\n")
    (src / "s.md").write_text(f"---\ntype: source\npublished: {date.today().isoformat()}\n---\n# s\n")
    assert _run("select_falsification", tmp_path, monkeypatch) is True


def test_output_outcome_gate(tmp_path, monkeypatch):
    pr = tmp_path / "wiki" / "predictions"
    from datetime import date
    for i in range(3):
        _pred(pr, f"o{i}", "confirmed", "high", date.today().isoformat())
    _pred(pr, "not-graded", "open", "high", date.today().isoformat())
    _pred(pr, "graded-old", "confirmed", "high", "2000-01-01")
    _pred(pr, "graded-undated", "confirmed", "high", None)
    assert _run("select_output_outcome", tmp_path, monkeypatch) is False  # no briefings
    b = tmp_path / "wiki" / "briefings"; b.mkdir(parents=True)
    (b / "2026-06-28.md").write_text("---\ntype: dashboard\ntitle: brief\n---\n# brief\n")
    assert _run("select_output_outcome", tmp_path, monkeypatch) is True


def test_numeric_base_rate_families_and_output_outcome_joins(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("HERMES_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-15")
    predictions = tmp_path / "wiki" / "predictions"
    sources = tmp_path / "wiki" / "sources"
    briefings = tmp_path / "wiki" / "briefings"
    predictions.mkdir(parents=True)
    sources.mkdir(parents=True)
    briefings.mkdir(parents=True)

    def write_prediction(slug, status, subject, basis, horizon, made_on="2026-07-02"):
        data = {"type": "prediction", "status": status, "subject": f"[[entities/{subject}]]",
                "basis": [f"[[sources/{basis}]]"], "horizon": horizon,
                "confidence": 0.7, "made_on": made_on, "resolves_by": "2026-07-14"}
        (predictions / f"{slug}.md").write_text(
            "---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n# prediction\n")

    for slug, signal_class in (("s1", "leading"), ("s2", "lagging"), ("s3", "leading")):
        (sources / f"{slug}.md").write_text(
            f"---\ntype: source\nsignal_class: {signal_class}\npublisher: Pub\n---\n# source\n")
    write_prediction("p0", "confirmed", "acme", "s1", "short")
    write_prediction("p1", "confirmed", "acme", "s1", "short")
    write_prediction("p2", "refuted", "beta", "s2", "medium")
    write_prediction("p3", "confirmed", "gamma", "s3", "long")
    write_prediction("p4", "confirmed", "acme", "s1", "short")

    event_rows = [
        {"event_id": "events/e1", "event_type": "capital", "entity": "acme",
         "date": "2026-07-10", "source": "sources/s1", "publisher": "P1",
         "scores": {"materiality": .8, "signal_strength": .9, "watchlist_relevance": 1}},
        {"event_id": "events/e2", "event_type": "capital", "entity": "beta",
         "date": "2026-07-09", "source": "sources/s2", "publisher": "P1",
         "scores": {"materiality": .4, "signal_strength": .5, "watchlist_relevance": 0}},
        {"event_id": "events/e3", "event_type": "launch", "entity": "gamma",
         "date": "2026-07-08", "source": "sources/s3", "publisher": "P2",
         "scores": {"materiality": .6, "signal_strength": .7, "watchlist_relevance": 0}},
        {"event_id": "events/e4", "event_type": "launch", "entity": "uncovered",
         "date": "2026-07-07", "source": "sources/s4", "publisher": "P2",
         "scores": {"materiality": .2, "signal_strength": .3, "watchlist_relevance": 0}},
        {"event_id": "sources/s1", "event_type": "source-evidence", "entity": "",
         "date": "2026-07-10", "source": "sources/s1", "publisher": "Pub",
         "score_scope": "source",
         "scores": {"materiality": .9, "signal_strength": .9, "watchlist_relevance": 0}},
    ]
    event_path = tmp_path / "data" / "state" / "okengine.events" / "event-scores.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text("".join(_json.dumps(row) + "\n" for row in event_rows))
    (briefings / "2026-07-01-watch.md").write_text(
        "---\ntype: briefing\ndate: 2026-07-01\n---\n"
        "Watch [[sources/s1]] and [[entities/acme]].\n")

    metrics = _load("numeric_metrics")
    rows, state_path, dashboard_path = metrics.compute_base_rates(tmp_path)
    lookup = {(row["rate_kind"], row["class_label"]): row for row in rows}
    assert lookup[("event-frequency", "capital")]["n_observations"] == 2
    assert ("event-frequency", "source-evidence") not in lookup
    assert lookup[("event-frequency", "capital")]["materiality_p50"] == .6
    assert lookup[("event-coverage", "launch")]["value"] == .5
    assert lookup[("entity-frequency", "entities/acme")]["on_watchlist"] is True
    overall = lookup[("outcome-rate", "(all-resolved)")]
    assert overall["value"] == .8 and overall["n_observations"] == 5
    assert overall["small_n"] is False
    assert lookup[("outcome-rate", "horizon=short")]["small_n"] is True
    assert ("outcome-rate", "basis-signal-class=leading") in lookup
    assert ("outcome-rate", "basis-event-type=capital") in lookup
    assert lookup[("publisher-mix", "P1")]["sole_basis_fraction"] == 1.0
    assert state_path.is_file() and "## C. Event coverage" in dashboard_path.read_text()

    outcome_rows, outcome_state, outcome_dash = metrics.compute_output_outcomes(tmp_path)
    outcomes = {row["metric_label"]: row for row in outcome_rows}
    assert outcomes["briefing_source_to_prediction_basis_yield"]["value"] == 1.0
    assert outcomes["briefing_entity_subsequent_material_event_yield"]["value"] == 1.0
    assert outcomes["high_materiality_event_briefing_coverage_miss_rate"]["value"] == .5
    assert outcome_state.is_file() and "50.0%" in outcome_dash.read_text()

    first_rates, first_outcomes = state_path.read_text(), outcome_state.read_text()
    metrics.compute_base_rates(tmp_path)
    metrics.compute_output_outcomes(tmp_path)
    assert state_path.read_text() == first_rates
    assert outcome_state.read_text() == first_outcomes


def test_numeric_base_rates_family_d_without_event_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("HERMES_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-15")
    _pred(tmp_path / "wiki" / "predictions", "hit", "confirmed", "high", "2026-07-01")
    rows, _, dashboard = _load("numeric_metrics").compute_base_rates(tmp_path)
    assert any(row["rate_kind"] == "outcome-rate" for row in rows)
    assert not any(row["rate_kind"] == "event-frequency" for row in rows)
    assert "families A–C and E are empty" in dashboard.read_text()


def test_numeric_metrics_malformed_and_empty_boundaries(tmp_path):
    metrics = _load("numeric_metrics")
    jsonl = tmp_path / "rows.jsonl"
    jsonl.write_text('{"ok":1}\nnot-json\n[]\n{"again":2}\n')
    assert metrics._jsonl(jsonl) == [{"ok": 1}, {"again": 2}]
    assert metrics._date(datetime(2026, 7, 15, 12)) == date(2026, 7, 15)
    assert metrics._date("not-a-date") is None
    assert metrics._refs(["[[sources/example.md]]", "", "plain.md"]) == [
        "sources/example", "plain"
    ]
    assert metrics._quantile([], .5) is None

    missing = tmp_path / "missing.md"
    assert metrics._page(missing) == ({}, "")
    plain = tmp_path / "plain.md"
    plain.write_text("body only")
    assert metrics._page(plain) == ({}, "body only")
    malformed = tmp_path / "malformed.md"
    malformed.write_text("---\nkey: [\n---\nbody")
    assert metrics._page(malformed) == ({}, "body")

    rows = metrics.base_rate_rows(
        [{"date": "2026-07-15", "event_type": "x", "entity": "e"}],
        [{"outcome": None, "basis": [], "subject": "", "horizon": "short"}],
        {}, date(2026, 7, 15),
    )
    assert not any(row["rate_kind"] == "outcome-rate" for row in rows)


def test_numeric_output_outcomes_ignores_unusable_briefings(tmp_path, monkeypatch):
    metrics = _load("numeric_metrics")
    briefings = tmp_path / "wiki" / "briefings"
    briefings.mkdir(parents=True)
    (briefings / "undated.md").write_text("---\ntype: briefing\n---\n[[sources/no-yield]]")
    (briefings / "2026-07-01.md").write_text(
        "---\ntype: briefing\ncreated: 2026-07-01\n---\n"
        "[[sources/no-yield]] [[entities/no-yield]]"
    )
    monkeypatch.setattr(metrics.P, "iter_pages", lambda *_args: iter(sorted(briefings.glob("*.md"))))
    rows = metrics.output_outcome_rows(
        tmp_path,
        [{"entity": "other", "date": "2026-07-02", "scores": {"materiality": .1}}],
        [{"basis": [], "made_on": None}],
        date(2026, 7, 15),
    )
    assert all(row["value"] == 0 for row in rows)


def test_calibration_helpers_cover_malformed_and_empty_boundaries(tmp_path, monkeypatch):
    cal = _load("calibration_refresh")
    monkeypatch.setenv("PREDICTION_CONFIDENCE_SCALE", '{"custom": 0.42}')
    assert cal._scale() == {"custom": .42}
    monkeypatch.setenv("PREDICTION_CONFIDENCE_SCALE", "not-json")
    assert cal._scale() == cal._DEFAULT_SCALE
    monkeypatch.setenv("PREDICTION_CONFIDENCE_SCALE", "[]")
    assert cal._scale() == cal._DEFAULT_SCALE
    assert cal.confidence_prob(None, {}) is None
    assert cal._date(datetime(2026, 7, 15, 12)) == date(2026, 7, 15)
    assert cal._date(date(2026, 7, 15)) == date(2026, 7, 15)
    assert cal._date("invalid") is None
    assert cal._refs(["[[sources/a.md]]", "", "plain.md"]) == ["sources/a", "plain"]
    assert cal._latest_evidence({"evidence": ["legacy", {"date": "invalid"}]}) is None
    assert cal.calibration([]) == {"n": 0, "brier": None, "base_rate": None, "bands": []}
    source_a, source_b = tmp_path / "a.md", tmp_path / "b.md"
    monkeypatch.setattr(cal.P, "iter_pages", lambda *_args: iter((source_a, source_b)))
    monkeypatch.setattr(cal.P, "read_fm", lambda path: {
        "signal_class": None if path == source_a else " leading "
    })
    assert cal._source_classes(tmp_path) == {"b": "leading"}

    rows = [{"status": "open", "evidence": [None, {}, {"direction": "neutral"}],
             "outcome": None, "confidence": None}]
    bias = cal.direction_bias(rows)
    assert bias["neutral"] == 1 and bias["positive"] == bias["negative"] == 0

    today = date(2026, 7, 15)
    row = {"status": "open", "made_on": today, "resolves_by": today,
           "latest_evidence": None, "updated": None, "rel": "predictions/x"}
    assert cal.near_due([row], today) == []
    row["made_on"] = today.replace(day=1)
    row["resolves_by"] = date(2026, 7, 16)
    row["updated"] = today
    assert cal.near_due([row], today) == []

    watch = tmp_path / "watch.yaml"
    watch.write_text("segments: [\n")
    monkeypatch.setenv("WATCHLIST_PATH", str(watch))
    assert cal._watchlist(tmp_path) == set()
    watch.write_text("segments:\n  ignored: string\n  valid:\n    competitors: [entities/acme]\n")
    assert cal._watchlist(tmp_path) == {"acme"}

    history = tmp_path / "history.jsonl"
    history.write_text('not-json\n[]\n{"date":"2026-07-14","brier":0.2}\n')
    result = cal.update_history(history, {"date": "2026-07-15", "brier": None})
    assert [row["date"] for row in result] == ["2026-07-14", "2026-07-15"]

    rendered = cal.render([], today, [], {}, set())
    assert "No resolved, scored predictions yet" in rendered
