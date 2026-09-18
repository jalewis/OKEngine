"""Failure-path and boundary coverage for the Cockpit read surface."""
from __future__ import annotations

import importlib.util
import sys
import datetime
import asyncio
import json
import os
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-cockpit/app.py"


def _load(tmp_path, monkeypatch, name="cockpit_edge_app"):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    monkeypatch.setenv("OKENGINE_TRUST", "private")
    monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")
    sys.path.insert(0, str(APP.parent))
    spec = importlib.util.spec_from_file_location(name, APP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_frontmatter_wikilink_and_safe_read_edges(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch)
    assert module.split_fm("plain") == ({}, "plain")
    assert module.split_fm("---\n- list\n---\nbody") == ({}, "body")
    assert module.split_fm("---\ninvalid: [\n---\nbody") == ({}, "body")
    assert module.split_fm("---\ndate: 2026-99-99\n---\nbody") == ({}, "body")
    alias = module._WIKILINK.search("[[entities/a/page|Alias]]")
    target = module._WIKILINK.search("[[entities/a/page]]")
    anchor = module._WIKILINK.search("[[#heading]]")
    assert module._wl_display(alias) == "Alias"
    assert module._wl_display(target) == "page"
    assert module._wl_display(anchor) == "heading"
    assert module._linkify("[[#heading]]") == "heading"
    assert "data-page" in module._linkify('[[entities/a/\"quoted]]')
    assert module._deref_local_links("[A](entities/a) [B](https://example.test)") == (
        "A [B](https://example.test)"
    )
    assert module._strip_md("**[[entities/a|A]]** `code`") == "A code"
    inline = module._inline_md("**bold** `code` [site](https://example.test) <tag>")
    assert "<strong>bold</strong>" in inline and "<code>code</code>" in inline
    assert "&lt;tag&gt;" in inline
    assert module._uncode_wikilinks("`[[entities/a]]`") == "[[entities/a]]"
    rendered = module.render_md("`[[entities/a|A]]`\n```dataview\nTABLE x\n```")
    assert "data-page=\"entities/a\"" in rendered and "Dataview view" in rendered
    assert module._file_date("daily-2026-08-04.md") == "2026-08-04"
    assert module._file_date("undated.md") is None
    with pytest.raises(module.HTTPException) as error:
        module.safe_read(wiki, "../outside")
    assert error.value.status_code == 404
    prefix_sibling = tmp_path / "wiki-private"
    prefix_sibling.mkdir()
    (prefix_sibling / "secret.md").write_text("secret")
    with pytest.raises(module.HTTPException):
        module.safe_read(wiki, "../wiki-private/secret.md")


def test_budget_pause_probe_fails_open_on_filesystem_error(monkeypatch):
    from okengine.cockpit_services import chat

    monkeypatch.setattr(chat, "VAULT", Path("/vault"), raising=False)
    monkeypatch.setattr(Path, "exists", lambda _path: (_ for _ in ()).throw(OSError("raced")))
    assert chat._budget_tripped() is False


def test_embed_resolution_cache_asset_missing_blocked_and_unreadable(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    page = wiki / "briefings/deep/page.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: Page\n---\nbody ![[nested]]\n")
    nested = wiki / "reports/nested.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("nested body")
    module = _load(tmp_path, monkeypatch, "cockpit_embed_edges")
    assert module._embed_rglob("page.md") == page
    assert module._embed_rglob("page.md") == page
    assert module._embed_rglob("absent.md") is None
    assert module._embed_rglob("absent.md") is None
    assert module._resolve_embeds("![[anything]]", depth=4) == "![[anything]]"
    assert "embedded asset" in module._resolve_embeds("![[image.png]]")
    assert "missing embed" in module._resolve_embeds("![[absent]]")
    assert "nested body" in module._resolve_embeds("![[page]]")

    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    link = wiki / "blocked.md"
    link.symlink_to(outside)
    assert "blocked embed" in module._resolve_embeds("![[blocked]]")

    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("raced"))
        if path == page else original(path, *args, **kwargs),
    )
    assert "unreadable embed" in module._resolve_embeds("![[page]]")


def test_original_source_link_enrichment_edges(tmp_path, monkeypatch):
    sources = tmp_path / "wiki/sources"
    sources.mkdir(parents=True)
    module = _load(tmp_path, monkeypatch, "cockpit_original_link_edges")
    missing = '<a class="wl" data-page="sources/missing">Missing</a>'
    assert module._link_originals(missing) == missing
    (sources / "local.md").write_text("---\ntitle: Local title\nurl: ftp://example.test\n---\n")
    local = module._link_originals('<a class="wl" data-page="sources/local">Local</a>')
    assert 'class="wl"' in local and "Local title" in local
    (sources / "external.md").write_text(
        "---\nname: External name\nurl: https://example.test/report?a=1&b=2\n---\n"
    )
    external = module._link_originals(
        '<a class="wl" data-page="sources/external">External</a>'
    )
    assert 'class="ext"' in external and "External name" in external and "&amp;" in external


def test_stream_enumeration_document_and_pdf_edges(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    briefings = wiki / "briefings"
    briefings.mkdir(parents=True)
    live = briefings / "daily-2026-08-04.md"
    live.write_text("---\ntype: daily\n---\n# Daily heading\nbody\n")
    other = briefings / "note.md"
    other.write_text("---\ntype: other\n---\nbody\n")
    raced = briefings / "raced.md"
    raced.write_text("---\ntype: daily\n---\n")
    module = _load(tmp_path, monkeypatch, "cockpit_stream_edges")
    streams = {
        "daily": {"key": "daily", "label": "Daily", "dir": "briefings", "type": "daily",
                  "pdf": True},
        "missing": {"key": "missing", "label": "Missing", "dir": "absent", "pdf": False},
    }
    monkeypatch.setattr(module, "_streams", lambda: streams)
    assert module._visible_page(tmp_path / "outside.md", briefings) is False
    assert module._ns_dirs(tmp_path / "outside.md") == frozenset()
    assert module._stream_pages(streams["missing"]) == []
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("raced"))
        if path == raced else original(path, *args, **kwargs),
    )
    assert module._stream_pages(streams["daily"]) == [str(live)]
    assert module._stream_dates("unknown") == []
    assert module._stream_dates("daily") == ["2026-08-04"]
    listing = module.api_streams()["streams"]
    assert listing[0]["latest"] == "2026-08-04" and listing[1]["latest"] is None
    with pytest.raises(module.HTTPException) as error:
        module._doc_path("unknown", "2026-08-04")
    assert error.value.status_code == 404
    with pytest.raises(module.HTTPException) as error:
        module._doc_path("daily", "bad")
    assert error.value.status_code == 400
    with pytest.raises(module.HTTPException):
        module._doc_path("daily", "2026-08-03")
    assert module.api_doc("daily", "2026-08-04")["title"] == "Daily heading"
    with pytest.raises(module.HTTPException):
        module.api_doc("unknown", "2026-08-04")
    with pytest.raises(module.HTTPException):
        module.api_stream_pdf("missing", "2026-08-04")
    with pytest.raises(module.HTTPException) as error:
        module.api_stream_pdf("daily", "bad")
    assert error.value.status_code == 400
    with pytest.raises(module.HTTPException):
        module.api_stream_pdf("daily", "2026-08-03")

    pdf = live.with_suffix(".pdf")
    pdf.write_bytes(b"pdf")
    response = module.api_stream_pdf("daily", "2026-08-04")
    assert Path(response.path) == pdf


