"""okengine.actor-risk-ranking v1 (okengine#170) — deterministic scorer regressions.

Pins the hard rules from the design: loud no-op without config, person-target
refusal, stale-artifact skip, the syndication gate (distinct origin domains, not
item counts), alias folding + unresolved-pair reporting, and driver evidence
being real page links.
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.actor-risk-ranking"


def _load():
    spec = importlib.util.spec_from_file_location("actor_risk_rank",
                                                  EXT / "actor_risk_rank.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["actor_risk_rank"] = m
    spec.loader.exec_module(m)
    return m


mod = _load()


def _page(wiki: Path, key: str, fm: dict):
    p = wiki / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\nbody\n")


def _vault(tmp_path, syndicated=False):
    """Fixture: actor apt-x linked to a vuln (touching okta), malware, a sector,
    the target entity, and 3 recent sources (3 domains, or 1 when syndicated)."""
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    (vault / "config").mkdir(parents=True)
    wiki.mkdir()
    today = time.strftime("%Y-%m-%d")
    _page(wiki, "entities/a/apt-x", {"type": "threat-actor", "name": "APT X"})
    _page(wiki, "entities/l/lazarus-group",
          {"type": "threat-actor", "name": "Lazarus Group", "aliases": ["lazarus"]})
    _page(wiki, "entities/l/lazarus", {"type": "threat-actor", "name": "Lazarus"})
    _page(wiki, "entities/c/cobalt", {"type": "threat-actor", "name": "Cobalt"})
    _page(wiki, "entities/c/cobalt-gang", {"type": "threat-actor", "name": "Cobalt Gang"})
    _page(wiki, "entities/v/cve-x", {"type": "vulnerability", "name": "CVE-X"})
    _page(wiki, "entities/m/badrat", {"type": "malware", "name": "BadRAT"})
    _page(wiki, "entities/p/okta", {"type": "product", "name": "Okta"})
    _page(wiki, "entities/o/acme", {"type": "vendor", "name": "Acme"})
    _page(wiki, "segments/cloud-infrastructure", {"type": "segment", "name": "Cloud infra"})
    doms = ["one.example", "one.example", "one.example"] if syndicated else \
           ["one.example", "two.example", "three.example"]
    for i, d in enumerate(doms):
        _page(wiki, f"sources/2026/s{i}",
              {"type": "source", "published": today, "url": f"https://{d}/r{i}"})
    bl = {
        "entities/a/apt-x": [{"key": f"sources/2026/s{i}", "title": f"s{i}"} for i in range(3)],
        "entities/v/cve-x": [{"key": "entities/a/apt-x", "title": "APT X"},
                             {"key": "entities/p/okta", "title": "Okta"}],
        "entities/m/badrat": [{"key": "entities/a/apt-x", "title": "APT X"}],
        "segments/cloud-infrastructure": [{"key": "entities/a/apt-x", "title": "APT X"}],
        "entities/o/acme": [{"key": "entities/a/apt-x", "title": "APT X"}],
    }
    (wiki / ".backlinks.json").write_text(json.dumps(
        {"version": 1, "built_at": int(time.time()), "backlinks": bl}))
    (vault / "config" / "actor-risk-targets.yaml").write_text(yaml.safe_dump({
        "targets": {"acme": {"type": "company", "entity": "entities/o/acme",
                             "sectors": ["cloud-infrastructure"],
                             "technologies": ["entities/p/okta"]}},
        "scoring": {"horizon_days": 180, "min_origin_domains": 2, "top_n": 10},
    }))
    return vault


def _run(vault, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(vault))
    return mod.main()


def test_no_config_is_loud_noop(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "v"
    (vault / "wiki").mkdir(parents=True)
    assert _run(vault, monkeypatch) == 0
    assert "no-op" in capsys.readouterr().out
    assert not (vault / "wiki" / "dashboards").exists()


def test_person_target_refused(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    (vault / "config" / "actor-risk-targets.yaml").write_text(yaml.safe_dump(
        {"targets": {"ceo": {"type": "person", "entity": "entities/p/someone"}}}))
    monkeypatch.setenv("VAULT_DIR", str(vault))
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 2


def test_stale_artifact_skips_loudly(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    p = vault / "wiki" / ".backlinks.json"
    old = time.time() - 5 * 86400
    os.utime(p, (old, old))
    with pytest.raises(SystemExit) as ei:
        _run(vault, monkeypatch)
    assert ei.value.code == 1
    assert "stale" in capsys.readouterr().err


def test_full_evidence_ranks_apt_x_top_with_all_drivers(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    assert _run(vault, monkeypatch) == 0
    rk = (vault / "wiki" / "dashboards" / "actor-risk" / "rankings.md").read_text()
    tgt = (vault / "wiki" / "dashboards" / "actor-risk" / "acme.md").read_text()
    first_row = next(ln for ln in rk.splitlines() if ln.startswith("| 1 |"))
    assert "apt-x" in first_row
    for d in ("direct", "opportunity", "capability", "intent", "recency"):
        assert f"**{d}**" in tgt, f"driver {d} missing from the target dashboard"
    assert "[[entities/v/cve-x]]" in tgt          # evidence is clickable pages
    assert "**origin domains** (3)" in tgt


def test_syndication_gate_caps_band(tmp_path, monkeypatch):
    """Same evidence, but all three sources from ONE domain — the band must not
    exceed moderate no matter the raw score."""
    v_multi = _vault(tmp_path / "multi")
    v_syn = _vault(tmp_path / "syn", syndicated=True)
    for v in (v_multi, v_syn):
        assert _run(v, monkeypatch) == 0
    def band(v):
        rk = (v / "wiki" / "dashboards" / "actor-risk" / "rankings.md").read_text()
        row = next(ln for ln in rk.splitlines() if "apt-x" in ln)
        return row.split("**")[1]
    assert band(v_multi) in ("elevated", "high")
    assert band(v_syn) == "moderate"


def test_vendor_ontology_via_config(tmp_path, monkeypatch):
    """okengine#174: the vendor variant is config, not a fork — actor_types: [vendor]
    ranks vendor pages, capability_types: [product, component] makes their supply
    footprint the capability driver."""
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    (vault / "config").mkdir(parents=True)
    wiki.mkdir()
    today = time.strftime("%Y-%m-%d")
    _page(wiki, "entities/a/acme-supply", {"type": "vendor", "name": "Acme Supply"})
    _page(wiki, "entities/q/quiet-vendor", {"type": "vendor", "name": "Quiet Vendor"})
    _page(wiki, "entities/w/widget", {"type": "product", "name": "Widget"})
    _page(wiki, "entities/l/libfoo", {"type": "component", "name": "libfoo"})
    _page(wiki, "entities/v/cve-y", {"type": "vulnerability", "name": "CVE-Y"})
    _page(wiki, "sources/2026/i0", {"type": "source", "published": today,
                                    "url": "https://a.example/breach"})
    _page(wiki, "sources/2026/i1", {"type": "source", "published": today,
                                    "url": "https://b.example/advisory"})
    bl = {
        "entities/a/acme-supply": [{"key": "sources/2026/i0", "title": "i0"},
                                   {"key": "sources/2026/i1", "title": "i1"}],
        "entities/w/widget": [{"key": "entities/a/acme-supply", "title": "Acme"}],
        "entities/l/libfoo": [{"key": "entities/a/acme-supply", "title": "Acme"}],
        "entities/v/cve-y": [{"key": "entities/a/acme-supply", "title": "Acme"},
                             {"key": "entities/w/widget", "title": "Widget"}],
    }
    (wiki / ".backlinks.json").write_text(json.dumps(
        {"version": 1, "built_at": int(time.time()), "backlinks": bl}))
    (vault / "config" / "actor-risk-targets.yaml").write_text(yaml.safe_dump({
        "targets": {"our-deps": {"type": "company",
                                 "technologies": ["entities/w/widget"]}},
        "scoring": {"actor_types": ["vendor"],
                    "capability_types": ["product", "component"], "top_n": 10},
    }))
    assert _run(vault, monkeypatch) == 0
    tgt = (vault / "wiki" / "dashboards" / "actor-risk" / "our-deps.md").read_text()
    assert "acme-supply" in tgt
    assert "[[entities/w/widget]]" in tgt and "[[entities/l/libfoo]]" in tgt  # capability = footprint
    assert "[[entities/v/cve-y]]" in tgt                       # opportunity: vuln touching widget
    rk = (vault / "wiki" / "dashboards" / "actor-risk" / "rankings.md").read_text()
    row1 = next(ln for ln in rk.splitlines() if ln.startswith("| 1 |"))
    assert "acme-supply" in row1                               # evidence-backed vendor outranks quiet one


def test_alias_folding_and_unresolved_report(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    assert _run(vault, monkeypatch) == 0
    rk = (vault / "wiki" / "dashboards" / "actor-risk" / "rankings.md").read_text()
    # lazarus declared as alias of lazarus-group -> folded, never its own row
    assert "| [[entities/l/lazarus]] |" not in rk
    # cobalt vs cobalt-gang: undeclared near-duplicate -> reported, not merged
    assert "Unresolved alias candidates" in rk
    assert "cobalt" in rk
