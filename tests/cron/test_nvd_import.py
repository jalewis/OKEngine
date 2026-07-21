"""Contract for the single engine-staged NVD lane (#267)."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "nvd_import.py"


def _load(monkeypatch):
    migrate = types.ModuleType("okf_migrate")

    def find_page(root, namespace, slug):
        base = Path(root) / "wiki" / namespace
        hits = list(base.rglob(f"{slug}.md")) if base.exists() else []
        return hits[0] if hits else None

    migrate.find_page = find_page
    migrate.write_key = lambda _root, ns, slug, _fm: f"{ns}/{slug[0].lower()}/{slug}"
    guard = types.ModuleType("importer_guard")
    guard.guard = lambda fm, **_kwargs: ([] if fm.get("type") in {"cve", "vulnerability"}
                                         else ["bad type"])
    monkeypatch.setitem(sys.modules, "okf_migrate", migrate)
    monkeypatch.setitem(sys.modules, "importer_guard", guard)
    spec = importlib.util.spec_from_file_location("shared_nvd_import", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.time.sleep = lambda *_args: None
    return module


def _raw(cid="CVE-2026-12345", score=9.8, severity="CRITICAL"):
    return {"id": cid, "descriptions": [{"lang": "en", "value": "description"}],
            "metrics": {"cvssMetricV31": [{"cvssData": {
                "baseScore": score, "baseSeverity": severity, "version": "3.1"}}]},
            "weaknesses": [{"description": [{"value": "CWE-79"}]}]}


def test_one_parser_serves_both_page_models(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    rec = m.nvd_record(_raw())
    assert rec["severity"] == "critical" and rec["cwe"] == ["CWE-79"]
    assert m.page_path(tmp_path, rec["cve_id"], "cve").as_posix().endswith(
        "/wiki/cves/c/CVE-2026-12345.md")
    assert m.page_path(tmp_path, rec["cve_id"], "vulnerability").as_posix().endswith(
        "/wiki/entities/c/cve-2026-12345.md")


def test_vulnerability_profile_stubs_high_and_merges_without_clobber(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    rec = m.nvd_record(_raw())
    result = m.apply_record(tmp_path, rec, "vulnerability", stub_new=True,
                            all_severities=False, today="2026-07-18", dry_run=False)
    assert result == "created"
    path = m.page_path(tmp_path, rec["cve_id"], "vulnerability")
    fm, body = m._read_page(path)
    fm["kev"] = True
    path.write_text(m._render(fm, body + "\ncurated"), encoding="utf-8")
    changed = m.nvd_record(_raw(score=8.8, severity="HIGH"))
    assert m.apply_record(tmp_path, changed, "vulnerability", stub_new=True,
                          all_severities=False, today="2026-07-19", dry_run=False) == "enriched"
    merged, merged_body = m._read_page(path)
    assert merged["kev"] is True and "curated" in merged_body
    assert merged["cvss_base"] == 8.8 and merged["severity"] == "high"


def test_cve_profile_is_enrich_only_and_backfill_targets_missing(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    rec = m.nvd_record(_raw())
    assert m.apply_record(tmp_path, rec, "cve", stub_new=False,
                          all_severities=False, today="2026-07-18", dry_run=False) == "skip"
    path = m.page_path(tmp_path, rec["cve_id"], "cve")
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: cve\ncve_id: CVE-2026-12345\nkev: true\n---\nbody\n",
                    encoding="utf-8")
    assert m.backfill_targets(tmp_path, "cve", reenrich=False) == ["CVE-2026-12345"]
    assert m.apply_record(tmp_path, rec, "cve", stub_new=False,
                          all_severities=False, today="2026-07-18", dry_run=False) == "enriched"
    fm, body = m._read_page(path)
    assert fm["kev"] is True and fm["cvss_base"] == 9.8 and body.strip() == "body"
    assert m.backfill_targets(tmp_path, "cve", reenrich=False) == []


def test_cli_fixture_is_standalone_and_boundary_clean(monkeypatch, tmp_path, capsys):
    m = _load(monkeypatch)
    fixture = tmp_path / "nvd.json"
    fixture.write_text(json.dumps({"vulnerabilities": [{"cve": _raw()}]}), encoding="utf-8")
    assert m.main(["--vault", str(tmp_path), "--page-model", "vulnerability",
                   "--stub-new", "--src", str(fixture)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wakeAgent"] is False and payload["nvd"]["created"] == 1
    page = next((tmp_path / "wiki" / "entities").rglob("*.md"))
    fm = yaml.safe_load(page.read_text(encoding="utf-8").split("---", 2)[1])
    assert fm["type"] == "vulnerability" and fm["tlp"] == "CLEAR"


def test_optional_observation_profile_is_preserved(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    rec = m.nvd_record(_raw())
    assert m.apply_observation(tmp_path, rec, all_severities=False,
                               today="2026-07-18", dry_run=False) == "written"
    path = m.observation_path(tmp_path, rec["cve_id"])
    fm, _ = m._read_page(path)
    assert fm["source"] == "nvd" and fm["canonical"] == "cve-2026-12345"
    assert fm["reliability"] == "A" and fm["credibility"] == "2"
