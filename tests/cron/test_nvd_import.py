"""Contract for the single engine-staged NVD lane (#267)."""
from __future__ import annotations

import importlib.util
import json
import runpy
import sys
import types
from pathlib import Path

import pytest
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
    # ``module.time`` is the process-wide stdlib module.  Assigning directly
    # leaks the stub into every later test (including hard-deadline tests).
    monkeypatch.setattr(module.time, "sleep", lambda *_args: None)
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


def test_truthy_record_metric_precedence_and_empty_shapes(monkeypatch):
    m = _load(monkeypatch)
    assert m._truthy(True) and m._truthy(" YES ") and not m._truthy(None)
    raw = _raw()
    raw["descriptions"].insert(0, {"lang": "fr", "value": "non"})
    raw["metrics"] = {
        "cvssMetricV40": [],
        "cvssMetricV31": [{"cvssData": {"baseScore": 8, "version": "3.1"},
                           "baseSeverity": "HIGH"}],
    }
    raw["weaknesses"][0]["description"].append({"value": "not-cwe"})
    rec = m.nvd_record(raw)
    assert rec["description"] == "description"
    assert (rec["cvss_base"], rec["severity"], rec["cvss_version"]) == (8, "high", "3.1")
    assert m.nvd_record({}) == {
        "cve_id": "", "description": "", "cvss_base": None,
        "cvss_version": None, "severity": None, "cwe": [],
    }


def test_request_recent_paging_and_fetch_one(monkeypatch):
    m = _load(monkeypatch)
    original_request = m._request

    class Response:
        def __init__(self, value):
            self.value = value
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return json.dumps(self.value).encode()

    seen = []
    payloads = [
        {"vulnerabilities": [{"cve": _raw()}], "totalResults": 2001},
        {"vulnerabilities": [{"cve": _raw("CVE-2026-54321")}], "totalResults": 2001},
    ]

    def urlopen(req, timeout):
        seen.append((req, timeout))
        return Response(payloads.pop(0))

    monkeypatch.setattr(m.urllib.request, "urlopen", urlopen)
    records = m.fetch_recent(0, "secret", now=m.datetime(2026, 7, 1, tzinfo=m.timezone.utc))
    assert [r["cve_id"] for r in records] == ["CVE-2026-12345", "CVE-2026-54321"]
    assert seen[0][0].headers["Apikey"] == "secret" and seen[0][1] == 90

    monkeypatch.setattr(m, "_request", lambda *_a: {"vulnerabilities": []})
    assert m.fetch_one("CVE-2026-1", None) is None
    monkeypatch.setattr(m, "_request", lambda *_a: {"vulnerabilities": [{"cve": _raw()}]})
    assert m.fetch_one("CVE-2026-12345", None)["cve_id"] == "CVE-2026-12345"

    calls = iter([
        {"vulnerabilities": [{"cve": _raw()}], "totalResults": 9999},
        {"vulnerabilities": [], "totalResults": 9999},
    ])
    monkeypatch.setattr(m, "_request", lambda *_a: next(calls))
    assert len(m.fetch_recent(2, None, max_pages=2)) == 1
    assert m.fetch_recent(2, None, max_pages=0) == []
    monkeypatch.setattr(
        m, "_request",
        lambda *_a: {"vulnerabilities": [{"cve": _raw()}], "totalResults": 9999},
    )
    assert len(m.fetch_recent(2, None, max_pages=1)) == 1

    # The transport also supports the unauthenticated header shape.
    monkeypatch.setattr(
        m.urllib.request, "urlopen",
        lambda req, timeout: Response({"vulnerabilities": []}),
    )
    monkeypatch.setattr(m, "_request", original_request)
    assert m._request({}, None) == {"vulnerabilities": []}


