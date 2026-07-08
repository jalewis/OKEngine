"""okengine-cockpit config loader — the ONLY place domain knowledge enters the
cockpit (an optional `cockpit:` block in the vault's schema.yaml). These tests pin
the domain-agnostic defaults (zero-config) and the parse of a populated block,
including the rule that the watchlist + competitors tabs stay hidden until a
`watchlist:` config exists."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-cockpit" / "app.py"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = m
    spec.loader.exec_module(m)
    return m


def _write_schema(vault: Path, body: str) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "schema.yaml").write_text(body, encoding="utf-8")


# ── zero-config defaults ─────────────────────────────────────────────────────
def test_defaults_with_no_cockpit_block(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    vault = tmp_path / "acme-research"
    vault.mkdir()
    cfg = m.load_cockpit_config(vault)

    # title defaults to the titleized vault dir name
    assert cfg["title"] == "Acme Research"
    # one generic "Recent briefings" stream over briefings/
    assert [s["key"] for s in cfg["streams"]] == ["briefings"]
    assert cfg["streams"][0]["dir"] == "briefings"
    assert cfg["streams"][0]["pdf"] is False
    # generic default tabs, no tracker tabs
    assert cfg["tabs"] == ["home", "briefings", "predictions", "dashboards"]
    # no watchlist/competitors config
    assert cfg["watchlist"] is None
    assert cfg["competitors"] == []
    # predictions source dir default
    assert cfg["predictions_dirs"] == ["predictions"]
    # dashboards auto-listed (no curated groups)
    assert cfg["dashboards"] is None


def test_defaults_when_schema_present_but_no_cockpit_key(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    vault = tmp_path / "v"
    _write_schema(vault, "types:\n  - entity\nexclude:\n  - operational\n")
    cfg = m.load_cockpit_config(vault)
    assert cfg["watchlist"] is None
    assert cfg["tabs"] == ["home", "briefings", "predictions", "dashboards"]
    assert [s["key"] for s in cfg["streams"]] == ["briefings"]


# ── tracker tabs hidden without a watchlist config ───────────────────────────
def test_tracker_tabs_dropped_without_watchlist(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    vault = tmp_path / "v"
    # tabs explicitly list watchlist + competitors, but there's no watchlist block
    _write_schema(vault, (
        "cockpit:\n"
        "  tabs: [briefings, watchlist, predictions, competitors, dashboards]\n"
    ))
    cfg = m.load_cockpit_config(vault)
    assert cfg["watchlist"] is None
    assert "watchlist" not in cfg["tabs"]
    assert "competitors" not in cfg["tabs"]
    # EXPLICIT tabs are respected verbatim (minus the dropped trackers) — home is only a
    # DEFAULT-tabs addition; a pack that lists its own tabs opts into home by listing it.
    assert cfg["tabs"] == ["briefings", "predictions", "dashboards"]


# ── full populated block ─────────────────────────────────────────────────────
SAMPLE = """
cockpit:
  title: "Acme Intelligence"
  streams:
    - {key: pdb, label: "Daily brief", dir: briefings, type: daily-brief}
    - {key: weekly, label: "Weekly review", dir: weekly, glob: "*-week-in-review.md"}
    - {key: deck, label: "Weekly deck", dir: briefings, glob: "weekly-deck-2*.md", pdf: true}
  watchlist:
    entity_types: [vendor, product]
    tier_field: competitor_tier
    rating_field: threat_level
    moved_field: last_material_move
    acquirer_field: acquirer_candidate
    labels:
      section: "Competitive watchlist"
      entity: "Competitor"
      rating: "Threat"
  competitors:
    - {key: movement, path: "dashboards/latest-competitor-movement-ledger.md"}
  predictions: [predictions, partner-predictions]
  tabs: [briefings, watchlist, predictions, competitors, dashboards]
  dashboards:
    - group: "Today"
      items:
        - {path: "dashboards/latest-pdb", title: "Daily PDB", desc: "what changed"}