def test_deck_renderer_stale_failure_and_success_edges(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_deck_edges")
    deck = tmp_path / "deck.md"
    deck.write_text("# deck")
    cache = tmp_path / "cache"
    cache.mkdir()
    stale = cache / "deck.1.pdf"
    stale.write_bytes(b"stale")
    monkeypatch.setattr(module, "_MARP", "/usr/bin/marp")
    monkeypatch.setattr(module, "_DECK_CACHE", cache)

    def render(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"rendered")

    monkeypatch.setattr(module.subprocess, "run", render)
    rendered = module._render_deck_pdf(deck)
    assert rendered and rendered.read_bytes() == b"rendered" and not stale.exists()
    rendered.unlink()
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    assert module._render_deck_pdf(deck) is None


def test_prediction_helper_schema_evidence_and_claim_edges(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_prediction_helper_edges")
    assert module._subject({"subject": []}) == ""
    assert module._subject({"subject": ["[[entities/a/alpha|Alpha alias]]"]}) == "Alpha alias"
    assert module._trajectory({"evidence": [
        {"date": "2026-02-01", "confidence_after": .7},
        {"date": "2026-01-01", "confidence_before": .4, "confidence_after": .5},
        "ignored",
    ]}) == [.4, .5, .7]
    assert module._trajectory({"evidence": "bad"}) == []

    artifact = tmp_path / ".okengine/composed-schema.yaml"
    artifact.parent.mkdir()
    artifact.write_text("[broken\n")
    module._ev_direction_enum.cache_clear()
    assert module._ev_direction_enum() == module._EV_DIR_FALLBACK
    artifact.write_text(
        "field_items:\n  evidence:\n    direction:\n      enum: [supports-custom, opposes-custom]\n"
    )
    module._ev_direction_enum.cache_clear()
    assert module._ev_direction_enum() == frozenset({"supports-custom", "opposes-custom"})
    assert module._ev_bucket("") is None
    assert module._ev_bucket("supports") == "reinforces"
    assert module._ev_bucket("unmapped") is None

    entries = module._evidence_entries({"evidence": [
        {"on": "2026-01-03", "tag": "note", "text": "context", "url": "https://x",
         "confidence": .6},
        "free text without a prefix",
        42,
        {},
    ]})
    assert entries[0]["note"] == "free text without a prefix"
    assert entries[1]["confidence_after"] == .6 and entries[1]["source"] == "https://x"
    assert len(entries) == 2
    assert module._evidence_entries({"evidence": "bad"}) == []

    monkeypatch.setenv("PREDICTION_CONFIDENCE_SCALE", '{"Custom": 0.42}')
    assert module._prediction_confidence_scale() == {"custom": .42}
    monkeypatch.setenv("PREDICTION_CONFIDENCE_SCALE", "[1]")
    assert module._prediction_confidence_scale() is module.PREDICTION_CONFIDENCE_SCALE
    monkeypatch.setenv("PREDICTION_CONFIDENCE_SCALE", '{"bad": "x"}')
    assert module._prediction_confidence_scale() is module.PREDICTION_CONFIDENCE_SCALE
    assert module._conf({"confidence": .3333}) == .333
    assert module._conf({"confidence": None}) is None
    assert module._claim({"claim": "**Claim:** The thing happens"}, "") == "The thing happens"
    assert module._claim({"trigger": "Trigger text"}, "") == "Trigger text"
    assert module._claim({}, "# Heading\n\nA sufficiently long forecast paragraph appears here.") == (
        "A sufficiently long forecast paragraph appears here."
    )
    assert module._claim({}, "# Heading\n\nshort") == ""


def test_prediction_loading_summary_detail_and_failure_edges(tmp_path, monkeypatch):
    pred = tmp_path / "wiki/predictions"
    pred.mkdir(parents=True)
    (pred / "_hidden.md").write_text("---\ntype: prediction\n---\n")
    (pred / "old.bak.md").write_text("---\ntype: prediction\n---\n")
    (pred / "other.md").write_text("---\ntype: note\n---\n")
    raced = pred / "raced.md"
    raced.write_text("---\ntype: prediction\n---\n")
    (pred / "no-date-match.md").write_text(
        "---\ntype: prediction\nstatus: confirmed\nresolves_by: someday\n---\n"
        "# No date\nA long enough claim paragraph for this row.\n"
    )
    (pred / "invalid-date.md").write_text(
        "---\ntype: prediction\nstatus: open\nmade_on: 2020-01-01\nresolves_by: '2026-99-99'\n"
        "forecast_set: set-b\nevidence:\n  - {date: 2026-01-01, direction: unmapped, note: x}\n"
        "---\n# Invalid\nA long enough claim paragraph for the row.\n"
    )
    due = pred / "due.md"
    due.write_text(
        "---\ntype: prediction\nstatus: open\nsubject: '[[entities/a/alpha]]'\n"
        "made_on: 2026-01-01\nresolves_by: 2026-08-07\nforecast_set: set-a\n---\n"
        "# Due\nA long enough claim paragraph for the due row.\n"
    )
    module = _load(tmp_path, monkeypatch, "cockpit_prediction_loading_edges")
    monkeypatch.setattr(module, "cockpit_config", lambda: {"predictions_dirs": ["predictions"]})
    monkeypatch.setattr(module, "TODAY", lambda: datetime.date(2026, 8, 4))
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("raced"))
        if path == raced else original(path, *args, **kwargs),
    )
    rows = module._load_predictions()
    assert {row["id"] for row in rows} == {
        "predictions/invalid-date", "predictions/no-date-match", "predictions/due",
    }
    invalid = next(row for row in rows if row["id"].endswith("invalid-date"))
    assert invalid["resolves_by"] is None and invalid["idle"] is False
    summary = module.api_predictions()
    assert summary["due_soon"] == 1 and summary["forecast_sets"] == ["set-a", "set-b"]
    for bad in ("", "/absolute", "../escape", "has.md"):
        with pytest.raises(module.HTTPException) as error:
            module.api_prediction(bad)
        assert error.value.status_code == 400
    with pytest.raises(module.HTTPException) as error:
        module.api_prediction("predictions/missing")
    assert error.value.status_code == 404
    detail = module.api_prediction("predictions/due")
    assert detail["id"] == "predictions/due" and detail["fm"]["subject"]


def test_dataset_cache_scan_warm_and_date_failure_edges(tmp_path, monkeypatch, capsys):
    pages = tmp_path / "wiki/entities"
    pages.mkdir(parents=True)
    raced = pages / "raced.md"
    raced.write_text("---\ntype: entity\n---\n")
    module = _load(tmp_path, monkeypatch, "cockpit_dataset_cache_edges")
    assert module._as_date("someday") is None
    assert module._as_date("2026-99-99") is None
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("raced"))
        if path == raced else original(path, *args, **kwargs),
    )
    assert module._scan_dir_meta("entities") == []
    module._DIR_REFRESHING.add("entities")
    module._refresh_dir_async("entities")
    assert module._DIR_REFRESHING == {"entities"}
    module._DIR_REFRESHING.clear()

    cfg = {
        "tabs": ["missing", "mixed"],
        "tab_defs": {"missing": [], "mixed": {"boxes": [
            {"dataset": "bad", "dir": "/legacy/"},
            {"dataset": {"dir": "/entities/"}},
        ]}},
        "streams": ["bad", {"dir": "/briefings/"}],
    }
    assert module._configured_dataset_dirs(cfg, landing_only=True) == []
    assert module._configured_dataset_dirs(cfg) == ["legacy", "entities", "briefings"]

    monkeypatch.setattr(module, "_configured_dataset_dirs", lambda **kwargs: ["bad"])
    monkeypatch.setattr(module, "_scan_dir_meta", lambda sub: (_ for _ in ()).throw(RuntimeError("scan")))
    module._warm_tab_datasets()
    module._warm_initial_tab_datasets()
    module._ACTIVE_REQUESTS = 1
    assert module._requests_active()
    module._ACTIVE_REQUESTS = 0
    assert not module._requests_active()
    monkeypatch.setattr(module, "_POST_READY_WARM_GAP", .1)
    slept = []
    monkeypatch.setattr(module.time, "sleep", lambda delay: slept.append(delay))
    module._warm_remaining_tab_datasets()
    assert slept == [.1]
    assert "dataset warm failed" in capsys.readouterr().err
    events = []
    module._schedule_remaining_tab_warmup._started = False
    monkeypatch.setattr(module, "_POST_READY_WARM_DELAY", 0)
    monkeypatch.setattr(module, "_warm_remaining_tab_datasets", lambda: events.append("warm"))

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
    module._schedule_remaining_tab_warmup()
    assert events == ["warm"]


