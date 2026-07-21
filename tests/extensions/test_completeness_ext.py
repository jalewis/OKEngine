"""okengine.completeness: the declared-expectation gap engine — manifest + fragment,
all four expectation kinds, gap lifecycle (open/auto-resolve/dismiss-respected),
saturation cap, loud no-op without rules, dashboard precision table."""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.completeness"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


def _run(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    m = _load("completeness_audit_ut", EXT / "completeness_audit.py")
    assert m.main() == 0
    return m


def _page(tmp_path, rel, fm_yaml, body=""):
    p = tmp_path / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm_yaml}---\n{body}")
    return p


RULES = """rules:
  - id: vendor-needs-exposure
    when: {type: vendor}
    expect: companion
    companion: "exposure/{slug}"
    severity: high
    resolution_hint: "Create the exposure page."
  - id: ttp-needs-detection
    when: {type: ttp}
    expect: link
    link: {prefix: "detections/"}
    severity: high
  - id: risk-owner
    when: {type: risk}
    expect: field
    field: owner
    severity: medium
  - id: assumption-fresh
    when: {type: assumption}
    expect: freshness
    field: last_reviewed
    max_age_days: 90
    severity: low
  - id: prediction-gradeable
    when: {type: prediction}
    expect: section
    section: What would refute this
    min_chars: 20
    severity: medium
"""


def _vault(tmp_path):
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "completeness-rules.yaml").write_text(RULES)
    _page(tmp_path, "entities/a/acme", "type: vendor\ntitle: Acme\n")            # gap: no exposure
    _page(tmp_path, "entities/g/goodco", "type: vendor\n")                        # satisfied
    _page(tmp_path, "exposure/goodco", "type: concept\n")
    _page(tmp_path, "concepts/t/spearphish", "type: ttp\n", "no links here\n")    # gap: no detection
    _page(tmp_path, "concepts/c/covered", "type: ttp\n", "see [[detections/edr-rule-7]]\n")
    _page(tmp_path, "detections/edr-rule-7", "type: concept\n")
    _page(tmp_path, "risks/r1", "type: risk\n")                                   # gap: no owner
    _page(tmp_path, "risks/r2", "type: risk\nowner: alice\n")
    _page(tmp_path, "assumptions/old", "type: assumption\nlast_reviewed: 2020-01-01\n")  # gap: stale
    _page(tmp_path, "assumptions/fresh", f"type: assumption\nlast_reviewed: 2099-01-01\n")


def test_manifest_and_fragment():
    man = _load("extension_manifest_c", REPO / "scripts/extension_manifest.py")
    m = yaml.safe_load((EXT / "extension.yaml").read_text())
    errors, _ = man.validate_manifest(m)
    assert not errors, errors
    assert m["core"] is False
    frag = yaml.safe_load((EXT / "schema" / "completeness.schema.yaml").read_text())
    assert "gaps" in frag["owns"]["namespaces"] and "gap" in frag["owns"]["types"]
    assert "lacuna" not in (EXT / "schema" / "completeness.schema.yaml").read_text()


def test_all_four_expectation_kinds(tmp_path, monkeypatch, capsys):
    _vault(tmp_path)
    _run(tmp_path, monkeypatch)
    gaps = {p.stem for p in (tmp_path / "wiki" / "gaps").rglob("*.md")}
    assert "vendor-needs-exposure--entities-a-acme" in gaps
    assert "ttp-needs-detection--concepts-t-spearphish" in gaps
    assert "risk-owner--risks-r1" in gaps
    assert "assumption-fresh--assumptions-old" in gaps
    assert len(gaps) == 4                                    # satisfied subjects create nothing
    assert not any(s in g for g in gaps for s in ("goodco", "covered", "--risks-r2", "assumptions-fresh"))
    dash = (tmp_path / "wiki" / "dashboards" / "completeness.md").read_text()
    assert "open: **4**" in dash and "Rule precision" in dash
    # explainability: rule + subject + the unmet expectation on the gap page
    g = (tmp_path / "wiki" / "gaps" / "risk-owner--risks-r1.md").read_text()
    assert "frontmatter field `owner` is missing/empty" in g and "[[risks/r1]]" in g