"""


def test_full_block_parse(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    vault = tmp_path / "v"
    _write_schema(vault, SAMPLE)
    cfg = m.load_cockpit_config(vault)

    assert cfg["title"] == "Acme Intelligence"

    # streams: type vs glob both preserved; pdf flag honoured
    by_key = cfg["streams_by_key"]
    assert by_key["pdb"]["type"] == "daily-brief" and "glob" not in by_key["pdb"]
    assert by_key["weekly"]["glob"] == "*-week-in-review.md"
    assert by_key["deck"]["pdf"] is True

    # watchlist: all field names + labels come from config (no hardcoded domain terms)
    wl = cfg["watchlist"]
    assert wl is not None
    assert wl["entity_types"] == ["vendor", "product"]
    assert wl["tier_field"] == "competitor_tier"
    assert wl["rating_field"] == "threat_level"
    assert wl["moved_field"] == "last_material_move"
    assert wl["acquirer_field"] == "acquirer_candidate"
    assert wl["labels"]["section"] == "Competitive watchlist"
    assert wl["labels"]["entity"] == "Competitor"
    assert wl["labels"]["rating"] == "Threat"
    # trends sub-tracker defaults ON when watchlist present
    assert wl["trends"] == {"concept_dir": "concepts", "type": "trend"}

    # competitors + predictions + dashboards
    assert cfg["competitors"] == [{"key": "movement",
                                   "path": "dashboards/latest-competitor-movement-ledger.md"}]
    assert cfg["predictions_dirs"] == ["predictions", "partner-predictions"]
    assert cfg["dashboards"][0]["group"] == "Today"

    # with a watchlist config, the tracker tabs survive in the configured order
    assert cfg["tabs"] == ["briefings", "watchlist", "predictions", "competitors", "dashboards"]


def test_watchlist_defaults_when_minimal(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    vault = tmp_path / "v"
    # a bare `watchlist: {}` still lights up the tab with generic defaults
    _write_schema(vault, "cockpit:\n  watchlist: {}\n  tabs: [briefings, watchlist]\n")
    cfg = m.load_cockpit_config(vault)
    wl = cfg["watchlist"]
    assert wl is not None
    assert wl["entity_dir"] == "entities"
    assert wl["entity_types"] == []          # empty => all types
    assert wl["tier_field"] == "tier"
    assert wl["rating_field"] is None        # no rating column unless configured
    assert wl["moved_field"] == "updated"
    assert wl["acquirer_field"] is None
    assert wl["labels"]["section"] == "Watchlist"
    assert "watchlist" in cfg["tabs"]


def test_trends_can_be_disabled(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    vault = tmp_path / "v"
    _write_schema(vault, "cockpit:\n  watchlist:\n    trends: false\n")
    cfg = m.load_cockpit_config(vault)
    assert "trends" not in cfg["watchlist"]


def test_malformed_schema_falls_back_to_defaults(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    vault = tmp_path / "v"
    _write_schema(vault, "cockpit: : : not yaml [\n")
    cfg = m.load_cockpit_config(vault)
    assert cfg["watchlist"] is None
    assert cfg["tabs"] == ["home", "briefings", "predictions", "dashboards"]


def test_about_parity_with_reader(tmp_path, monkeypatch):
    """The cockpit's /api/about carries the same purpose/composition fields as the
    reader's (kept in sync by hand — this is the contract test for the pair)."""
    vault = tmp_path
    (vault / "wiki" / "doctrine").mkdir(parents=True)
    (vault / "wiki" / "doctrine" / "schema.yaml").write_text("types: {}\n")
    (vault / "pack.yaml").write_text("name: okpack-x\ndescription: D\nmission: M\n")
    (vault / "CLAUDE.md").write_text("## Installed domain: x (okpack-x)\n")
    (vault / ".okengine").mkdir()
    (vault / ".okengine" / "extensions.yaml").write_text("enabled:\n  okengine.events: {}\n")
    m = _load(tmp_path, monkeypatch)
    a = m._about_info()
    for k, want in (("description", "D"), ("mission", "M"),
                    ("installed_domains", ["x (okpack-x)"]),
                    ("sub_domains", ["doctrine"]),
                    ("extensions", [{"id": "okengine.events", "name": "okengine.events",
                                     "description": ""}])):
        assert a[k] == want, (k, a[k])


def test_humanize_preserves_acronyms(tmp_path, monkeypatch):
    """A vault dir name / watchlist tab key humanizes for display without mangling common
    initialisms — ai-research -> 'AI Research' (not 'Ai Research'), iot -> 'IoT' (operator report:
    the pack-side theme titles had the same bug; this is the engine display-fallback half)."""
    m = _load(tmp_path, monkeypatch)
    cases = {"ai-research": "AI Research", "iot-fleet": "IoT Fleet", "my-vault": "My Vault",
             "api-monitoring": "API Monitoring", "saas-metrics": "SaaS Metrics", "okcti": "Okcti"}
    for slug, want in cases.items():
        assert m._humanize(slug) == want, (slug, m._humanize(slug))