def test_watchlist_source_display_and_competitor_edges(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_watchlist_edges")
    assert module._source_title({"title": "vendor-report_pdf"}) == "Vendor Report"
    assert module._source_title({"_name": "fallback-title"}) == "Fallback Title"
    assert module._source_publisher({"publisher": "Publisher"}) == "Publisher"
    assert module._source_publisher({"publisher": "Unknown", "url": "https://www.example.test/a"}) == (
        "example.test"
    )
    assert module._source_publisher({"publisher": "—", "url": "http://[bad"}) == (
        "Unknown publisher ⚠"
    )
    assert module._truthy(True) and module._truthy(" YES ") and not module._truthy(1)

    no_watchlist = {"watchlist": None, "competitors": []}
    monkeypatch.setattr(module, "cockpit_config", lambda: no_watchlist)
    assert module.api_watchlist() == {"sections": [], "counts": {}}
    assert module.api_competitors() == {"views": []}

    labels = {
        "section": "Watch", "entity": "Entity", "tier": "Tier", "rating": "Rating",
        "acquirers": "Acquirers",
    }
    config = {
        "watchlist": {
            "labels": labels, "entity_dir": "entities", "entity_types": ["vendor"],
            "tier_field": "tier", "rating_field": "rating", "moved_field": "updated",
            "acquirer_field": "acquirer", "trends": {"concept_dir": "concepts", "type": "trend"},
        },
        "competitors": [
            {"key": "missing", "path": "dashboards/missing"},
            {"key": "view", "path": "dashboards/view"},
        ],
    }
    entities = [
        {"_name": "a", "_sub": "entities", "type": "vendor", "tier": "one",
         "rating": "high", "updated": "2026-08-01", "acquirer": "yes"},
        {"_name": "b", "_sub": "entities", "type": "vendor", "tier": "one",
         "rating": "unknown", "updated": "2026-01-01", "acquirer": False},
        {"_name": "c", "_sub": "entities", "type": "concept", "tier": "two"},
    ]
    concepts = [
        {"_name": "active", "_sub": "concepts", "type": "trend", "trend_status": "active",
         "thesis_confidence": .8, "thesis": "x" * 130,
         "anchored_predictions": ["a"], "last_thesis_update": "2026-08-01"},
        {"_name": "closed", "_sub": "concepts", "type": "trend", "trend_status": "dormant"},
        {"_name": "unset", "_sub": "concepts", "type": "trend"},
    ]
    monkeypatch.setattr(module, "cockpit_config", lambda: config)
    monkeypatch.setattr(module, "_load_dir", lambda sub: entities if sub == "entities" else concepts)
    monkeypatch.setattr(module, "TODAY", lambda: datetime.date(2026, 8, 4))
    watch = module.api_watchlist()
    assert watch["counts"] == {"tracked": 2, "trends": 3}
    titles = {section["title"] for section in watch["sections"]}
    assert {"Rating matrix by tier", "Acquirers", "Active trends", "Needs status (no trend_status)"} <= titles

    config["watchlist"] = {
        **config["watchlist"], "entity_types": [], "rating_field": None, "trends": None,
    }
    watch = module.api_watchlist()
    assert watch["counts"] == {"tracked": 3}

    dashboard = wiki / "dashboards/view.md"
    dashboard.parent.mkdir()
    dashboard.write_text("---\ntitle: Competitor view\nupdated: 2026-08-04\n---\n# View\nbody")
    competitors = module.api_competitors()["views"]
    assert len(competitors) == 1 and competitors[0]["title"] == "Competitor view"


def test_home_empty_dashboard_shapes_and_refine_all_filters(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_home_refine_edges")
    config = {
        "dashboards": [{"items": [{}, {"path": ""}]}, ""],
        "watchlist": None, "competitors": [], "streams": [],
    }
    monkeypatch.setattr(module, "cockpit_config", lambda: config)
    monkeypatch.setattr(module, "_load_dir", lambda sub: [])
    monkeypatch.setattr(module, "api_streams", lambda: {"streams": []})
    monkeypatch.setattr(module, "api_predictions", lambda: {"total": 0, "rows": []})
    assert module.api_home() == {"sections": []}

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    rows = [
        {"kind": "keep", "present": "yes", "absent": "", "tags": ["safe"],
         "published": today + "T01:00:00Z"},
        {"kind": "drop", "present": "", "absent": "value", "tags": "blocked",
         "published": "2020-01-01"},
    ]
    refined = module._refine_rows(rows, {
        "where": {"kind": "keep"}, "has": ["present"], "missing": ["absent"],
        "exclude_values": {"tags": ["blocked"]}, "today_prefix": "published",
    })
    assert refined == [rows[0]]


def test_refine_rows_supports_rolling_hour_window(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_rolling_window")
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = [
        {"created": (now - datetime.timedelta(hours=23, minutes=59)).isoformat(), "id": "fresh"},
        {"created": (now - datetime.timedelta(hours=1)).replace(tzinfo=None).isoformat(), "id": "naive"},
        {"created": (now - datetime.timedelta(hours=24, minutes=1)).isoformat(), "id": "stale"},
        {"created": "not-a-date", "id": "invalid"},
        {"id": "missing"},
    ]

    assert module._refine_rows(
        rows, {"within_hours": {"field": "created", "hours": 24}}
    ) == rows[:2]

    assert module._refine_rows(
        rows, {"within_hours": {"field": "created", "hours": "invalid"}}
    ) == rows


def test_configured_rows_forwards_rolling_hour_window(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_rolling_window_owner")
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = [{"created": (now - datetime.timedelta(hours=2)).isoformat()}]
    monkeypatch.setattr(module, "_load_dir", lambda _sub: rows)

    owner = {
        "dataset": {"dir": "sources"},
        "within_hours": {"field": "created", "hours": 1},
    }
    assert module._configured_rows(owner) == []


def test_search_backlink_artifact_and_link_parser_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_backlink_edges")
    assert module.api_search(" ") == {"q": "", "results": []}
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        FileNotFoundError("rg")))
    with pytest.raises(module.HTTPException) as error:
        module.api_search("term")
    assert error.value.status_code == 503
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        module.subprocess.TimeoutExpired("rg", 12)))
    assert module.api_search("term")["truncated"] is True

    outside = tmp_path / "outside.md"
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: types.SimpleNamespace(
        stdout=f"malformed\n{outside}:1:nope\n{wiki / 'page.md'}:2:term here\n"
               f"{wiki / 'page.md'}:3:term again\n"
    ))
    result = module.api_search("term", limit=0)
    assert result["total"] == 1 and len(result["results"]) == 1

    assert module._artifact_backlinks() is None
    artifact = wiki / ".backlinks.json"
    artifact.write_text("bad-json")
    assert module._artifact_backlinks() is None
    artifact.write_text("[]")
    assert module._artifact_backlinks() is None
    artifact.write_text(json.dumps({"backlinks": {"entities/a": [{"key": "reports/r"}]}}))
    backlinks = module._artifact_backlinks()
    assert backlinks and module._artifact_backlinks() is backlinks
    old = time.time() - module._BL_ARTIFACT_MAX_AGE - 1
    os.utime(artifact, (old, old))
    assert module._artifact_backlinks() is None

    assert module._bl_skip_name("INDEX-x.md") is True
    assert module._bl_skip_name("normal.md") is False
    assert module._bl_wikikey("https://example.test") is None
    assert module._bl_wikikey("entities/a.md#section|A") == "entities/a"
    assert module._bl_mdkey("https://example.test/a.md", "reports") is None
    assert module._bl_mdkey("../../outside.md", "reports") is None
    assert module._bl_mdkey("../entities/a.md#x", "reports") == "entities/a"
    stripped = module._bl_strip("---\ntitle: X\n---\n```\n[[hidden]]\n```\n`[[inline]]` [[shown]]")
    assert "shown" in stripped and "hidden" not in stripped and "inline" not in stripped


