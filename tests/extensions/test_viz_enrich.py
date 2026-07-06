"""okengine.viz concept-enrich (okengine#172): the axis-field backfill drain — fragment
composition (first extends-on-core-type user), anchor-first selection, never-overwrite,
uncertain-skip, byte-preserved bodies, own-clock checkpointing."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.viz"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


def _fake_llm(evo="product", val="foundational", fail_after=None):
    """Stub llm_lib injected under the script's import name. Answers evolution then
    value-chain per concept; optionally raises LLMError after N successful calls."""
    mod = types.ModuleType("llm_lib")
    calls = {"n": 0}

    class LLMError(Exception):
        pass

    def classify(text, labels, **kw):
        if fail_after is not None and calls["n"] >= fail_after:
            raise LLMError("endpoint down")
        calls["n"] += 1
        return evo if "genesis" in text else val
    mod.classify, mod.LLMError, mod._calls = classify, LLMError, calls
    return mod


def _vault(tmp_path):
    c = tmp_path / "wiki" / "concepts"
    for slug, extra in [("anchored-core", ""), ("popular-hub", ""), ("tail-item", ""),
                        ("human-set", "evolution: genesis\n"),
                        ("already-done", "evolution: product\nvalue_chain: 0.5\nviz_enriched: 2026-06-01\n")]:
        p = c / slug[0] / f"{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntype: concept\ntitle: {slug}\n{extra}---\n# {slug}\nbody text\n")
    # watchlist anchor links anchored-core; popular-hub gets in-degree from sources
    wl = tmp_path / "wiki" / "concepts" / "w" / "watchlist.md"
    wl.parent.mkdir(parents=True, exist_ok=True)
    wl.write_text("---\ntype: concept\ntitle: WL\n---\n[[concepts/a/anchored-core]]\n")
    s = tmp_path / "wiki" / "sources"
    s.mkdir(parents=True)
    for i in range(4):
        (s / f"s{i}.md").write_text("---\ntype: source\n---\n[[concepts/p/popular-hub]]\n")
    return tmp_path


def _run(tmp_path, monkeypatch, llm, batch="25", budget="300"):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("VIZ_ANCHOR", "concepts/w/watchlist.md")
    monkeypatch.setenv("ENRICH_BATCH", batch)
    monkeypatch.setenv("ENRICH_TIME_BUDGET", budget)
    sys.modules["llm_lib"] = llm
    try:
        m = _load("enrich_concepts_under_test", EXT / "enrich_concepts.py")
        assert m.main() == 0
    finally:
        sys.modules.pop("llm_lib", None)
        sys.modules.pop("enrich_concepts_under_test", None)


def _fm(tmp_path, slug):
    p = next((tmp_path / "wiki" / "concepts").rglob(f"{slug}.md"))
    t = p.read_text()
    return yaml.safe_load(t.split("---")[1]), t


def test_fragment_extends_core_concept():
    sl = _load("schema_lib_vt", REPO / "scripts" / "cron" / "schema_lib.py")
    frag = yaml.safe_load((EXT / "schema" / "viz.schema.yaml").read_text())
    composed, errors = sl.compose_schema(REPO / "config", [("ext:okengine.viz", frag)])
    assert not errors, errors
    fields = composed["types"]["concept"].get("fields") or {}
    assert {"evolution", "value_chain", "viz_enriched"} <= set(fields)


def test_enriches_anchor_scope_first_and_never_overwrites(tmp_path, monkeypatch):
    _vault(tmp_path)
    _run(tmp_path, monkeypatch, _fake_llm(), batch="2")   # room for only 2 of the 3 candidates
    fm_a, _ = _fm(tmp_path, "anchored-core")
    assert fm_a["evolution"] == "product" and fm_a["value_chain"] == 0.85
    assert fm_a["viz_enriched"]
    fm_p, _ = _fm(tmp_path, "popular-hub")                # second priority: in-degree
    assert fm_p.get("evolution") == "product"
    fm_t, _ = _fm(tmp_path, "tail-item")                  # batch exhausted before the tail
    assert fm_t.get("evolution") is None
    fm_h, _ = _fm(tmp_path, "human-set")                  # human-set (no marker): untouched
    assert fm_h["evolution"] == "genesis" and "viz_enriched" not in fm_h
    fm_d, _ = _fm(tmp_path, "already-done")               # marker present: not re-enriched
    assert str(fm_d["viz_enriched"]) == "2026-06-01"


def test_uncertain_skips_page_untouched(tmp_path, monkeypatch):
    _vault(tmp_path)
    _, before = _fm(tmp_path, "anchored-core")
    _run(tmp_path, monkeypatch, _fake_llm(evo="uncertain"))
    _, after = _fm(tmp_path, "anchored-core")
    assert before == after                                # byte-identical: nothing written


def test_endpoint_failure_stops_run_with_checkpoint(tmp_path, monkeypatch):
    _vault(tmp_path)
    _run(tmp_path, monkeypatch, _fake_llm(fail_after=2))  # concept 1 completes (2 calls), then down
    fm_a, _ = _fm(tmp_path, "anchored-core")
    assert fm_a.get("evolution")                          # first item checkpointed
    fm_p, _ = _fm(tmp_path, "popular-hub")
    assert fm_p.get("evolution") is None                  # run stopped, not burned


def test_body_preserved_byte_for_byte(tmp_path, monkeypatch):
    _vault(tmp_path)
    _, before = _fm(tmp_path, "anchored-core")
    body_before = before.split("---", 2)[2]
    _run(tmp_path, monkeypatch, _fake_llm())
    _, after = _fm(tmp_path, "anchored-core")
    assert after.split("---", 2)[2] == body_before