def test_model_page_read_frontmatter_render_and_write_edges(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    with pytest.raises(ValueError, match="unsupported"):
        m._model("other")
    existing = tmp_path / "wiki" / "cves" / "x" / "CVE-2026-12345.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("plain")
    assert m.page_path(tmp_path, "CVE-2026-12345", "cve") == existing
    assert m._read_page(existing) == ({}, "plain")
    existing.write_text("---\n[\n---\nbody")
    assert m._read_page(existing)[0] == {}
    existing.write_text("---\n- one\n---\nbody")
    assert m._read_page(existing) == ({}, "body")
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert m._read_page(existing) == ({}, "")

    rec = {"cve_id": "CVE-2026-12345", "cvss_base": None, "cvss_version": "",
           "severity": "", "cwe": []}
    cve = m._frontmatter(rec, "cve", "2026-01-01")
    vuln = m._frontmatter({**rec, "cwe": ["CWE-1", "CWE-2"]},
                          "vulnerability", "2026-01-01")
    assert "cvss_base" not in cve and cve["url"].endswith(rec["cve_id"])
    assert vuln["cwe"] == ["CWE-1", "CWE-2"] and vuln["tlp"] == "CLEAR"

    monkeypatch.setattr(m.importer_guard, "guard", lambda *_a, **_k: ["blocked"])
    assert m._write(existing, cve, "body", tmp_path, "cves", False) == (False, ["blocked"])
    monkeypatch.setattr(m.importer_guard, "guard", lambda *_a, **_k: [])
    assert m._write(existing, cve, "body", tmp_path, "cves", True) == (True, [])


def test_apply_record_all_dispositions(monkeypatch, tmp_path, capsys):
    m = _load(monkeypatch)
    base = m.nvd_record(_raw())
    assert m.apply_record(tmp_path, {}, "cve", stub_new=True, all_severities=True,
                          today="2026", dry_run=True) == "skip"
    low = {**base, "severity": "medium"}
    assert m.apply_record(tmp_path, low, "vulnerability", stub_new=True,
                          all_severities=False, today="2026", dry_run=True) == "skip"
    assert m.apply_record(tmp_path, low, "vulnerability", stub_new=True,
                          all_severities=True, today="2026", dry_run=True) == "created"

    path = m.page_path(tmp_path, base["cve_id"], "cve")
    path.parent.mkdir(parents=True)
    path.write_text("not frontmatter")
    assert m.apply_record(tmp_path, base, "cve", stub_new=False, all_severities=True,
                          today="2026", dry_run=True) == "rejected"
    path.write_text(m._render(m._frontmatter(base, "cve", "old"), "body"))
    assert m.apply_record(tmp_path, base, "cve", stub_new=False, all_severities=True,
                          today="new", dry_run=True) == "unchanged"
    changed = {**base, "cvss_base": 7.7}
    monkeypatch.setattr(m, "_write", lambda *_a, **_k: (False, ["policy"]))
    assert m.apply_record(tmp_path, changed, "cve", stub_new=False, all_severities=True,
                          today="new", dry_run=False) == "rejected"
    assert "policy" in capsys.readouterr().err


def test_observation_registry_existing_reject_and_skip(monkeypatch, tmp_path, capsys):
    m = _load(monkeypatch)
    rec = m.nvd_record(_raw())
    assert m.apply_observation(tmp_path, {}, all_severities=True,
                               today="x", dry_run=True) == "skip"
    assert m.apply_observation(tmp_path, {**rec, "severity": "low"},
                               all_severities=False, today="x", dry_run=True) == "skip"
    (tmp_path / "schema.yaml").write_text(
        "source_registry:\n  nvd:\n    reliability: B\n    credibility_default: 3\n"
    )
    monkeypatch.setattr(m, "_write", lambda *_a, **_k: (False, ["bad"]))
    assert m.apply_observation(tmp_path, rec, all_severities=True,
                               today="x", dry_run=False) == "rejected"
    assert "observation reject" in capsys.readouterr().err
    existing = tmp_path / "wiki" / "observations" / "nvd" / "c" / "cve-2026-12345.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("x")
    assert m.observation_path(tmp_path, rec["cve_id"]) == existing


def test_observation_registry_failure_uses_explicit_safe_defaults(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    rec = m.nvd_record(_raw())
    captured = {}
    monkeypatch.setattr(
        m.schema_lib,
        "merged_schema",
        lambda _vault: (_ for _ in ()).throw(ValueError("invalid registry")),
    )

    def capture_write(path, frontmatter, body, vault, namespace, dry_run):
        captured.update(
            path=path,
            frontmatter=frontmatter,
            body=body,
            vault=vault,
            namespace=namespace,
            dry_run=dry_run,
        )
        return True, []

    monkeypatch.setattr(m, "_write", capture_write)

    assert m.apply_observation(
        tmp_path, rec, all_severities=True, today="2026-09-13", dry_run=True
    ) == "written"
    assert captured["frontmatter"]["reliability"] == "A"
    assert captured["frontmatter"]["credibility"] == "2"
    assert captured["namespace"] == "observations/nvd"


def test_backfill_targets_and_run_outcomes(monkeypatch, tmp_path, capsys):
    m = _load(monkeypatch)
    assert m.backfill_targets(tmp_path, "cve", False) == []
    base = tmp_path / "wiki" / "cves"
    base.mkdir(parents=True)
    for name, frontmatter in [
        ("a.md", "type: cve\ncve_id: CVE-2026-1111"),
        ("b.md", "type: cve\ncve_id: invalid"),
        ("c.md", "type: cve\ncve_id: CVE-2026-2222\ncvss_base: 5\nseverity: medium"),
        ("d.md", "type: cve\ncve_id: CVE-2026-1111"),
    ]:
        (base / name).write_text(f"---\n{frontmatter}\n---\nbody")
    assert m.backfill_targets(tmp_path, "cve", False) == ["CVE-2026-1111"]
    assert m.backfill_targets(tmp_path, "cve", True) == [
        "CVE-2026-1111", "CVE-2026-2222"
    ]
    original_read_page = m._read_page
    monkeypatch.setattr(m, "_read_page", lambda *_a: (_ for _ in ()).throw(OSError()))
    assert m.backfill_targets(tmp_path, "cve", True) == []
    monkeypatch.setattr(m, "_read_page", original_read_page)

    args = types.SimpleNamespace(vault=str(tmp_path), page_model="cve", reenrich=True,
                                 limit=1, dry_run=True, strict=False)
    results = iter([None, _raw("CVE-2026-2222")])
    monkeypatch.setattr(m, "fetch_one", lambda *_a: next(results))
    monkeypatch.setattr(m, "apply_record", lambda *_a, **_k: "enriched")
    counts = m.run_backfill(args, None)
    assert counts["missing"] == 1
    args.limit = 0
    results = iter([None, _raw("CVE-2026-2222")])
    counts = m.run_backfill(args, None)
    assert counts["missing"] == 1 and counts["enriched"] == 1

    monkeypatch.setattr(m, "backfill_targets", lambda *_a: ["CVE-2026-1111"])
    monkeypatch.setattr(m, "fetch_one", lambda *_a: (_ for _ in ()).throw(RuntimeError("network")))
    assert m.run_backfill(args, None)["errors"] == 1
    assert "network" in capsys.readouterr().err
    args.strict = True
    with pytest.raises(RuntimeError):
        m.run_backfill(args, "key")


def test_main_network_observations_strict_and_entrypoint(monkeypatch, tmp_path, capsys):
    m = _load(monkeypatch)
    rec = m.nvd_record(_raw())
    monkeypatch.setattr(m, "fetch_recent", lambda *_a, **_k: [rec])
    monkeypatch.setattr(m, "apply_record", lambda *_a, **_k: "unexpected")
    assert m.main(["--vault", str(tmp_path), "--stub-new"]) == 0
    assert json.loads(capsys.readouterr().out)["nvd"]["skip"] == 1

    monkeypatch.setattr(m, "apply_observation", lambda *_a, **_k: "written")
    assert m.main(["--vault", str(tmp_path), "--page-model", "vulnerability",
                   "--observations"]) == 0
    assert json.loads(capsys.readouterr().out)["nvd"]["written"] == 1
    with pytest.raises(SystemExit):
        m.main(["--observations", "--page-model", "cve"])

    monkeypatch.setattr(m, "fetch_recent", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert m.main([]) == 0
    assert "WARN" in capsys.readouterr().err
    assert m.main(["--strict"]) == 1
    assert "ERROR" in capsys.readouterr().err

    monkeypatch.setattr(m, "run_backfill", lambda *_a: {"errors": 0, "rejected": 1})
    assert m.main(["--backfill", "--strict"]) == 1

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--src", str(tmp_path / "missing")])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