def test_backlink_schema_titles_scan_build_and_cache(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_backlink_scan_edges")
    schema = tmp_path / "schema.yaml"
    schema.write_text("backlink_drop: [wiki/raw/nested, reports]\n")
    monkeypatch.setattr(module, "_governing_schema_path", lambda: schema)
    module._BL_DROP_CACHE = (0.0, None)
    assert module._backlink_drop_dirs() == frozenset({"raw", "reports"})
    assert module._backlink_drop_dirs() == frozenset({"raw", "reports"})
    schema.write_text("[broken")
    module._BL_DROP_CACHE = (0.0, None)
    assert module._backlink_drop_dirs() == frozenset({"sources"})

    pages = {
        "entities/a": "---\ntitle: Alpha\n---\n# ignored\n",
        "entities/b": "---\nname: Beta\n---\nbody\n",
        "entities/c": "# Charlie\n",
        "entities/delta-name": "body\n",
        "reports/ref": "[[entities/a]] [[a]] [B](../entities/b.md) `[[entities/c]]`\n",
        "reports/dupe": "[[entities/a]] [[entities/a]]\n",
        "_hidden/ref": "[[entities/a]]\n",
    }
    for rel, content in pages.items():
        path = wiki / f"{rel}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    module._BL_DROP_CACHE = (time.monotonic(), frozenset())
    assert module._backlink_title("entities/a") == "Alpha"
    assert module._backlink_title("entities/b") == "Beta"
    assert module._backlink_title("entities/c") == "Charlie"
    assert module._backlink_title("entities/delta-name") == "delta name"
    assert module._backlink_title("entities/missing") == "missing"
    assert module._skip_backlink_src("_hidden/ref") is True
    docs = module._scan_forward_refs()
    assert any(doc["key"] == "reports/ref" for doc in docs)
    built = module._build_backlinks()
    assert {row["key"] for row in built["entities/a"]} == {"reports/ref", "reports/dupe"}

    monkeypatch.setattr(module, "_artifact_backlinks", lambda: {"cached": []})
    assert module._load_backlinks() == {"cached": []}
    monkeypatch.setattr(module, "_artifact_backlinks", lambda: None)
    module._BACKLINKS = {"map": {"fresh": []}, "ts": time.monotonic()}
    assert module._load_backlinks() == {"fresh": []}
    module._BACKLINKS = {"map": None, "ts": 0.0}
    monkeypatch.setattr(module, "_refresh_backlinks_async", lambda: None)
    assert module._load_backlinks(blocking=False) == {}
    monkeypatch.setattr(module, "_build_backlinks", lambda: {"built": []})
    assert module._load_backlinks(blocking=True) == {"built": []}


def test_chat_shell_and_health_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text('<link href="/static/style.css"><script src="/static/app.js"></script>')
    (static / "style.css").write_text("css")
    (static / "app.js").write_text("js")
    (static / "favicon.svg").write_text("<svg/>")
    module = _load(tmp_path, monkeypatch, "cockpit_chat_shell_edges")
    monkeypatch.setattr(module, "STATIC", static)

    class Request:
        def __init__(self, value=None, error=None):
            self.value, self.error = value, error

        async def json(self):
            if self.error:
                raise self.error
            return self.value

    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.api_chat(Request({"messages": []})))
    assert error.value.status_code == 503
    monkeypatch.setattr(module, "_AGENT_API", "http://agent")
    monkeypatch.setattr(module, "_AGENT_KEY", "secret")
    pause = tmp_path / ".okengine" / "budget-paused"
    pause.parent.mkdir()
    pause.write_text("budget exceeded\n", encoding="utf-8")
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.api_chat(Request({"messages": [{"role": "user", "content": "x"}]})))
    assert error.value.status_code == 503
    assert "budget-guard" in error.value.detail
    pause.unlink()
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.api_chat(Request(error=ValueError("bad"))))
    assert error.value.status_code == 400
    for body in ({}, {"messages": []}, {"messages": [None, {"role": "system", "content": "x"}]}):
        with pytest.raises(module.HTTPException):
            asyncio.run(module.api_chat(Request(body)))
    response = asyncio.run(module.api_chat(Request({"messages": [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": " hello "},
        {"role": "assistant", "content": "answer"},
    ]})))
    assert response.media_type == "text/event-stream"

    class Upstream:
        def __enter__(self):
            return iter((b"data: one\n\n", b"data: two\n\n"))

        def __exit__(self, *_args):
            return False

    async def consume(stream):
        return b"".join([chunk async for chunk in stream.body_iterator])

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: Upstream())
    response = asyncio.run(module.api_chat(Request({"messages": [{"role": "user", "content": "x"}]})))
    assert asyncio.run(consume(response)) == b"data: one\n\ndata: two\n\n"

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        module.urllib.error.HTTPError("url", 429, "limited", {}, None)))
    response = asyncio.run(module.api_chat(Request({"messages": [{"role": "user", "content": "x"}]})))
    assert b"agent error 429" in asyncio.run(consume(response))
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("offline")))
    response = asyncio.run(module.api_chat(Request({"messages": [{"role": "user", "content": "x"}]})))
    assert b"agent unreachable" in asyncio.run(consume(response))

    assert module.favicon().media_type == "image/svg+xml"
    html = module.index()
    assert "?v=" in html
    original_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (_ for _ in ()).throw(OSError("raced"))
                        if path.name == "style.css" else original_read_bytes(path))
    assert "/static/app.js" in module.index()
    assert module.healthz()["vault_present"] is True


def test_backlink_transient_and_singleflight_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_backlink_transient_edges")
    bad_title = wiki / "entities/bad.md"
    bad_title.parent.mkdir(parents=True)
    bad_title.write_text("---\ntitle: [\n---\n# Fallback heading\n")
    assert module._backlink_title("entities/bad") == "Fallback heading"

    dangling = wiki / "entities/dangling.md"
    dangling.symlink_to(wiki / "entities/absent.md")
    assert all(doc["key"] != "entities/dangling" for doc in module._scan_forward_refs())

    monkeypatch.setattr(module, "_scan_forward_refs", lambda: [
        {}, {"key": "_hidden/a"},
        {"key": "reports/a", "references": [{}, {"key": "reports/a"},
                                                 {"key": "_hidden/b"}, {"key": "entities/x"},
                                                 {"key": "entities/x"}]},
    ])
    monkeypatch.setattr(module, "_skip_backlink_src", lambda key: key.startswith("_hidden"))
    monkeypatch.setattr(module, "_backlink_title", lambda _key: "Report")
    assert module._build_backlinks() == {"entities/x": [{"key": "reports/a", "title": "Report"}]}

    class Lock:
        def __init__(self, acquired=True):
            self.acquired = acquired

        def acquire(self, blocking=False):
            return self.acquired

        def release(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    module._BL_LOCK = Lock(False)
    assert module._refresh_backlinks_async() is None
    module._BL_LOCK = Lock(True)
    module._BACKLINKS = {"map": {}, "ts": time.monotonic()}
    assert module._refresh_backlinks_async() is None
    started = []
    module._BACKLINKS = {"map": None, "ts": 0.0}
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: types.SimpleNamespace(
        start=lambda: started.append(kwargs)))
    module._refresh_backlinks_async()
    assert started

    monkeypatch.setattr(module, "_artifact_backlinks", lambda: None)
    monkeypatch.setattr(module, "_build_backlinks", lambda: {})
    module._BL_LOCK = Lock(True)
    # stale RELATIVE to monotonic() -- an absolute 0.0 only reads as stale once the host has been
    # up longer than _BACKLINKS_TTL (24h default), so on a rebooted machine this took the early
    # return and never exercised the in-lock rebuild path below.
    module._BACKLINKS = {"map": {"stale": []}, "ts": time.monotonic() - module._BACKLINKS_TTL - 1}
    assert module._load_backlinks(blocking=True) == {"stale": []}


