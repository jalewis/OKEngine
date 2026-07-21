#!/usr/bin/env python3
"""framework review — human-in-the-loop sign-off (okengine#69).

  framework review <pack>                                   # show the current review queue
  framework review <pack> --approve <wiki-path> --by NAME   # approve the current page version
  framework review <pack> --decision request-changes --page <path> --by NAME --note TEXT
  framework review <pack> --migrate [--apply]               # legacy needs_review backlog

Approval sets `reviewed_by` / `reviewed_on` THROUGH the enforced MCP write path (write_server._update
— validates, bumps version, appends to wiki/log.md), never a bypass. The page then drops off the
review queue until it's edited again (which returns it for re-review). The queue itself is built by
the review-queue cron (review_queue.py) into wiki/dashboards/review-queue.md.
"""
from __future__ import annotations

import argparse
from collections import Counter
import os
import sys
from pathlib import Path
import re
import yaml

_HERE = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="framework review")
    ap.add_argument("pack", type=Path, help="pack / vault directory")
    ap.add_argument("--approve", metavar="WIKI_PATH",
                    help="sign off a page (wiki-relative, e.g. entities/a/acme)")
    ap.add_argument("--page", metavar="WIKI_PATH", help="page for --decision")
    ap.add_argument("--decision", choices=["approve", "request-changes", "reject", "dismiss", "defer"])
    ap.add_argument("--note", default="", help="decision rationale (required except for approval)")
    ap.add_argument("--by", metavar="NAME", help="reviewer name (required for every decision)")
    ap.add_argument("--migrate", action="store_true", help="inventory legacy needs_review pages")
    ap.add_argument("--apply", action="store_true", help="create missing review records during migration")
    args = ap.parse_args(argv)
    os.environ["WIKI_PATH"] = str(args.pack.expanduser())

    if args.migrate:
        sys.path.insert(0, str(_HERE.parent / "okengine-mcp"))
        try:
            import write_server as ws
        except Exception as e:
            print(f"ERROR: cannot load the write path: {e}", file=sys.stderr)
            return 1
        pages, reasons, types, ages, created, historical, historical_created = 0, Counter(), Counter(), Counter(), 0, 0, 0
        existing = {p.name for p in ws._review_store().rglob("*.yaml")} if ws._review_store().is_dir() else set()
        fm_re = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)
        for p in (args.pack.expanduser() / "wiki").rglob("*.md"):
            if "operational" in p.parts:
                continue
            try:
                match = fm_re.match(p.read_text(encoding="utf-8", errors="replace"))
                fm = yaml.safe_load(match.group(1)) if match else {}
            except Exception:
                continue
            if not isinstance(fm, dict):
                continue
            if fm.get("needs_review") is True:
                pages += 1; types[str(fm.get("type") or "unknown")] += 1
                derived = ws._structured_review_reasons(fm, match.group(2) if match else "")
                for reason in derived: reasons[str(reason.get("code") or "legacy-unspecified")] += 1
                stamp = str(fm.get("last_updated") or fm.get("updated") or fm.get("created") or "")[:10]
                try:
                    age = (ws.datetime.date.today() - ws.datetime.date.fromisoformat(stamp)).days
                    ages["0-30d" if age <= 30 else "31-90d" if age <= 90 else "91-365d" if age <= 365 else ">365d"] += 1
                except (TypeError, ValueError):
                    ages["unknown"] += 1
                if args.apply:
                    rec = ws._ensure_review_request(p)
                    name = ws._review_record_path(rec["review_id"]).name
                    if name not in existing:
                        existing.add(name); created += 1
            if fm.get("reviewed_by") and fm.get("reviewed_on"):
                historical += 1
                subject = p.relative_to(args.pack.expanduser() / "wiki").as_posix()[:-3]
                legacy_id = f"review:{subject}:legacy:{str(fm['reviewed_on'])[:10]}"
                rp = ws._review_record_path(legacy_id)
                if args.apply and rp.name not in existing:
                    _, _, _, version, digest = ws._review_page_state(p)
                    evidence = fm.get("sources") or fm.get("source") or []
                    if not isinstance(evidence, list): evidence = [evidence]
                    stamp = str(fm.get("reviewed_at") or fm.get("reviewed_on"))
                    rec = {
                        "version": 1, "review_id": legacy_id, "subject": subject,
                        "subject_version": int(fm.get("reviewed_version") or version),
                        "subject_hash": digest, "state": "approved",
                        "reasons": [{"code": "legacy-signoff-import",
                                     "detail": "imported sign-off metadata; evidence examination is not asserted"}],
                        "evidence": [str(v) for v in evidence if str(v).strip()],
                        "requested_by": "migration", "requested_at": stamp,
                        "assigned_to": str(fm.get("reviewed_by")),
                        "decision_by": str(fm.get("reviewed_by")), "decision_at": stamp,
                        "decision_note": "Imported from legacy reviewed_* metadata; scope unknown.",
                        "decision_service": "migration", "machine_checks": [],
                        "history": [{"decision": "approve", "state": "approved",
                                     "decision_by": str(fm.get("reviewed_by")), "decision_at": stamp,
                                     "decision_note": "Imported legacy sign-off; evidence scope unknown.",
                                     "service": "migration"}],
                    }
                    rp.parent.mkdir(parents=True, exist_ok=True)
                    rp.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
                    existing.add(rp.name); historical_created += 1
        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"review migration {mode}: {pages} flagged page(s); {created} open record(s) created; "
              f"{historical} legacy sign-off(s); {historical_created} historical record(s) created")
        print("reasons: " + ", ".join(f"{k}={v}" for k, v in reasons.most_common()))
        print("types: " + ", ".join(f"{k}={v}" for k, v in types.most_common()))
        print("ages: " + ", ".join(f"{k}={v}" for k, v in ages.items()))
        return 0

    page = args.approve or args.page
    decision = "approve" if args.approve else args.decision
    if page or decision:
        if not page or not decision:
            print("ERROR: --decision requires --page", file=sys.stderr)
            return 2
        if not args.by:
            print("ERROR: review decisions require --by NAME", file=sys.stderr)
            return 2
        sys.path.insert(0, str(_HERE.parent / "okengine-mcp"))
        try:
            import write_server as ws
        except Exception as e:
            print(f"ERROR: cannot load the write path: {e}", file=sys.stderr)
            return 1
        target = ws._safe(page)
        if target is None or not target.is_file():
            print(f"ERROR: page not found: {page}", file=sys.stderr)
            return 1
        _, _, _, version, digest = ws._review_page_state(target)
        res = ws._resolve_review(page, decision, args.by, args.note, version, digest, service="cli")
        print(yaml.safe_dump(res, sort_keys=False).rstrip())
        return 0 if res.get("ok") else 1

    dash = args.pack.expanduser() / "wiki" / "dashboards" / "review-queue.md"
    if dash.is_file():
        print(dash.read_text(encoding="utf-8"))
    else:
        print("no review-queue.md yet — run the review-queue cron (review_queue.py) to build it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
