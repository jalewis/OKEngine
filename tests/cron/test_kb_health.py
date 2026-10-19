"""kb_health: the vault-health aggregator's parsing, thresholds, trend verdicts and history ledger.

The parsers and renderer are pure; the snapshot readers, namespace discovery and ``main`` run
in-process against disposable vaults with the module's path constants pointed at them.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "kb_health.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("kb_health_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


K = _load_module()
D1 = date(2026, 9, 1)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "wiki" / "operational").mkdir(parents=True)
    (root / "schema.yaml").write_text("types:\n  entity: {required: [type]}\n", encoding="utf-8")
    op = root / "wiki" / "operational"
    monkeypatch.setattr(K, "VAULT", root)
    monkeypatch.setattr(K, "OP_DIR", op)
    monkeypatch.setattr(K, "DASH_PATH", root / "wiki" / "dashboards" / "kb-health.md")
    monkeypatch.setattr(K, "HISTORY_PATH", op / "kb-health-snapshots.md")
    monkeypatch.setattr(K, "QUEUE_SNAPSHOTS", op / "queue-snapshots.md")
    return root


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- _parse_date --------------------------------------------------------------------------------

def test_parse_date_accepts_a_date():
    assert K._parse_date(D1) == D1


def test_parse_date_truncates_a_datetime():
    assert K._parse_date(datetime(2026, 9, 1, 13, 5)) == D1


def test_parse_date_finds_a_date_inside_a_string():
    assert K._parse_date("snapshot 2026-09-01 (am)") == D1


def test_parse_date_rejects_an_impossible_calendar_date():
    assert K._parse_date("2026-13-45") is None


def test_parse_date_rejects_a_string_without_a_date():
    assert K._parse_date("yesterday") is None


def test_parse_date_rejects_a_non_date_value():
    assert K._parse_date(20260901) is None


# --- parse_queue_snapshots ----------------------------------------------------------------------

def test_queue_rows_are_parsed_and_sorted_by_date():
    text = ("# header\n| date | queues |\n|---|---|\n"
            "| 2026-09-02 | orphans=5, broken-wikilinks=7 |\n"
            "| 2026-09-01 | orphans=9 |\n")

    rows = K.parse_queue_snapshots(text)

    assert rows == [(D1, {"orphans": 9}), (date(2026, 9, 2), {"orphans": 5, "broken-wikilinks": 7})]


def test_malformed_queue_rows_and_pairs_are_dropped():
    text = ("|only-one-col|\n"
            "| not-a-date | orphans=1 |\n"
            "| 2026-09-01 | orphans=many, flag, schema-drift=3 |\n"
            "| 2026-09-02 | nothing parseable |\n")

    assert K.parse_queue_snapshots(text) == [(D1, {"schema-drift": 3})]


# --- snapshot readers ---------------------------------------------------------------------------

def test_field_loss_is_unknown_without_its_snapshot(vault):
    assert K._latest_field_loss() is None


def test_field_loss_reads_the_last_numeric_row(vault):
    _write(vault / "wiki" / "operational" / "field-loss-snapshots.md",
           "| date | losses |\n|---|---|\n| 2026-09-01 | 4 |\n| 2026-09-02 | n/a |\n"
           "| 2026-09-03 | 2 |\n| 2026-09-04 |\n")

    assert K._latest_field_loss() == 2


def test_page_quality_is_unknown_without_its_snapshot(vault):
    assert K._latest_page_quality() is None


def test_page_quality_is_unknown_without_a_complete_row(vault):
    _write(vault / "wiki" / "operational" / "page-quality-snapshots.md",
           "| date | deficient | x | stub% |\n| 2026-09-01 | n/a | 1 | 2 |\n| 2026-09-02 | 3 |\n")

    assert K._latest_page_quality() is None


def test_page_quality_reads_deficient_count_and_stub_percentage(vault):
    _write(vault / "wiki" / "operational" / "page-quality-snapshots.md",
           "| 2026-09-01 | 10 | x | 30.0 |\n| 2026-09-02 | 12 | x | 18.5 |\n")

    assert K._latest_page_quality() == (12, 18.5)


def test_page_quality_with_a_non_numeric_percentage_is_unknown(vault):
    _write(vault / "wiki" / "operational" / "page-quality-snapshots.md", "| 2026-09-02 | 12 | x | high |\n")

    assert K._latest_page_quality() is None


# --- namespaces and page totals -----------------------------------------------------------------

def test_declared_namespaces_exclude_operational_and_dashboards(vault):
    _write(vault / "schema.yaml", "partitioning:\n  namespaces:\n    entities: {strategy: flat}\n"
                                  "    dashboards: {strategy: flat}\n")

    names = K.knowledge_namespaces()

    assert "entities" in names
    assert "dashboards" not in names and "operational" not in names


def test_namespaces_fall_back_to_on_disk_dirs_when_none_are_declared(vault):
    _write(vault / ".okengine" / "composed-schema.yaml", "types:\n  entity: {required: [type]}\n")
    for d in ("widgets", "_scratch", ".hidden", "dashboards"):
        (vault / "wiki" / d).mkdir()
    _write(vault / "wiki" / "loose.md", "x\n")

    assert K.knowledge_namespaces() == ["widgets"]


def test_no_namespaces_and_no_wiki_dir_yields_none(tmp_path, monkeypatch):
    root = tmp_path / "bare"
    _write(root / ".okengine" / "composed-schema.yaml", "types: {}\n")
    monkeypatch.setattr(K, "VAULT", root)

    assert K.knowledge_namespaces() == []


def test_total_pages_counts_markdown_in_existing_namespaces_only(vault):
    _write(vault / "schema.yaml", "partitioning:\n  namespaces:\n    widgets: {strategy: flat}\n"
                                  "    gadgets: {strategy: flat}\n")
    _write(vault / "wiki" / "widgets" / "a.md", "x")
    _write(vault / "wiki" / "widgets" / "deep" / "b.md", "x")
    _write(vault / "wiki" / "widgets" / "c.txt", "x")

    assert K.total_pages() == 2


# --- status and trend ---------------------------------------------------------------------------

@pytest.mark.parametrize(("ok", "warn", "icon"), [(True, False, "🟢"), (False, True, "🟡"), (False, False, "🔴")])
def test_status_icon(ok, warn, icon):
    assert K._status(ok, warn) == icon


def test_trend_without_a_snapshot_has_no_data():
    assert K._trend_row("orphans", None, None) == "| orphans | — | — | — | no data |"


def test_trend_for_an_untracked_queue():
    assert K._trend_row("orphans", (D1, {"x": 1}), None).endswith("| not tracked |")


def test_trend_without_a_prior_row():
    assert K._trend_row("orphans", (D1, {"orphans": 4}), None) == "| orphans | 4 | — | — | (no prior row) |"


def test_trend_when_the_prior_row_lacks_the_queue():
    assert K._trend_row("orphans", (D1, {"orphans": 4}), (date(2026, 8, 25), {})).endswith("(no prior row) |")


def test_trend_when_the_prior_row_is_the_same_day():
    assert K._trend_row("orphans", (D1, {"orphans": 4}), (D1, {"orphans": 9})).endswith("(no prior row) |")


def test_trend_verdict_draining():
    row = K._trend_row("orphans", (date(2026, 9, 8), {"orphans": 30}), (D1, {"orphans": 100}))

    assert row == "| orphans | 30 | 100 (2026-09-01) | -10.0 | 🟢 draining (-10.0/day) |"


def test_trend_verdict_flat():
    row = K._trend_row("orphans", (date(2026, 9, 8), {"orphans": 30}), (D1, {"orphans": 30}))

    assert row.endswith("| 🟡 ~flat (+0.0/day) |")


def test_trend_verdict_growing():
    row = K._trend_row("orphans", (date(2026, 9, 8), {"orphans": 60}), (D1, {"orphans": 32}))

    assert row.endswith("| 🔴 GROWING (+4.0/day) |")


def test_trend_on_the_flat_boundary():
    row = K._trend_row("orphans", (date(2026, 9, 3), {"orphans": 11}), (D1, {"orphans": 10}))

    assert "🟡 ~flat (+0.5/day)" in row


# --- render -------------------------------------------------------------------------------------

def test_render_with_no_snapshots_shows_placeholders(vault):
    page = K.render(D1, [], 0)

    assert "Snapshot source date: **n/a** · total vault pages: **0**" in page
    assert "| Frontmatter-valid | 0.00% (0 unparseable / 0) | ≥99.5% | 🔴 |" in page
    assert "| Curated-field losses (24h) | _no data yet_ | 0 | — |" in page
    assert "Page quality" not in page
    assert "Other queue depths" not in page
    assert "| orphans | — | — | — | no data |" in page


def test_render_compares_against_the_row_closest_to_a_week_back(vault):
    rows = [(date(2026, 8, 1), {"orphans": 50}),
            (date(2026, 8, 20), {"orphans": 40}),
            (date(2026, 8, 25), {"orphans": 35}),
            (date(2026, 8, 31), {"orphans": 31}),
            (date(2026, 9, 1), {"orphans": 30}),
            (date(2026, 9, 1), {"orphans": 30, "schema-drift": 2})]

    page = K.render(D1, rows, 1000)

    assert "| orphans | 30 | 35 (2026-08-25) | -0.7 |" in page


def test_render_reports_healthy_topline_and_other_queues(vault):
    _write(vault / "wiki" / "operational" / "field-loss-snapshots.md", "| 2026-09-01 | 0 |\n")
    _write(vault / "wiki" / "operational" / "page-quality-snapshots.md", "| 2026-09-01 | 40 | x | 12 |\n")
    rows = [(D1, {"fm-parse-errors": 0, "yaml-invalid": 0, "reference-pages": 1200, "stale": 3})]

    page = K.render(D1, rows, 2000)

    assert "Reference-catalog pages: **1,200**" in page
    assert "| Frontmatter-valid | 100.00% (0 unparseable / 2,000) | ≥99.5% | 🟢 |" in page
    assert "| YAML-invalid pages | 0 | 0 | 🟢 |" in page
    assert "| Curated-field losses (24h) | 0 | 0 | 🟢 |" in page
    assert "| Page quality (entity stub%) | 40 deficient · 12% entity stubs | ≤15% | 🟢 |" in page
    assert "- `stale`: 3" in page
    assert "- `yaml-invalid`: 0" in page
    assert "- `fm-parse-errors`" not in page
    assert "- `orphans`" not in page


def test_render_flags_degraded_topline_metrics(vault):
    _write(vault / "wiki" / "operational" / "field-loss-snapshots.md", "| 2026-09-01 | 3 |\n")
    _write(vault / "wiki" / "operational" / "page-quality-snapshots.md", "| 2026-09-01 | 40 | x | 30 |\n")
    rows = [(D1, {"fm-parse-errors": 20, "yaml-invalid": 9, "orphans": 1})]

    page = K.render(D1, rows, 1000)

    assert "| Frontmatter-valid | 98.00% (20 unparseable / 1,000) | ≥99.5% | 🔴 |" in page
    assert "| YAML-invalid pages | 9 | 0 | 🔴 |" in page
    assert "| Curated-field losses (24h) | 3 | 0 | 🔴 |" in page
    assert "30% entity stubs | ≤15% | 🔴 |" in page
    assert "- `orphans`" not in page


# --- history ledger and main --------------------------------------------------------------------

def test_history_is_created_then_replaced_per_day(vault):
    rows = [(D1, {"broken-wikilinks": 4, "orphans": 2, "schema-drift": 1, "fm-parse-errors": 1})]
    K.append_history(D1, rows, 100)
    K.append_history(D1, rows, 200)
    K.append_history(date(2026, 9, 2), [], 0)

    history = (vault / "wiki" / "operational" / "kb-health-snapshots.md").read_text()

    assert "| 2026-09-01 | 100.0 |" not in history
    assert "| 2026-09-01 | 99.5 | 4 | 2 | 1 |" in history
    assert "| 2026-09-02 | 0.0 |  |  |  |" in history
    assert history.startswith("---\n")


def test_main_renders_the_dashboard_and_never_wakes(vault, capsys):
    _write(vault / "wiki" / "operational" / "queue-snapshots.md", "| 2026-09-01 | orphans=3 |\n")

    rc = K.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "queue snapshot: 2026-09-01" in out
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}
    assert "# KB Health" in (vault / "wiki" / "dashboards" / "kb-health.md").read_text()


def test_main_without_a_queue_snapshot_still_renders(vault, capsys):
    rc = K.main()

    assert rc == 0
    assert "queue snapshot:" not in capsys.readouterr().out
    assert "**n/a**" in (vault / "wiki" / "dashboards" / "kb-health.md").read_text()