def test_rollup_download_conversion_and_chat_export_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    pages = wiki / "briefings"
    pages.mkdir(parents=True)
    (pages / "daily-2026-08-04.md").write_text("---\ntitle: Daily\n---\nBody")
    module = _load(tmp_path, monkeypatch, "cockpit_download_edges")
    monkeypatch.setattr(module, "_streams", lambda: {
        "daily": {"dir": "briefings", "label": "Daily"}
    })
    with pytest.raises(module.HTTPException):
        module.api_rollup("missing", 7)
    monkeypatch.setattr(module, "_stream_dates", lambda _stream: ["2026-08-04", "2026-08-03"])

    def doc(stream, date):
        if date.endswith("03"):
            raise module.HTTPException(404, "missing")
        return {"html": f"<p>{stream}-{date}</p>"}

    monkeypatch.setattr(module, "api_doc", doc)
    rollup = module.api_rollup("daily", 99)
    assert rollup["count"] == 1 and "2026-08-04" in rollup["html"]

    with pytest.raises(module.HTTPException):
        module._resolve_source("missing", "2026-08-04", None)
    raw, base, title = module._resolve_source("daily", "2026-08-04", None)
    assert "Body" in raw and base == "daily-2026-08-04" and title.startswith("Daily")
    for path, status in (("../bad", 400), ("absent", 404)):
        with pytest.raises(module.HTTPException) as error:
            module._resolve_source(None, None, path)
        assert error.value.status_code == status
    with pytest.raises(module.HTTPException):
        module._resolve_source(None, None, None)

    (wiki / "entities/a").mkdir(parents=True)
    (wiki / "entities/b").mkdir(parents=True)
    (wiki / "entities/a/same.md").write_text("one")
    (wiki / "entities/b/same.md").write_text("two")
    with pytest.raises(module.HTTPException) as error:
        module._resolve_source(None, None, "same")
    assert error.value.status_code == 409
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (wiki / "blocked.md").symlink_to(outside)
    with pytest.raises(module.HTTPException) as error:
        module._resolve_source(None, None, "blocked")
    assert error.value.status_code == 403

    def convert(cmd, **_kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_bytes(b"converted")
        return types.SimpleNamespace()

    monkeypatch.setattr(module.subprocess, "run", convert)
    assert module._pandoc("# Report", "docx") == b"converted"
    assert module._pandoc("# Report", "pdf", "Title") == b"converted"
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(
        FileNotFoundError("pandoc")))
    with pytest.raises(module.HTTPException) as error:
        module._pandoc("x", "pdf")
    assert error.value.status_code == 503
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(
        module.subprocess.CalledProcessError(1, "pandoc", stderr=b"bad input")))
    with pytest.raises(module.HTTPException) as error:
        module._pandoc("x", "docx")
    assert error.value.status_code == 500 and "bad input" in error.value.detail

    with pytest.raises(module.HTTPException):
        module.api_download("bad", path="briefings/daily-2026-08-04")
    response = module.api_download("md", path="briefings/daily-2026-08-04")
    assert response.media_type.startswith("text/markdown")

    class Request:
        def __init__(self, value=None, error=None): self.value, self.error = value, error
        async def json(self):
            if self.error: raise self.error
            return self.value

    with pytest.raises(module.HTTPException):
        asyncio.run(module.api_chat_export(Request({}), "bad"))
    with pytest.raises(module.HTTPException):
        asyncio.run(module.api_chat_export(Request(error=ValueError("bad")), "md"))
    with pytest.raises(module.HTTPException):
        asyncio.run(module.api_chat_export(Request({}), "md"))
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.api_chat_export(Request({"content": "x" * 200_001}), "md"))
    assert error.value.status_code == 413
    exported = asyncio.run(module.api_chat_export(Request({"content": "Checking now\n\n# Real",
                                                            "title": "Report"}), "md"))
    assert exported.media_type.startswith("text/markdown")


def test_browse_schema_namespace_and_about_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_browse_about_edges")
    assert module._within(wiki, tmp_path.resolve()) is False
    assert module._top_dirs() == []
    assert module._disp_ts("") == ""
    assert module._disp_ts("2026-08-04T12:34:56Z") == "2026-08-04 12:34:56"
    assert module._disp_ts("2026-08-04") == "2026-08-04"
    assert module._disp_ts("unknown") == "unknown"
    assert module._read_head(tmp_path / "missing.md") == ""

    schema = tmp_path / "schema.yaml"
    monkeypatch.setattr(module, "_governing_schema_path", lambda: schema)
    schema.write_text("exclude: [wiki/raw/, dashboards]\n"
                      "display_groups:\n  Actors: [actor, '']\n  Empty: []\n"
                      "rail_top_section: {label: Outputs, namespaces: [dashboards, missing]}\n")
    module._EXCLUDE_CACHE = (float("-inf"), frozenset())
    module._GROUPS_CACHE = (float("-inf"), [])
    module._RAILTOP_CACHE = (float("-inf"), ("", ()))
    assert module._excluded_dirs() == frozenset({"raw"})
    assert module._excluded_dirs() == frozenset({"raw"})
    assert module._display_groups() == [("Actors", frozenset({"actor"}))]
    assert module._display_groups() == [("Actors", frozenset({"actor"}))]
    assert module._rail_top_section() == ("Outputs", ("dashboards", "missing"))
    assert module._rail_top_section() == ("Outputs", ("dashboards", "missing"))

    schema.write_text("[broken")
    module._EXCLUDE_CACHE = (float("-inf"), frozenset())
    module._GROUPS_CACHE = (float("-inf"), [])
    module._RAILTOP_CACHE = (float("-inf"), ("", ()))
    assert module._excluded_dirs() == frozenset()
    assert module._display_groups() == []
    assert module._rail_top_section() == ("", ())

    briefings = wiki / "briefings"
    briefings.mkdir()
    module._RAILTOP_CACHE = (float("-inf"), ("", ()))
    assert module._rail_top_section() == ("Briefs", ("briefings",))
    about = briefings / "_about.md"
    about.write_text("---\ntitle: About\n---\nDescription")
    assert "Description" in module._ns_about("briefings")
    assert module._ns_about("") == ""
    assert module._ns_about("missing") == ""
    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: (_ for _ in ()).throw(
        OSError("raced")) if path == about else original_read_text(path, *args, **kwargs))
    assert module._ns_about("briefings") == ""
    monkeypatch.setattr(Path, "read_text", original_read_text)

    derived = wiki / "dashboards/a.md"
    derived.parent.mkdir()
    derived.write_text("---\ntype: dashboard\n---\n")
    missing = wiki / "dashboards/missing.md"
    assert module._dir_is_derived([missing, derived]) is True
    assert module._dir_is_derived([]) is False

    monkeypatch.setattr(module, "_display_groups", lambda: [("Actors", frozenset({"actor"}))])
    monkeypatch.setattr(module, "_pages_of_types", lambda types: [{"path": "entities/a"}] if types else [])
    assert module.api_pages(group="Actors")["pages"]
    with pytest.raises(module.HTTPException):
        module.api_pages(group="Missing")
    for bad in ("../x", ".hidden", "/root"):
        with pytest.raises(module.HTTPException):
            module.api_pages(dir=bad, group="")

    (tmp_path / "pack.yaml").write_text(
        "name: Demo\nversion: '1.2'\ndescription: Desc\nmission: Mission\nproject_url: https://demo\n")
    (tmp_path / "CLAUDE.md").write_text("## Installed domain: alpha\n## Installed domain: beta\n")
    sub = wiki / "sub"
    sub.mkdir()
    (sub / "schema.yaml").write_text("types: {}\n")
    effective = tmp_path / ".okengine/extensions-effective.yaml"
    effective.parent.mkdir()
    effective.write_text("effective:\n- {id: ext.b, name: B}\n- ext.a\n")
    runtime = tmp_path / ".hermes-data/engine-runtime.yaml"
    runtime.parent.mkdir()
    runtime.write_text("engine_release: '2.0'\nhermes_pin: abc\n")
    info = module._about_info()
    assert info["vault"] == "Demo" and info["installed_domains"] == ["alpha", "beta"]
    assert [item["id"] for item in info["extensions"]] == ["ext.a", "ext.b"]
    assert info["engine_version"] == "2.0" and info["sub_domains"] == ["sub"]
    monkeypatch.setattr(module, "_about_info", lambda: {"vault": "Demo"})
    monkeypatch.setattr(module, "_AGENT_API", "http://agent")
    monkeypatch.setattr(module, "_AGENT_KEY", "key")
    assert module.api_about() == {"vault": "Demo", "chat_enabled": True}
    monkeypatch.setenv("BROKEN_INT", "bad")
    assert module._intenv("BROKEN_INT", 5, 2) == 5
    monkeypatch.setenv("BROKEN_INT", "1")
    assert module._intenv("BROKEN_INT", 5, 2) == 2