def test_auto_resolve_and_dismiss_respected(tmp_path, monkeypatch):
    _vault(tmp_path)
    _run(tmp_path, monkeypatch)
    # operator dismisses the ttp gap
    gp = tmp_path / "wiki" / "gaps" / "ttp-needs-detection--concepts-t-spearphish.md"
    gp.write_text(gp.read_text().replace("status: open", "status: dismissed\ndismiss_reason: tracked in SIEM"))
    # the vendor gap gets satisfied
    _page(tmp_path, "exposure/acme", "type: concept\n")
    _run(tmp_path, monkeypatch)
    vfm = yaml.safe_load((tmp_path / "wiki" / "gaps" / "vendor-needs-exposure--entities-a-acme.md")
                         .read_text().split("---")[1])
    assert vfm["status"] == "resolved" and vfm["resolved_on"]
    tfm = yaml.safe_load(gp.read_text().split("---")[1])
    assert tfm["status"] == "dismissed"                      # never reopened
    dash = (tmp_path / "wiki" / "dashboards" / "completeness.md").read_text()
    assert "dismissed: 1" in dash


def test_no_rules_noops_loudly(tmp_path, monkeypatch, capsys):
    (tmp_path / "wiki").mkdir(parents=True)
    _run(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert "no completeness rules" in out and '"wakeAgent": false' in out
    assert not (tmp_path / "wiki" / "gaps").exists()


def test_saturation_cap(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "completeness-rules.yaml").write_text(
        "rules:\n  - id: r\n    when: {type: risk}\n    expect: field\n    field: owner\n")
    for i in range(5):
        _page(tmp_path, f"risks/r{i}", "type: risk\n")
    _run(tmp_path, monkeypatch, COMPLETENESS_MAX_PER_RULE="3")
    out = capsys.readouterr().out
    assert "SATURATED: r" in out
    open_gaps = [p for p in (tmp_path / "wiki" / "gaps").rglob("*.md")]
    assert len(open_gaps) == 3                               # capped, not hidden
    assert "saturated" in (tmp_path / "wiki" / "dashboards" / "completeness.md").read_text()


def test_gap_drain_gate_selects_only_fixable(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "completeness-rules.yaml").write_text(
        "rules:\n"
        "  - id: fixme\n    when: {type: risk}\n    expect: field\n    field: owner\n"
        "    fix: agent-draft\n    resolution_hint: assign it\n"
        "  - id: humans-only\n    when: {type: risk}\n    expect: field\n    field: review_date\n")
    g = tmp_path / "wiki" / "gaps"
    g.mkdir(parents=True)
    (g / "fixme--risks-r1.md").write_text(
        "---\ntype: gap\nrule: fixme\nsubject: risks/r1\nseverity: high\nstatus: open\n"
        "expectation: owner missing\nfirst_seen: 2026-07-01\n---\n")
    (g / "fixme--risks-r2.md").write_text(
        "---\ntype: gap\nrule: fixme\nsubject: risks/r2\nseverity: high\nstatus: dismissed\n---\n")
    (g / "humans-only--risks-r1.md").write_text(
        "---\ntype: gap\nrule: humans-only\nsubject: risks/r1\nseverity: low\nstatus: open\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    m = _load("select_gap_fixes_ut", EXT / "select_gap_fixes.py")
    assert m.main() == 0
    out = capsys.readouterr().out
    assert '"wakeAgent": true' in out
    assert "fixme--risks-r1" in out and "DRAFT MODE" in out
    assert "humans-only--risks-r1" not in out          # human rules never surface
    assert "risks-r2" not in out                       # dismissed never surfaces


def test_gap_drain_silent_when_nothing_fixable(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "completeness-rules.yaml").write_text(
        "rules:\n  - id: r\n    when: {type: risk}\n    expect: field\n    field: owner\n")
    (tmp_path / "wiki").mkdir()
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    m = _load("select_gap_fixes_ut2", EXT / "select_gap_fixes.py")
    assert m.main() == 0
    out = capsys.readouterr().out
    assert '"wakeAgent": false' in out and "operator-only" in out


def test_section_kind_gradeability_gate(tmp_path, monkeypatch):
    """okengine#214: a resolvable proposition without substantive refutation criteria opens a
    gap; a thick section satisfies; a present-but-thin section still gaps (vacuous criteria)."""
    _vault(tmp_path)
    _page(tmp_path, "predictions/p-good", "type: prediction\ntitle: Good\n",
          "# G\n\n## What would refute this\n\nA vendor advisory retracting the CVE, or 90 days with zero KEV additions.\n")
    _page(tmp_path, "predictions/p-missing", "type: prediction\ntitle: Missing\n",
          "# M\n\nNo criteria section at all.\n")
    _page(tmp_path, "predictions/p-thin", "type: prediction\ntitle: Thin\n",
          "# T\n\n## What would refute this\n\nTBD\n\n## Other\nx\n")
    _run(tmp_path, monkeypatch)
    gaps = {p.stem for p in (tmp_path / "wiki" / "gaps").rglob("*.md")}
    assert "prediction-gradeable--predictions-p-missing" in gaps
    assert "prediction-gradeable--predictions-p-thin" in gaps
    assert not any("p-good" in g for g in gaps), gaps
