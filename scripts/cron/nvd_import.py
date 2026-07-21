#!/usr/bin/env python3
"""Shared bounded NVD enrichment lane for composed OKEngine deployments.

The engine owns this transport/normalization implementation; packs select a page
model instead of forking it. Supported models are the public ``cve`` catalog
(``wiki/cves``) and the cyber-market ``vulnerability`` entity catalog
(``wiki/entities``). Both use the same fetch, CVSS selection, merge, boundary,
backfill, and failure semantics.

Env/CLI: WIKI_PATH, NVD_API_KEY, NVD_DAYS, NVD_PAGE_MODEL,
NVD_STUB_NEW, NVD_BACKFILL, NVD_REENRICH, NVD_LIMIT, NVD_OBSERVATIONS.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importer_guard  # noqa: E402
import okf_migrate  # noqa: E402

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$")
_HIGH = {"high", "critical"}
_OWNED = {"cvss_base", "cvss_version", "severity", "cwe", "last_updated"}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def nvd_record(cve: dict) -> dict:
    """Flatten an NVD 2.0 CVE object, preferring CVSS 4.0 then 3.1/3.0/2."""
    cid = str(cve.get("id") or "").strip().upper()
    desc = next((str(d.get("value") or "").strip() for d in cve.get("descriptions") or []
                 if d.get("lang") == "en"), "")
    score = severity = version = None
    metrics = cve.get("metrics") or {}
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        rows = metrics.get(key) or []
        if not rows:
            continue
        data = rows[0].get("cvssData") or {}
        score = data.get("baseScore")
        version = data.get("version")
        severity = str(data.get("baseSeverity") or rows[0].get("baseSeverity") or "").lower() or None
        break
    cwes = sorted({str(d.get("value")) for w in cve.get("weaknesses") or []
                   for d in w.get("description") or []
                   if re.fullmatch(r"CWE-\d+", str(d.get("value") or ""))})
    return {"cve_id": cid, "description": desc, "cvss_base": score,
            "cvss_version": version, "severity": severity, "cwe": cwes}


def _request(params: dict, api_key: str | None) -> dict:
    headers = {"User-Agent": "okengine/nvd-import"}
    if api_key:
        headers["apiKey"] = api_key
    req = urllib.request.Request(f"{NVD_API}?{urllib.parse.urlencode(params)}", headers=headers)
    with urllib.request.urlopen(req, timeout=90) as response:  # noqa: S310  # nosec B310
        return json.loads(response.read().decode("utf-8"))


def fetch_recent(days: int, api_key: str | None, now: datetime | None = None,
                 max_pages: int = 5) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=max(1, days))).strftime("%Y-%m-%dT%H:%M:%S.000")
    end = now.strftime("%Y-%m-%dT%H:%M:%S.000")
    out: list[dict] = []
    index = 0
    for page in range(max_pages):
        data = _request({"lastModStartDate": start, "lastModEndDate": end,
                         "resultsPerPage": 2000, "startIndex": index}, api_key)
        rows = data.get("vulnerabilities") or []
        out.extend(nvd_record(row.get("cve") or {}) for row in rows)
        index += 2000
        if not rows or index >= int(data.get("totalResults") or 0):
            break
        if page + 1 < max_pages:
            time.sleep(0.7 if api_key else 6.5)
    return out


def fetch_one(cve_id: str, api_key: str | None) -> dict | None:
    rows = _request({"cveId": cve_id}, api_key).get("vulnerabilities") or []
    return nvd_record(rows[0].get("cve") or {}) if rows else None


def _model(page_model: str) -> tuple[str, str]:
    if page_model == "cve":
        return "cve", "cves"
    if page_model == "vulnerability":
        return "vulnerability", "entities"
    raise ValueError(f"unsupported NVD page model: {page_model}")


def page_path(vault: Path, cve_id: str, page_model: str) -> Path:
    typ, namespace = _model(page_model)
    slug = cve_id.lower() if page_model == "vulnerability" else cve_id.upper()
    root = Path(vault)
    existing = okf_migrate.find_page(root, namespace, slug)
    if existing:
        return existing
    fm = {"type": typ, "cve_id": cve_id.upper()}
    return root / "wiki" / (okf_migrate.write_key(root, namespace, slug, fm) + ".md")


def _read_page(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""  # pages may move during concurrent reshard/scan lanes
    if not text.startswith("---\n") or "\n---" not in text[4:]:
        return {}, text
    head, body = text[4:].split("\n---", 1)
    try:
        fm = yaml.safe_load(head) or {}
    except yaml.YAMLError:
        return {}, text
    return (fm if isinstance(fm, dict) else {}), body.lstrip("\n")


def _frontmatter(rec: dict, page_model: str, today: str) -> dict:
    typ, _ = _model(page_model)
    cid = rec["cve_id"]
    if page_model == "cve":
        fm = {"type": typ, "id": cid, "cve_id": cid, "title": cid,
              "exploitation_status": "reported", "sources": ["NVD"],
              "url": f"https://nvd.nist.gov/vuln/detail/{cid}"}
    else:
        fm = {"type": typ, "cve_id": cid, "title": cid, "tlp": "CLEAR",
              "sources": ["NVD"]}
    if rec.get("cvss_base") is not None:
        fm["cvss_base"] = rec["cvss_base"]
    if rec.get("cvss_version"):
        fm["cvss_version"] = str(rec["cvss_version"])
    if rec.get("severity"):
        fm["severity"] = rec["severity"]
    if rec.get("cwe"):
        fm["cwe"] = rec["cwe"] if len(rec["cwe"]) > 1 else rec["cwe"][0]
    fm["last_updated"] = today
    return fm


def _render(fm: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip() + \
        "\n---\n\n" + body.rstrip() + "\n"


def _write(path: Path, fm: dict, body: str, vault: Path, namespace: str,
           dry_run: bool) -> tuple[bool, list[str]]:
    problems = importer_guard.guard(fm, vault=vault, namespace=namespace)
    if problems:
        return False, problems
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(fm, body), encoding="utf-8")
    return True, []


def apply_record(vault: Path, rec: dict, page_model: str, *, stub_new: bool,
                 all_severities: bool, today: str, dry_run: bool) -> str:
    cid = rec.get("cve_id") or ""
    if not _CVE_RE.fullmatch(cid):
        return "skip"
    path = page_path(vault, cid, page_model)
    exists = path.exists()
    if not exists and not stub_new:
        return "skip"
    if not exists and not (all_severities or rec.get("severity") in _HIGH):
        return "skip"
    incoming = _frontmatter(rec, page_model, today)
    if exists:
        current, body = _read_page(path)
        if not current:
            return "rejected"
        candidate = dict(current)
        for key in _OWNED:
            candidate.pop(key, None)
        candidate.update({k: v for k, v in incoming.items() if k in _OWNED})
        if all(current.get(k) == candidate.get(k) for k in _OWNED - {"last_updated"}):
            return "unchanged"
        fm = candidate
    else:
        fm = incoming
        body = f"# {cid}\n\n{rec.get('description') or f'{cid} — see NVD.'}\n\n" \
               "> CVSS/CWE imported deterministically from NVD."
    _, namespace = _model(page_model)
    ok, problems = _write(path, fm, body, vault, namespace, dry_run)
    if not ok:
        print(f"nvd-import reject {cid}: {'; '.join(problems)}", file=sys.stderr)
        return "rejected"
    return "enriched" if exists else "created"


def observation_path(vault: Path, cve_id: str) -> Path:
    slug = cve_id.lower()
    namespace = "observations/nvd"
    existing = okf_migrate.find_page(vault, namespace, slug)
    return existing or vault / "wiki" / namespace / slug[0] / f"{slug}.md"


def apply_observation(vault: Path, rec: dict, *, all_severities: bool,
                      today: str, dry_run: bool) -> str:
    """Write the optional source-specific record used by multi-source assembly."""
    cid = rec.get("cve_id") or ""
    if not _CVE_RE.fullmatch(cid) or not (all_severities or rec.get("severity") in _HIGH):
        return "skip"
    reliability, credibility = "A", "2"
    try:
        schema = yaml.safe_load((vault / "schema.yaml").read_text(encoding="utf-8")) or {}
        source = (schema.get("source_registry") or {}).get("nvd") or {}
        reliability = str(source.get("reliability") or reliability)
        credibility = str(source.get("credibility_default") or credibility)
    except Exception:  # noqa: BLE001 - registry metadata has safe defaults
        pass
    fm = _frontmatter(rec, "vulnerability", today)
    fm.update({"source": "nvd", "reliability": reliability, "credibility": credibility,
               "canonical": cid.lower()})
    body = f"# {cid}\n\n{rec.get('description') or f'{cid} — see NVD.'}\n\n" \
           "> NVD source observation for deterministic canonical assembly."
    path = observation_path(vault, cid)
    ok, problems = _write(path, fm, body, vault, "observations/nvd", dry_run)
    if not ok:
        print(f"nvd-import observation reject {cid}: {'; '.join(problems)}", file=sys.stderr)
        return "rejected"
    return "written"


def backfill_targets(vault: Path, page_model: str, reenrich: bool) -> list[str]:
    _, namespace = _model(page_model)
    base = vault / "wiki" / namespace
    out = []
    for path in sorted(base.rglob("*.md")) if base.exists() else []:
        try:
            fm, _ = _read_page(path)
        except OSError:
            continue  # a concurrent reshard/removal is normal during a vault scan
        cid = str(fm.get("cve_id") or "").strip().upper()
        if _CVE_RE.fullmatch(cid) and (reenrich or fm.get("cvss_base") in (None, "")
                                      or fm.get("severity") in (None, "")):
            out.append(cid)
    return list(dict.fromkeys(out))


def run_backfill(args, api_key: str | None) -> dict:
    targets = backfill_targets(Path(args.vault), args.page_model, args.reenrich)
    if args.limit:
        targets = targets[:args.limit]
    counts = {"targeted": len(targets), "enriched": 0, "unchanged": 0,
              "missing": 0, "rejected": 0, "errors": 0}
    for cid in targets:
        try:
            rec = fetch_one(cid, api_key)
            if rec is None:
                counts["missing"] += 1
                continue
            result = apply_record(Path(args.vault), rec, args.page_model, stub_new=False,
                                  all_severities=True, today=date.today().isoformat(),
                                  dry_run=args.dry_run)
            counts[result if result in counts else "rejected"] += 1
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            print(f"nvd-backfill WARN {cid}: {exc}", file=sys.stderr)
            if args.strict:
                raise
        time.sleep(0.7 if api_key else 6.5)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shared bounded NVD enrichment lane")
    parser.add_argument("--vault", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    parser.add_argument("--page-model", choices=("cve", "vulnerability"),
                        default=os.environ.get("NVD_PAGE_MODEL", "cve"))
    parser.add_argument("--days", type=int, default=int(
        os.environ.get("NVD_DAYS") or os.environ.get("OKPACK_SEC_NVD_DAYS") or "7"))
    parser.add_argument("--max-pages", type=int, default=int(os.environ.get("NVD_MAX_PAGES", "5")))
    parser.add_argument("--stub-new", action="store_true", default=_truthy(os.environ.get("NVD_STUB_NEW")))
    parser.add_argument("--all-severities", action="store_true")
    parser.add_argument("--backfill", action="store_true", default=_truthy(
        os.environ.get("NVD_BACKFILL") or os.environ.get("OKPACK_SEC_NVD_BACKFILL")))
    parser.add_argument("--reenrich", action="store_true", default=_truthy(os.environ.get("NVD_REENRICH")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("NVD_LIMIT", "0")))
    parser.add_argument("--src", help="local NVD API JSON fixture instead of network")
    parser.add_argument("--observations", action="store_true",
                        default=_truthy(os.environ.get("NVD_OBSERVATIONS") or
                                        os.environ.get("OKPACK_SEC_OBSERVATIONS")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true", default=_truthy(os.environ.get("NVD_STRICT")))
    args = parser.parse_args(argv)
    api_key = os.environ.get("NVD_API_KEY") or None
    try:
        if args.backfill:
            counts = run_backfill(args, api_key)
        else:
            if args.src:
                data = json.loads(Path(args.src).read_text(encoding="utf-8"))
                records = [nvd_record(row.get("cve") or {}) for row in data.get("vulnerabilities") or []]
            else:
                records = fetch_recent(args.days, api_key, max_pages=args.max_pages)
            if args.observations and args.page_model != "vulnerability":
                parser.error("--observations requires --page-model vulnerability")
            counts = ({"total": len(records), "written": 0, "skip": 0, "rejected": 0}
                      if args.observations else
                      {"total": len(records), "enriched": 0, "created": 0,
                       "unchanged": 0, "skip": 0, "rejected": 0})
            for rec in records:
                if args.observations:
                    result = apply_observation(Path(args.vault), rec,
                                               all_severities=args.all_severities,
                                               today=date.today().isoformat(), dry_run=args.dry_run)
                else:
                    result = apply_record(Path(args.vault), rec, args.page_model,
                                          stub_new=args.stub_new,
                                          all_severities=args.all_severities,
                                          today=date.today().isoformat(), dry_run=args.dry_run)
                counts[result if result in counts else "skip"] += 1
        print(json.dumps({"wakeAgent": False, "nvd": counts}, sort_keys=True))
        failures = counts.get("errors", 0) + counts.get("rejected", 0)
        return 1 if args.strict and failures else 0
    except Exception as exc:  # noqa: BLE001
        print(f"nvd-import {'ERROR' if args.strict else 'WARN'}: {exc}", file=sys.stderr)
        return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