def test_chip_renderer_group_link_assessment_and_empty_boundaries(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_chip_edges")
    monkeypatch.setattr(module, "_ds_pairs", lambda _box, _rows: [])
    assert module._v_chips({}, []) == ""

    pairs = [("Opaque", 2, False, "G1"), ("Unknown", 1, True, "G2")]
    monkeypatch.setattr(module, "_ds_pairs", lambda _box, _rows: pairs)
    original_link_page_map = module._link_page_map
    monkeypatch.setattr(module, "_link_page_map", lambda _box: {
        "G1": {"path": "entities/g1", "label": "Named actor"}
    })
    grouped = module._v_chips({"group_by": "actor"}, [{}], drill=("x", 0))
    assert "Named actor" in grouped and "data-dpage" in grouped and "um" in grouped
    plain = module._v_chips({}, [{}], drill=("x", 0))
    assert "Opaque" in plain and "data-dpage" in plain
    assessed = module._v_chips({"assessment": {}}, [{}], drill=("x", 0))
    assert "Assessment-backed rollup" in assessed

    monkeypatch.setattr(module, "_link_page_map", original_link_page_map)
    monkeypatch.setattr(module, "_load_dir", lambda _dir: [
        {"id": "", "_sub": "entities", "_name": "ignored"},
        {"id": "G1", "title": "Name", "_sub": "entities", "_rel": "g/g1"},
    ])
    assert module._link_page_map({"link_page": {"dir": "entities", "by": "id",
                                                  "label_field": "title"}}) == {
        "G1": {"path": "entities/g/g1", "label": "Name"}
    }


def test_application_tab_drill_and_dashboard_error_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    dashboards = wiki / "dashboards"
    dashboards.mkdir(parents=True)
    module = _load(tmp_path, monkeypatch, "cockpit_tab_dashboard_edges")
    monkeypatch.setattr(module, "cockpit_config", lambda: {
        "application": None, "tab_defs": {}, "tab_aliases": {}, "dashboards": []
    })
    with pytest.raises(module.HTTPException):
        module.api_application()
    with pytest.raises(module.HTTPException):
        module.api_tab("missing")

    application = {"profile": "intel", "profile_version": "1", "propositions": [],
                   "surfaces": {}, "queues": {}, "success_measures": {}}
    boxes = [
        {"title": "Application", "view": "application"},
        {"title": "Help", "view": "application-help", "summary": "Guide"},
        {"title": "Control", "view": "operation-control", "empty": "Unavailable"},
        {"title": "Bad", "view": "unsupported", "section": "Section"},
    ]
    monkeypatch.setattr(module, "cockpit_config", lambda: {
        "application": application,
        "tab_defs": {"main": {"label": "Main", "boxes": boxes}},
        "tab_aliases": {"alias": "main"}, "dashboards": [],
    })
    monkeypatch.setattr(module, "_v_operation_control", lambda _box: "")
    assert module.api_application()["profile"] == "intel"
    tab = module.api_tab("alias")
    assert tab["canonical_key"] == "main" and len(tab["boxes"]) == 4
    assert tab["boxes"][2]["meta"] == "not configured"
    assert tab["boxes"][3]["meta"] == "configuration error"
    assert tab["boxes"][3]["layout_section"] == "Section"
    assert tab["boxes"][3]["section"] == "Section"

    with pytest.raises(module.HTTPException):
        module.api_drill("missing", 0, "", -1)
    monkeypatch.setattr(module, "cockpit_config", lambda: {
        "tab_defs": {"x": {"boxes": [
            {"view": "bignums", "items": []}, {"view": "coverage"}, {"view": "table"},
            {"view": "doc"},
        ]}}
    })
    with pytest.raises(module.HTTPException):
        module.api_drill("x", 0, "", 0)                 # bignums item index out of range
    with pytest.raises(module.HTTPException):
        module.api_drill("x", 1, "", -1)                # coverage with no `versus` group
    # okengine#564: a table IS drillable. With no dataset configured it enumerates nothing —
    # an empty list, not an error: the box is well-formed, it simply has no rows behind it.
    empty = module.api_drill("x", 2, "", -1)
    assert empty["count"] == 0 and empty["pages"] == [] and empty["truncated"] is False
    with pytest.raises(module.HTTPException):
        module.api_drill("x", 3, "", -1)                # `doc` has no row set to enumerate

    bad = dashboards / "bad.md"
    bad.write_text("---\ntitle: Bad\n---\n")
    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: (_ for _ in ()).throw(
        OSError("raced")) if path == bad else original_read_text(path, *args, **kwargs))
    monkeypatch.setattr(module, "cockpit_config", lambda: {
        "dashboards": ["bad", {"group": "Pinned", "items": [None, {}, {"path": "dashboards/bad"}]}]
    })
    result = module.api_dashboards()
    assert result["groups"][0]["group"] == "Pinned"
    monkeypatch.setattr(module, "cockpit_config", lambda: {"dashboards": []})
    assert module.api_dashboards() == {"groups": []}


def test_page_reference_metadata_and_quality_helper_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_page_helper_edges")
    schema = tmp_path / "schema.yaml"
    monkeypatch.setattr(module, "_governing_schema_path", lambda: schema)
    schema.write_text("[broken")
    module._SRC_REL_CACHE = (float("-inf"), {})
    module._TYPE_REQ_CACHE = (float("-inf"), {})
    assert module._source_reliability() == {}
    assert module._type_required_fields() == {}
    schema.write_text("source_registry:\n  feed: {reliability: A}\n  empty: {}\n"
                      "types:\n  actor: {required: [type, name]}\n  scalar: bad\n")
    module._SRC_REL_CACHE = (float("-inf"), {})
    module._TYPE_REQ_CACHE = (float("-inf"), {})
    assert module._source_reliability() == {"feed": "A"}
    assert module._source_reliability() == {"feed": "A"}
    assert module._type_required_fields() == {"actor": ["name"]}
    assert module._type_required_fields() == {"actor": ["name"]}

    assert module._url_label("http://[") == "http://["
    assert module._ref_target(42) is None
    assert module._ref_target("entity") is None
    assert module._ref_target("entities/a") is None
    entity = wiki / "entities/a/a.md"
    entity.parent.mkdir(parents=True)
    entity.write_text("---\ntype: actor\ntitle: A\n---\n")
    assert module._ref_target("entities/a") == "entities/a/a"
    second = wiki / "entities/b/a.md"
    second.parent.mkdir(parents=True)
    second.write_text("---\ntype: actor\n---\n")
    assert module._ref_target("entities/a") is None

    assert module._meta_panel_items("bad") == {"primary": [], "secondary": []}
    panel = module._meta_panel_items({"country": "A", "name": "", "extra": "value"},
                                     order=["country"])
    assert panel["primary"] and panel["secondary"]

    monkeypatch.setattr(module, "_source_reliability", lambda: {})
    assert module._evidence_sources({"sources": ["", "prose"]}) == [
        {"name": "prose", "page": None, "reliability": "", "date": ""}
    ]
    assert module._review_reasons({"conflicts": [None, {"field": "type"}]}, "")[0]["code"] == "conflict"
    grounding = module._review_reasons({}, "## Grounding check\nunsupported")
    assert grounding[0]["code"] == "grounding"

    module._OBS_INDEX_CACHE = (float("-inf"), {})
    observations = wiki / "observations"
    observations.mkdir()
    (observations / "_hidden.md").write_text("---\ncanonical: a\n---\n")
    (observations / "empty.md").write_text("---\ntype: observation\n---\n")
    (observations / "valid.md").write_text(
        "---\ntype: observation\ncanonical: A\nsource: feed\n---\n")
    result = module._observations_by_canonical()
    assert result["a"][0]["source"] == "feed" and module._observations_by_canonical() is result

    badges = module._quality_badges(
        {"type": "source", "url": "https://example.test", "updated": "invalid"},
        "substantial " * 100, "source", {}, [],
    )
    assert not any(badge["label"] == "no sources" for badge in badges)


