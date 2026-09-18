"""Signal-role producer and cross-surface contract (#221)."""
import importlib.util
import json
import runpy
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


def test_classifier_helper_date_freshness_and_remaining_rules():
    mod = _load("signal_classifier")
    today = date(2026, 7, 16)
    assert mod._values({"material_tags": "bad"}, "material_tags") == mod.DEFAULTS["material_tags"]
    assert mod._date(__import__("datetime").datetime(2026, 1, 1)) == date(2026, 1, 1)
    assert mod._date(date(2026, 1, 2)) == date(2026, 1, 2)
    assert mod._date("bad") is None and mod._date(None) is None
    assert mod._fresh("bad-date-2026-99-99.md", {}, {}, today)
    assert mod._fresh("2026-99-99-bad.md", {}, {}, today)
    assert mod._fresh("plain.md", {"ingested": "2026-07-01"}, {}, today)
    assert mod.classify(
        "sources/x.md", {"publisher": "Internal desk"}, today=today
    )[1] == "marketing-fragment"
    assert mod.classify(
        "sources/x.md", {"title": "Acme launches product", "published": "2026-07-01"},
        today=today,
    )[1] == "material-title"
    assert mod.classify(
        "sources/x.md", {"title": "A retrospective", "published": "2026-07-01"},
        today=today,
    )[1] == "historical-title"


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


def test_classify_source_config_schema_and_page_edge_paths(
    tmp_path, monkeypatch, capsys
):
    mod = _load("classify_sources")
    config = tmp_path / "config.yaml"
    config.write_text("[]")
    monkeypatch.setenv("SIGNAL_CLASSIFIER_CONFIG", str(config))
    assert mod._config(tmp_path) == {}
    config.unlink()
    assert mod._config(tmp_path) == {}

    monkeypatch.setattr(mod, "_supported", lambda _vault: False)
    assert mod.run(tmp_path, apply=False)["unsupported_schema"] == 1
    monkeypatch.setattr(mod, "_supported", lambda _vault: True)
    monkeypatch.setattr(mod, "_config", lambda _vault: {})
    monkeypatch.setattr(mod, "classify", lambda *_a, **_k: ("entity-enrichment", "test"))
    monkeypatch.setattr(mod, "guard", lambda *_a, **_k: [])
    sources = tmp_path / "wiki" / "sources"
    sources.mkdir(parents=True)
    (sources / "_skip.md").write_text("x")
    (sources / "plain.md").write_text("plain")
    (sources / "bad.md").write_text("---\n[\n---\nbody")
    (sources / "scalar.md").write_text("---\n- one\n---\nbody")
    (sources / "done.md").write_text(
        "---\nsignal_class: historical-baseline\n---\nbody"
    )
    target = sources / "target.md"
    target.write_text("---\ntype: source\n---\nbody")
    counts = mod.run(tmp_path, apply=False, limit=99)
    assert counts["no_frontmatter"] == 3
    assert counts["unchanged"] == 1 and counts["classified"] == 1
    assert "signal_class" not in target.read_text()
    counts = mod.run(tmp_path, apply=True, force=True, limit=1)
    assert counts["seen"] == 0  # sorted hidden page consumes the bounded slot


def test_classify_source_read_race_supported_exception_main_and_entrypoint(
    tmp_path, monkeypatch, capsys
):
    mod = _load("classify_sources")
    fake = type(sys)("schema_lib")
    fake.merged_schema = lambda *_a: (_ for _ in ()).throw(RuntimeError("bad"))
    monkeypatch.setitem(sys.modules, "schema_lib", fake)
    assert mod._supported(tmp_path) is False
    fake.merged_schema = lambda *_a: {
        "field_enums": {"signal_class": {"enum": "signals"}},
        "enums": {"signals": list(mod.ALL_CLASSES)},
    }
    assert mod._supported(tmp_path) is True

    source = tmp_path / "wiki" / "sources" / "gone.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\ntype: source\n---\n")
    monkeypatch.setattr(mod, "_supported", lambda _v: True)
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("gone"))
            if path == source else original_read(path, *a, **k)
        ),
    )
    assert mod.run(tmp_path, apply=True)["vanished"] == 1

    monkeypatch.setattr(mod, "run", lambda *_a, **_k: {"classified": 2})
    assert mod.main(["--vault", str(tmp_path), "--dry-run", "--force"]) == 0
    out = capsys.readouterr().out
    assert "classified=2" in out and '"wakeAgent": false' in out

    monkeypatch.setattr(sys, "argv", [
        str(CRON / "classify_sources.py"), "--vault", str(tmp_path), "--dry-run",
    ])
    with __import__("pytest").raises(SystemExit) as exc:
        runpy.run_path(str(CRON / "classify_sources.py"), run_name="__main__")
    assert exc.value.code == 0
