"""Signal-role producer and cross-surface contract (#221)."""
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"


def _load(name):
    sys.path.insert(0, str(CRON))
    spec = importlib.util.spec_from_file_location(f"{name}_test", CRON / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_classifier_is_conservative_and_first_match_wins():
    mod = _load("signal_classifier")
    today = date(2026, 7, 16)
    assert mod.classify("marketing/internal.md", {}, today=today)[0] == \
        "marketing-positioning"
    assert mod.classify("sources/old.md", {"published": "2020-01-01",
                                          "tags": ["funding"]}, today=today)[0] == \
        "historical-baseline"
    assert mod.classify("sources/new.md", {"published": "2026-07-01",
                                          "tags": ["funding"]}, today=today)[0] == \
        "current-market-signal"
    assert mod.classify("sources/unknown.md", {}, today=today)[0] == "entity-enrichment"


def test_lane_guards_writes_and_surfaces_rejections(tmp_path, capsys):
    schema = {
        "types": {"source": {}},
        "enums": {"tlp": ["CLEAR", "GREEN"], "signal_class": [
            "current-market-signal", "historical-baseline",
            "marketing-positioning", "entity-enrichment"]},
        "field_enums": {"tlp": {"enum": "tlp"},
                        "signal_class": {"enum": "signal_class"}},
    }
    artifact = tmp_path / ".okengine" / "composed-schema.yaml"
    artifact.parent.mkdir()
    artifact.write_text(yaml.safe_dump(schema))
    sources = tmp_path / "wiki" / "sources"
    sources.mkdir(parents=True)
    good = sources / "good.md"
    good.write_text("---\ntype: source\ntlp: clear\ntags: [funding]\n---\nbody\n")
    bad = sources / "bad.md"
    bad.write_text("---\ntype: source\ntlp: junk\n---\nbody\n")

    counts = _load("classify_sources").run(tmp_path, apply=True)
    good_fm = yaml.safe_load(good.read_text().split("---", 2)[1])
    assert good_fm["tlp"] == "CLEAR"
    assert good_fm["signal_class"] == "current-market-signal"
    assert "signal_class" not in bad.read_text()
    assert counts["classified"] == 1 and counts["rejected"] == 1
    assert "signal-class-reject: sources/bad.md" in capsys.readouterr().err


def test_consumer_has_scheduled_producer_and_declared_vocabulary():
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    producer = next(job for job in jobs if job["name"] == "classify-new-sources")
    assert producer["no_agent"] is True and producer["script"] == "classify_sources.py"
    consumer = (CRON / "source_portfolio_watch.py").read_text()
    assert "signal_class" in consumer
    assert set(_load("signal_classifier").ALL_CLASSES) == {
        "current-market-signal", "historical-baseline",
        "marketing-positioning", "entity-enrichment",
    }
