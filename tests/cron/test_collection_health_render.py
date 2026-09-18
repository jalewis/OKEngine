"""The collection-health dashboard's origin-class tally.

`collection_health` had no test of its own. The mutation gate said so plainly the moment the
module was first registered: score 0.00%, with every mutant of the tally comparison surviving —
`==` could have become `!=`, `<` or `is not` and no test would have noticed the dashboard
reporting the wrong number of primary sources.

The tally is also the one place the ledger's `origin_class` reaches a human, so it is where a
half-finished rename would show up as a column of zeros (okengine#595).
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "collection_health.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="collection_health absent")

NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("collection_health", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["collection_health"] = mod
    spec.loader.exec_module(mod)
    return mod


def _ledger(tmp_path: Path, mod, sources: list[dict]) -> Path:
    ledger = tmp_path / "ledger"
    mod.collection_ledger.register_sources(ledger, sources)
    for source in sources:
        mod.collection_ledger.append_attempt(ledger, {
            "connector_id": source["connector_id"], "source_id": source["source_id"],
            "started_at": NOW - timedelta(minutes=1), "finished_at": NOW,
            "outcome": "success", "fetched": 1, "accepted": 1, "latency_ms": 10,
        })
    return ledger


def _render(tmp_path: Path, mod, sources: list[dict]) -> str:
    ledger = _ledger(tmp_path, mod, sources)
    vault = tmp_path / "vault"
    (vault / "wiki" / "dashboards").mkdir(parents=True, exist_ok=True)
    return mod.render(vault=vault, ledger=ledger, now=NOW).read_text(encoding="utf-8")


def test_the_rendered_tally_counts_each_origin_class_exactly(tmp_path):
    """Asserted on the RENDERED numbers, not on a re-implementation of the comprehension.

    Restating the tally in the test proves only that I can write the same expression twice.
    Every surviving mutant here — `==` becoming `!=`, `<=`, `>` — still produces a tally; it
    just produces the wrong one, and only the rendered figure can tell the difference. With
    `!=` the primary count becomes total-minus-primary, which is 2 for this fixture either way,
    so the counts are deliberately made distinct.
    """
    mod = _load()
    text = _render(tmp_path, mod, [
        {"source_id": "p1", "connector_id": "t", "label": "P1", "origin_class": "primary"},
        {"source_id": "p2", "connector_id": "t", "label": "P2", "origin_class": "primary"},
        {"source_id": "p3", "connector_id": "t", "label": "P3", "origin_class": "primary"},
        {"source_id": "s1", "connector_id": "t", "label": "S1", "origin_class": "secondary"},
        {"source_id": "u1", "connector_id": "t", "label": "U1"},
    ])
    assert "primary 3 · secondary 1 · unknown 1" in text, \
        next((line for line in text.splitlines() if "source class" in line), text[:400])


def test_the_rendered_tally_is_all_zero_when_no_source_carries_a_class(tmp_path):
    """A mutant that inverts the comparison reports 1 here instead of 0 for two of the three."""
    mod = _load()
    text = _render(tmp_path, mod, [
        {"source_id": "u1", "connector_id": "t", "label": "U1", "origin_class": "unknown"},
    ])
    assert "primary 0 · secondary 0 · unknown 1" in text


def test_the_tally_totals_every_source_exactly_once(tmp_path):
    """A comparison mutant that double-counts or drops a row breaks this sum."""
    mod = _load()
    sources = [
        {"source_id": f"p{i}", "connector_id": "t", "label": f"P{i}", "origin_class": "primary"}
        for i in range(3)
    ] + [
        {"source_id": "s1", "connector_id": "t", "label": "S1", "origin_class": "secondary"},
        {"source_id": "u1", "connector_id": "t", "label": "U1", "origin_class": "unknown"},
    ]
    ledger = _ledger(tmp_path, mod, sources)
    rows = mod.collection_ledger.project_current(
        mod.collection_ledger.load_sources(ledger),
        mod.collection_ledger.load_attempts(ledger, now=NOW), now=NOW)
    tally = {key: sum(mod.collection_ledger.origin_class_of(row) == key for row in rows)
             for key in ("primary", "secondary", "unknown")}
    assert tally == {"primary": 3, "secondary": 1, "unknown": 1}
    assert sum(tally.values()) == len(rows), "every source lands in exactly one bucket"


def test_a_legacy_row_is_tallied_under_its_migrated_class(tmp_path):
    """A v1 ledger must not report every source as `unknown` after the rename."""
    mod = _load()
    ledger = _ledger(tmp_path, mod, [
        {"source_id": "a", "connector_id": "t", "label": "A", "source_kind": "primary"},
    ])
    rows = mod.collection_ledger.project_current(
        mod.collection_ledger.load_sources(ledger),
        mod.collection_ledger.load_attempts(ledger, now=NOW), now=NOW)
    assert [mod.collection_ledger.origin_class_of(row) for row in rows] == ["primary"]


def test_the_rendered_table_shows_each_source_its_own_class(tmp_path):
    mod = _load()
    text = _render(tmp_path, mod, [
        {"source_id": "a", "connector_id": "t", "label": "Alpha", "origin_class": "primary"},
        {"source_id": "b", "connector_id": "t", "label": "Beta", "origin_class": "secondary"},
    ])
    alpha = next(line for line in text.splitlines() if "Alpha" in line)
    beta = next(line for line in text.splitlines() if "Beta" in line)
    assert "primary" in alpha and "secondary" not in alpha
    assert "secondary" in beta


# ── okengine#748: the standing churn detector ─────────────────────────────────────────────

def _ch():
    import importlib.util, sys
    from pathlib import Path as _P
    script = _P(__file__).resolve().parents[2] / "scripts/cron/collection_health.py"
    spec = importlib.util.spec_from_file_location("collection_health_under_test", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _raw(tmp_path, names):
    d = tmp_path / "raw" / "ai"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text("x", encoding="utf-8")
    return tmp_path


def test_a_clean_tree_has_a_ratio_of_one(tmp_path):
    m = _ch()
    _raw(tmp_path, ["a.md", "b.md", "c.md"])
    churn = m.raw_churn(tmp_path)
    assert churn == {"files": 3, "articles": 3, "ratio": 1.0, "worst": 1, "worst_article": "c.md"}


def test_revision_siblings_raise_the_ratio(tmp_path):
    m = _ch()
    _raw(tmp_path, ["a-revision-00000001.md", "a-revision-00000002.md",
                    "a-revision-00000003.md", "b.md"])
    churn = m.raw_churn(tmp_path)
    assert churn["files"] == 4 and churn["articles"] == 2
    assert churn["ratio"] == 2.0 and churn["worst"] == 3


def test_the_incident_shape_is_detected(tmp_path):
    """Scale check against the real numbers: 639 copies of one article must show up as a ratio
    far above the warn threshold, with the offender named."""
    m = _ch()
    names = [f"hot-revision-{i:08x}.md" for i in range(639)] + [f"o{i}.md" for i in range(50)]
    _raw(tmp_path, names)
    churn = m.raw_churn(tmp_path)
    assert churn["articles"] == 51
    assert churn["ratio"] > m.CHURN_RATIO_WARN
    assert churn["worst"] == 639 and churn["worst_article"] == "hot.md"


def test_no_raw_tree_reports_unknown_not_healthy(tmp_path):
    """NEGATIVE: an absent tree must not read as a perfect ratio. No data is not data saying
    no — the rule this repo already applies to empty parses."""
    m = _ch()
    churn = m.raw_churn(tmp_path)
    assert churn["ratio"] is None
    assert churn["articles"] == 0


def test_an_empty_raw_tree_also_reports_unknown(tmp_path):
    m = _ch()
    (tmp_path / "raw").mkdir()
    assert m.raw_churn(tmp_path)["ratio"] is None


def test_dotted_directories_are_skipped(tmp_path):
    """State and checkpoint trees are not corpus and must not skew the ratio."""
    m = _ch()
    _raw(tmp_path, ["a.md"])
    hidden = tmp_path / "raw" / ".state"
    hidden.mkdir(parents=True)
    for i in range(20):
        (hidden / f"s{i}.md").write_text("x", encoding="utf-8")
    assert m.raw_churn(tmp_path)["files"] == 1