def test_lifespan_stream_pdf_page_and_about_failure_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_lifecycle_page_edges")
    calls = []
    monkeypatch.setattr(module, "_warm_initial_tab_datasets", lambda: calls.append("initial"))
    monkeypatch.setattr(module, "_schedule_remaining_tab_warmup", lambda: calls.append("remaining"))

    async def lifespan():
        async with module._lifespan(None):
            calls.append("inside")

    asyncio.run(lifespan())
    assert calls == ["initial", "remaining", "inside"]

    deck = wiki / "briefings/deck-2026-08-04.md"
    deck.parent.mkdir()
    deck.write_text("# Deck")
    rendered = tmp_path / "rendered.pdf"
    rendered.write_bytes(b"pdf")
    monkeypatch.setattr(module, "_streams", lambda: {
        "deck": {"dir": "briefings", "pdf": True}
    })
    monkeypatch.setattr(module, "_stream_pages", lambda _cfg: [deck])
    monkeypatch.setattr(module, "_render_deck_pdf", lambda _path: rendered)
    response = module.api_stream_pdf("deck", "2026-08-04")
    assert response.media_type == "application/pdf"
    monkeypatch.setattr(module, "_render_deck_pdf", lambda _path: None)
    with pytest.raises(module.HTTPException):
        module.api_stream_pdf("deck", "2026-08-04")

    for path, status in (("../bad", 400), ("missing", 404)):
        with pytest.raises(module.HTTPException) as error:
            module.api_page(path)
        assert error.value.status_code == status
    (wiki / "entities/a").mkdir(parents=True)
    (wiki / "entities/b").mkdir(parents=True)
    (wiki / "entities/a/same.md").write_text("one")
    (wiki / "entities/b/same.md").write_text("two")
    with pytest.raises(module.HTTPException) as error:
        module.api_page("same")
    assert error.value.status_code == 409
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (wiki / "blocked.md").symlink_to(outside)
    with pytest.raises(module.HTTPException) as error:
        module.api_page("blocked")
    assert error.value.status_code == 403

    malformed = tmp_path / "pack.yaml"
    malformed.write_text("[broken")
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("domain")
    original_read_text = Path.read_text
    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: (_ for _ in ()).throw(
        OSError("raced")) if path == claude else original_read_text(path, *args, **kwargs))
    monkeypatch.setattr(Path, "iterdir", lambda path: (_ for _ in ()).throw(OSError("raced"))
                        if path == wiki else original_iterdir(path))
    info = module._about_info()
    assert info["vault"] == "" and info["installed_domains"] == [] and info["sub_domains"] == []


def test_ops_summary_warning_unknown_and_read_failure_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "dashboards").mkdir(parents=True)
    (wiki / "operational").mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_ops_summary_edges")
    (wiki / "dashboards/fleet-health.md").write_text("ok: 1 · stale: 0 · errored: 0")
    (wiki / "operational/deployment-validation.md").write_text("**PASS** — 0 fail · 2 warn")
    monkeypatch.setattr(module, "_load_dir", lambda _sub: [
        {"_sub": "sources", "_name": "old", "created": "2020-01-01"}
    ])
    monkeypatch.setattr(module, "_ops_groups", lambda: [])
    result = module._ops_summary()
    assert result["state"] == "warning"
    assert any(metric["label"] == "validation warnings" for metric in result["metrics"])

    (wiki / "dashboards/fleet-health.md").write_text("ok: 1")
    (wiki / "operational/deployment-validation.md").write_text("no result")
    monkeypatch.setattr(module, "_load_dir", lambda _sub: [])
    result = module._ops_summary()
    assert result["state"] == "unknown"

    queue = wiki / "_review-queue.md"
    queue.write_text("- item")
    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: (_ for _ in ()).throw(
        OSError("raced")) if path == queue else original_read_text(path, *args, **kwargs))
    assert module._ops_summary()["state"] == "unknown"


def test_remaining_metadata_navigation_and_filesystem_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_remaining_fs_edges")

    assert module._MetaValues()["missing"] == "{missing}"
    assert module._dataset_meta({"meta_template": "{missing}"}, "table", []) == "{missing}"
    assert module._dataset_meta({"meta_template": "{"}, "table", []) == "{"
    assert module._ref_target("entities/missing") is None
    monkeypatch.setattr(module, "WIKI", tmp_path / "absent-wiki")
    assert module._ref_target("entities/missing") is None
    assert module._top_dirs() == []
    monkeypatch.setattr(module, "WIKI", wiki)

    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: (_ for _ in ()).throw(OSError("raced"))
                        if path == wiki else original_iterdir(path))
    assert module._content_dirs() == []
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    assert module._panel_for({"panel": {"kind": "two-axis"}}, "<!-- panel-svg -->") is None
    assert module._meta_values("entities/missing") == [{"text": "entities/missing"}]
    assert module._shape_conflicts({"conflicts": [None]}) == []
    assert module._skip_backlink_src("INDEX.md") is True
    monkeypatch.setattr(module, "_excluded_dirs", lambda: frozenset({"raw"}))
    assert module._scan_dir("raw") == []

    hidden = wiki / "_hidden"
    hidden.mkdir()
    visible = wiki / "visible"
    visible.mkdir()
    assert module.api_tree()["dirs"] == []
    assert module.api_pages(dir="visible", group="") == {
        "dir": "visible", "about": "", "pages": []
    }


def test_remaining_dashboard_ops_and_review_cache_boundaries(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    dashboards = wiki / "dashboards"
    operational = wiki / "operational"
    dashboards.mkdir(parents=True)
    operational.mkdir()
    (dashboards / "_hidden.md").write_text("hidden")
    (operational / "_reserved.md").write_text("hidden")
    (operational / "custom.md").write_text("---\ntitle: Custom\n---\n")
    module = _load(tmp_path, monkeypatch, "cockpit_remaining_ops_edges")

    monkeypatch.setattr(module, "cockpit_config", lambda: {
        "dashboards": [{"group": "Pinned", "items": []}]
    })
    assert module.api_dashboards()["groups"] == [{"group": "Pinned", "items": []}]
    assert any(group["group"] == "Operational log" for group in module._ops_groups())
    assert module._ops_meta("operational/missing") == {}

    module._review_snapshot_cache = (float("-inf"), [], [])
    expected = ([{"subject": "cached"}], [{"id": "record"}])

    class PopulateReviewCache:
        def __enter__(self):
            module._review_snapshot_cache = (time.monotonic(), *expected)
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(module, "_review_snapshot_lock", PopulateReviewCache())
    assert module._review_queue_snapshot() == expected

    module._assessment_subject_cache = (float("-inf"), {})
    assessment_expected = {"entities/a": [{"path": "assessments/a"}]}

    class PopulateAssessmentCache:
        def __enter__(self):
            module._assessment_subject_cache = (time.monotonic(), assessment_expected)
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(module, "_assessment_subject_lock", PopulateAssessmentCache())
    assert module._assessment_subject_index() == assessment_expected


def test_review_queue_global_scope_and_empty_paths(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_review_queue_global_edges")
    monkeypatch.setattr(module, "_review_queue_snapshot", lambda: (
        [{"type": "actor"}, {"type": "malware"}], []
    ))
    assert "Actor 1" in module._v_review_queue({"review_types": ["actor"]})
    monkeypatch.setattr(module, "_review_queue_snapshot", lambda: ([], []))
    assert "No records currently require review" in module._v_review_queue({})


def test_basic_auth_is_installed_when_password_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_READER_PASSWORD", "secret")
    module = _load(tmp_path, monkeypatch, "cockpit_basic_auth_enabled")
    assert any(item.cls is module._BasicAuth for item in module.app.user_middleware)


def test_final_statement_boundaries_are_observable(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "sources").mkdir(parents=True)
    (wiki / "dashboards").mkdir()
    (wiki / "operational").mkdir()
    (wiki / "entities").mkdir()
    (wiki / "entities/a.md").write_text("---\ntitle: A\n---\n")
    module = _load(tmp_path, monkeypatch, "cockpit_final_statement_edges")

    (wiki / "dashboards/fleet-health.md").write_text("ok: 1 · stale: 0 · errored: 0")
    (wiki / "operational/deployment-validation.md").write_text("**PASS** — 0 fail · 0 warn")
    monkeypatch.setattr(module, "_load_dir", lambda sub: [{
        "_sub": "sources", "_name": "old", "created": "2020-01-01"
    }, {
        "_sub": "sources", "_name": "invalid", "created": "not-a-date"
    }] if sub == "sources" else [])
    monkeypatch.setattr(module, "_ops_groups", lambda: [])
    assert module._ops_summary()["state"] == "warning"

    assert module._meta_values("entities/a") == [{"text": "entities/a", "page": "entities/a"}]
    original_rglob = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("raced")))
    assert module._ref_target("entities/missing") is None
    monkeypatch.setattr(Path, "rglob", original_rglob)

    rows = [
        {"id": "shared", "_name": "one", "_sub": "entities"},
        {"id": "shared", "_name": "two", "_sub": "entities"},
        {"id": "shared", "_name": "three", "_sub": "entities"},
    ]
    monkeypatch.setattr(module, "_load_dir", lambda _sub: rows)
    module._ID_INDEX_CACHE = (float("-inf"), {})
    assert "shared" not in module._id_index()

    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    monkeypatch.setattr(module, "_review_records", lambda: [])
    monkeypatch.setattr(Path, "rglob", lambda path, _pattern: [outside] if path == wiki else [])
    assert module._build_review_snapshot() == ([], [])
    monkeypatch.setattr(Path, "rglob", original_rglob)

    assert module._strip_report_preamble("\n\nChecking now\n\nBody") == "Body"

    stdout = "\n".join(f"{wiki / f'page-{i}.md'}:1:term" for i in range(1500))
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: types.SimpleNamespace(stdout=stdout))
    assert module.api_search("term")["total"] == 1500


