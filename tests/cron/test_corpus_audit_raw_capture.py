"""Regression: the raw tree's counts must separate duplicates from a real backlog.

The raw tree is append-only and nothing dedupes it, so every number derived from "raw files"
inherits its duplication. On one deployment that was 14,425 captures of 3,996 distinct URLs — 10,429
redundant files, one URL captured 555 times. A backlog computed from FILE counts read as 12,358
unpromoted; the honest figure was 1,516 distinct URLs. Counting files conflates waste with work.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "corpus_audit.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="corpus_audit absent")


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("corpus_audit", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["corpus_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, url: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: source\nurl: {url}\n---\n\nBody.\n", encoding="utf-8")


def test_duplicate_captures_are_reported_separately_from_the_backlog(tmp_path):
    """One promoted URL captured three times is waste, not three items of pending work."""
    mod = _load()
    for i in range(3):
        _write(tmp_path / "raw" / f"dup-{i}.md", "https://example.com/a")
    _write(tmp_path / "raw" / "new.md", "https://example.com/b")
    _write(tmp_path / "wiki" / "sources" / "2026" / "a.md", "https://example.com/a")

    r = mod.raw_capture_health(tmp_path)

    assert r["captures"] == 4
    assert r["distinct_urls"] == 2
    assert r["duplicate_captures"] == 2, "two redundant files, not two backlog items"
    assert r["unpromoted_urls"] == 1, "only example.com/b is genuinely unpromoted"


def test_url_normalisation_ignores_scheme_www_and_trailing_slash(tmp_path):
    """Otherwise the same article counts as promoted AND unpromoted at once."""
    mod = _load()
    _write(tmp_path / "raw" / "a.md", "http://www.example.com/a/")
    _write(tmp_path / "wiki" / "sources" / "a.md", "https://example.com/a")

    assert mod.raw_capture_health(tmp_path)["unpromoted_urls"] == 0


def test_an_unparseable_capture_is_counted_not_silently_skipped(tmp_path):
    """An empty parse is 'unknown', never 'nothing here' — it must stay visible in the numbers."""
    mod = _load()
    bad = tmp_path / "raw" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\ntitle: 'unclosed\n---\n\nBody.\n", encoding="utf-8")

    r = mod.raw_capture_health(tmp_path)
    assert r["unparseable_captures"] == 1
    assert r["captures"] == 0


def _schema(mod, monkeypatch, schema: dict):
    monkeypatch.setattr(mod.schema_lib, "merged_schema", lambda *a, **k: schema)


def test_the_detector_is_wired_into_the_audit_output(tmp_path, monkeypatch):
    """A detector nobody reads is not a detector.

    Asserted through audit()'s real output rather than by grepping the source for the call: a
    wiring test that matches a string literal passes for a call that was moved somewhere it
    never runs, and fails for a call that was merely reformatted.
    """
    mod = _load()
    _write(tmp_path / "raw" / "a.md", "https://example.com/a")
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    _schema(mod, monkeypatch, {"types": {"source": {}}})

    state = mod.audit(tmp_path)

    assert state["raw_capture_health"]["captures"] == 1


# --- okengine#594: the vocabulary an ingest lane mints in raw/ ------------------------------------
SOURCES_SCHEMA = {
    "types": {"source": {}},
    "enums": {"source_kind": ["news", "report"]},
    "field_enums": {"source_kind": {"enum": "source_kind"}},
}


def _capture(path: Path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}: {v}\n" for k, v in fields.items())
    path.write_text(f"---\ntype: source\n{body}---\n\nBody.\n", encoding="utf-8")


def test_a_value_no_schema_declares_is_reported_with_the_lane_that_wrote_it(tmp_path):
    """The lane name is what makes it fixable — a value alone sends you through every lane."""
    mod = _load()
    rules = mod._enum_rules(SOURCES_SCHEMA)
    _capture(tmp_path / "raw" / "a.md", source_kind="cyber-news", watch_lane="cti-search")
    _capture(tmp_path / "raw" / "b.md", source_kind="cyber-news", watch_lane="cti-search")
    _capture(tmp_path / "raw" / "c.md", source_kind="news", watch_lane="cti-search")

    minted = mod.raw_capture_health(tmp_path, rules)["minted_vocabulary"]

    assert minted["source_kind"]["cyber-news"]["count"] == 2
    assert minted["source_kind"]["cyber-news"]["lanes"] == ["cti-search"]
    assert minted["source_kind"]["cyber-news"]["extensible"] is False
    assert "news" not in minted["source_kind"], "a declared value is not a finding"


def test_an_extensible_enum_reports_the_value_as_legal_rather_than_as_a_violation(tmp_path):
    """A pack that declared its enum extensible said these values may be added — say so."""
    mod = _load()
    schema = dict(SOURCES_SCHEMA,
                  field_enums={"source_kind": {"enum": "source_kind", "extensible": True}})
    _capture(tmp_path / "raw" / "a.md", source_kind="cyber-news", source_channel="api")

    rec = mod.raw_capture_health(tmp_path, mod._enum_rules(schema))["minted_vocabulary"]
    assert rec["source_kind"]["cyber-news"]["extensible"] is True
    assert rec["source_kind"]["cyber-news"]["lanes"] == ["api"], "falls back to source_channel"


def _rendered(mod, monkeypatch, vault, schema):
    """The report a human actually reads, built from the real audit state."""
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    _schema(mod, monkeypatch, schema)
    return mod.render(mod.audit(vault), "2026-08-16")


def test_an_unresolvable_schema_reports_undetectable_and_never_a_clean(tmp_path, monkeypatch):
    """Silence is never success: no rules must not read as 'the ingest wrote nothing wrong'."""
    mod = _load()
    _capture(tmp_path / "raw" / "a.md", source_kind="cyber-news")

    r = mod.raw_capture_health(tmp_path, None)
    assert r["vocabulary_checked"] is False
    assert r["minted_vocabulary"] == {}

    rendered = _rendered(mod, monkeypatch, tmp_path, {"types": {"source": {}}})
    assert "UNDETECTABLE" in rendered
    assert "cyber-news" not in rendered, "an unmeasured vault must not exhibit findings"


def test_a_clean_ingest_is_reported_as_measured_not_as_undetectable(tmp_path, monkeypatch):
    mod = _load()
    _capture(tmp_path / "raw" / "a.md", source_kind="news")

    rendered = _rendered(mod, monkeypatch, tmp_path, SOURCES_SCHEMA)

    assert "every value the ingest lanes wrote is one the schema declares" in rendered


def test_the_finding_reaches_the_rendered_report(tmp_path, monkeypatch):
    """The count and the writing lane both have to survive into what a human reads."""
    mod = _load()
    _capture(tmp_path / "raw" / "a.md", source_kind="cyber-news", watch_lane="cti-search")

    rendered = _rendered(mod, monkeypatch, tmp_path, SOURCES_SCHEMA)

    assert "`cyber-news`" in rendered
    assert "`cti-search`" in rendered
    assert "closed enum" in rendered


def test_the_report_leads_with_the_value_written_most_often(tmp_path):
    """Ordering is the finding: the lane minting 900 records and the one minting 2 need not be
    read in the order their names happen to sort."""
    mod = _load()
    for i in range(3):
        _capture(tmp_path / "raw" / f"many-{i}.md", source_kind="zzz-frequent")
    _capture(tmp_path / "raw" / "one.md", source_kind="aaa-rare")

    minted = mod.raw_capture_health(tmp_path, mod._enum_rules(SOURCES_SCHEMA))
    assert list(minted["minted_vocabulary"]["source_kind"]) == ["zzz-frequent", "aaa-rare"]


def test_every_constrained_field_is_checked_not_just_the_first(tmp_path):
    """A capture whose first checked field is fine must not end the check for the rest of it."""
    mod = _load()
    schema = {
        "types": {"source": {}},
        "enums": {"tlp": ["CLEAR"], "source_kind": ["news"]},
        "field_enums": {"tlp": {"enum": "tlp"}, "source_kind": {"enum": "source_kind"}},
    }
    _capture(tmp_path / "raw" / "a.md", tlp="CLEAR", source_kind="cyber-news")

    minted = mod.raw_capture_health(tmp_path, mod._enum_rules(schema))["minted_vocabulary"]

    assert minted["source_kind"]["cyber-news"]["count"] == 1
    assert "tlp" not in minted, "the conformant field is not a finding"


def test_the_summary_line_reports_the_count_and_distinguishes_unmeasured(tmp_path, monkeypatch,
                                                                         capsys):
    """The cron's stdout is what a fleet-health reader sees; a measured zero and an unmeasured
    vault must not print the same thing."""
    mod = _load()
    _capture(tmp_path / "raw" / "a.md", source_kind="cyber-news")
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    mod.VAULT, mod.WIKI = tmp_path, tmp_path / "wiki"
    mod.DASH_DIR = tmp_path / "wiki" / "dashboards"

    _schema(mod, monkeypatch, SOURCES_SCHEMA)
    assert mod.main() == 0
    assert "1 undeclared ingest value(s)" in capsys.readouterr().out

    _schema(mod, monkeypatch, {"types": {"source": {}}})
    assert mod.main() == 0
    assert "ingest vocabulary UNDETECTABLE" in capsys.readouterr().out


def test_a_vault_whose_sources_schema_will_not_resolve_yields_no_rules(tmp_path, monkeypatch):
    """Unresolvable is None — the caller then reports UNDETECTABLE instead of a measured clean."""
    mod = _load()
    monkeypatch.setattr(mod.schema_lib, "merged_schema",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no schema")))
    assert mod._sources_enum_rules(tmp_path) is None


def test_a_schema_declaring_no_enums_also_yields_no_rules(tmp_path, monkeypatch):
    mod = _load()
    _schema(mod, monkeypatch, {"types": {"source": {}}})
    assert mod._sources_enum_rules(tmp_path) is None


def test_the_lane_list_is_capped_while_the_count_stays_complete(tmp_path):
    """Many lanes minting one value is worse, not less worth counting."""
    mod = _load()
    lanes = mod.MAX_EXAMPLES + 3
    for i in range(lanes):
        _capture(tmp_path / "raw" / f"{i:02d}.md", source_kind="cyber-news",
                 watch_lane=f"lane-{i:02d}")

    rec = mod.raw_capture_health(tmp_path, mod._enum_rules(SOURCES_SCHEMA))
    rec = rec["minted_vocabulary"]["source_kind"]["cyber-news"]

    assert rec["count"] == lanes
    assert len(rec["lanes"]) == mod.MAX_EXAMPLES


def test_a_capture_naming_no_lane_still_counts(tmp_path):
    mod = _load()
    _capture(tmp_path / "raw" / "a.md", source_kind="cyber-news")

    rec = mod.raw_capture_health(tmp_path, mod._enum_rules(SOURCES_SCHEMA))
    assert rec["minted_vocabulary"]["source_kind"]["cyber-news"] == {
        "count": 1, "extensible": False, "lanes": [],
    }


def test_a_list_valued_field_is_not_reported_as_an_out_of_enum_string(tmp_path):
    """`['a','b']` stringified would be a finding on every page carrying a list."""
    mod = _load()
    schema = dict(SOURCES_SCHEMA,
                  enums={"tags": ["x"]}, field_enums={"tags": {"enum": "tags"}})
    path = tmp_path / "raw" / "a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: source\ntags:\n- x\n- y\n---\n\nBody.\n", encoding="utf-8")

    assert mod.raw_capture_health(tmp_path, mod._enum_rules(schema))["minted_vocabulary"] == {}