def test_remaining_collection_and_render_branch_pairs(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_remaining_collection_branches")

    monkeypatch.setattr(module, "_streams", lambda: {"daily": {"dir": "briefings"}})
    monkeypatch.setattr(module, "_stream_pages", lambda _cfg: ["undated.md", "also-undated.md"])
    assert module._stream_dates("daily") == []

    assert module._ds_pairs({"group_by": "kind"}, [{"kind": "Unknown"}]) == []
    assert module._ds_pairs(
        {"group_by": "kind", "labels": {"known": "Known"}, "bucket_unmapped": True},
        [{"kind": "known"}],
    ) == [("Known", 1, False, "known")]
    assert module._markdown_section("# Last", "Last") == ""

    monkeypatch.setattr(module, "_latest_doc", lambda _box: (
        {}, "short body", "briefings/x", "x"
    ))
    html, _ = module._v_doc_summary({"section": "Missing"})
    assert "short body" in html and " …" not in html
    assert module._clean_markdown("# Existing", "Title") == "# Existing\n"

    monkeypatch.setattr(module, "cockpit_config", lambda: {"dashboards": [{"group": "Only"}]})
    assert module.api_dashboards() == {"groups": [{"group": "Only", "items": []}]}
    monkeypatch.setattr(module, "cockpit_config", lambda: {"dashboards": []})
    assert module.api_dashboards() == {"groups": []}

    monkeypatch.setattr(module, "cockpit_config", lambda: {"tab_defs": {"x": {"boxes": [{
        "view": "bignums", "items": [{"label": "Item", "dataset": {"dir": "records"}}]
    }]}}})
    monkeypatch.setattr(module, "_configured_rows", lambda _box: [])
    assert module.api_drill("x", 0, "", 0)["title"] == "Item"

    module._STALE_DAYS = 0
    assert not any(badge["label"].startswith("stale") for badge in module._quality_badges(
        {"updated": "2020-01-01"}, "substantial " * 100, "page", {}, []
    ))


def test_remaining_config_cache_and_backlink_branch_pairs(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_remaining_config_branches")
    schema = tmp_path / "schema.yaml"
    monkeypatch.setattr(module, "_governing_schema_path", lambda: schema)

    schema.write_text("exclude: ['']\ndisplay_groups: scalar\nrail_top_section: scalar\n")
    module._EXCLUDE_CACHE = (float("-inf"), frozenset())
    module._GROUPS_CACHE = (float("-inf"), [])
    module._RAILTOP_CACHE = (float("-inf"), ("", ()))
    module._BL_DROP_CACHE = (float("-inf"), None)
    assert module._excluded_dirs() == frozenset()
    assert module._display_groups() == []
    assert module._rail_top_section() == ("", ())
    assert module._backlink_drop_dirs() == frozenset({"sources"})

    schema.write_text("backlink_drop: ['', wiki/raw/nested]\n")
    module._BL_DROP_CACHE = (float("-inf"), None)
    assert module._backlink_drop_dirs() == frozenset({"raw"})

    monkeypatch.setattr(module, "_governing_schema_path", lambda: tmp_path / "missing-schema.yaml")
    module._GROUPS_CACHE = (float("-inf"), [])
    assert module._display_groups() == []

    broken = wiki / "broken.md"
    broken.write_text("---\nunterminated")
    plain = wiki / "plain.md"
    plain.write_text("---\ntype: page\n---\n# Heading")
    assert module._backlink_title("broken") == "broken"
    assert module._backlink_title("plain") == "Heading"
    (wiki / "links.md").write_text("[[https://example.test]]")
    assert module._scan_forward_refs()[0]["references"] == []

    assert module._scan_dir("missing") == []
    no_type = wiki / "notype.md"
    no_type.write_text("body")
    assert module._dir_is_derived([no_type]) is False
    monkeypatch.setattr(module, "WIKI", tmp_path / "missing-wiki")
    assert module.api_tree()["dirs"] == []


def test_remaining_single_flight_and_index_branch_pairs(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    module = _load(tmp_path, monkeypatch, "cockpit_remaining_singleflight_branches")

    application = {
        "profile": "threat-informed-detection",
        "roles": {"role": [{"namespace": "empty-path"}]},
    }
    monkeypatch.setattr(module, "cockpit_config", lambda: {"application": application})
    monkeypatch.setattr(module, "_load_dir", lambda namespace: [{}] if namespace in {
        "empty-path", "defensive-actions"
    } else [])
    data = module._tid_application_data()
    assert data["index"] == {}

    module._review_snapshot_cache = (time.monotonic() - module._REVIEW_SNAPSHOT_TTL - 1, [], [])
    module._review_snapshot_refreshing = True
    assert module._review_queue_snapshot() == ([], [])

    # `ts` must be stale RELATIVE TO time.monotonic(), not an absolute 0.0. The cache check is
    # `now - ts <= _BACKLINKS_TTL` and the TTL defaults to 86400, so a literal 0.0 only reads as
    # stale once monotonic() exceeds 24h -- i.e. once the HOST has been up a day. On a long-uptime
    # runner this passed; after a reboot it returned the "old" map without ever taking the lock,
    # so the single-flight re-check under test never ran. That is the fresh-host cache trap the
    # production code is already guarded against, reproduced in the test that guards it.
    module._BACKLINKS = {"map": {"old": []}, "ts": time.monotonic() - module._BACKLINKS_TTL - 1}

    class PopulateBacklinkCache:
        def __enter__(self):
            module._BACKLINKS = {"map": {"fresh": []}, "ts": time.monotonic()}
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(module, "_BL_LOCK", PopulateBacklinkCache())
    assert module._load_backlinks() == {"fresh": []}

    monkeypatch.setattr(module, "_review_records", lambda: [{"requested_at": "x"}])
    monkeypatch.setattr(Path, "rglob", lambda *_args: [])
    assert module._build_review_snapshot()[1] == [{"requested_at": "x"}]

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("plain")
    monkeypatch.setattr(module, "STATIC", static)
    assert module.index() == "plain"
