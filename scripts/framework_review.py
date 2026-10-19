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
    ap.add_argument("--apply", action="store_true",
                    help="write changes during --migrate / --reconcile (both are dry runs without it)")
    ap.add_argument("--reconcile", action="store_true",
                    help="close ledger records that can no longer be actioned (okengine#842)")
    args = ap.parse_args(argv)
    os.environ["WIKI_PATH"] = str(args.pack.expanduser())

    if args.reconcile:
        return _reconcile(args)

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



def _reconcile(args) -> int:
    """Close review records that no surface can reach, without ever claiming anyone reviewed them.

    A record is actionable only while its page exists AND still requires review. Measured on a live
    vault, 3,440 of 3,891 open records were neither: the page's flag had been cleared elsewhere
    (autoverify, an edit, a decision under another record) but the record stayed `open`. The worklist
    lists only pages that currently require review, so those rows never appear and cannot be closed.

    Three dispositions, all `dismissed` -- `dismiss` judges the REQUEST, not the content, which is
    the honest verb when nobody examined the page. None is ever `approved`:

      subject-absent     the page no longer exists anywhere (exact path or reshard-proof key)
      cleared-elsewhere  the page exists but no longer requires review
      superseded         a newer open record covers the same page (the one-outstanding-row invariant)

    History is appended, never replaced, so each closure is auditable. Dry run unless --apply.
    """
    import datetime as _dt
    sys.path.insert(0, str(_HERE.parent / "src"))
    from okengine.review_policy import requires_review, review_required_types

    pack = args.pack.expanduser()
    wiki = pack / "wiki"
    store = wiki / "operational" / "reviews"
    if not store.is_dir():
        print(f"review reconcile: no ledger at {store} -- nothing to do")
        return 0
    required = review_required_types(pack)
    fm_re = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)

    def _key(subject):
        parts = [x for x in str(subject).removeprefix("wiki/").removesuffix(".md").split("/") if x]
        return (parts[0], parts[-1]) if len(parts) >= 2 else tuple(parts)

    # A filter, not a loop with `continue`: the index must not depend on rglob's traversal order,
    # which the filesystem does not guarantee. (A `break` in that loop would have silently dropped
    # every page walked after the first operational one.)
    by_key = {}
    for page in (p for p in wiki.rglob("*.md") if "operational" not in p.parts):
        by_key.setdefault(_key(page.relative_to(wiki).with_suffix("").as_posix()), page)

    def _page_for(subject):
        exact = wiki / (str(subject) + ".md")
        return exact if exact.is_file() else by_key.get(_key(subject))

    def _live(page):
        try:
            m = fm_re.match(page.read_text(encoding="utf-8", errors="replace"))
            fm = yaml.safe_load(m.group(1)) if m else {}
        except Exception:
            return True                       # unreadable -> keep the request; never close on doubt
        return requires_review(fm if isinstance(fm, dict) else {}, m.group(2) if m else "", required)

    open_recs = []
    for f in sorted(store.glob("*.yaml")):  # glob-ok: operational/reviews/ is a flat yaml dir (unsharded)
        try:
            rec = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("state") == "open":
            open_recs.append((f, rec))

    plan, groups = [], {}
    for f, rec in open_recs:
        page = _page_for(rec.get("subject", ""))
        if page is None:
            plan.append((f, rec, "subject-absent", None))
        elif not _live(page):
            plan.append((f, rec, "cleared-elsewhere", None))
        else:
            groups.setdefault(_key(rec.get("subject", "")), []).append((f, rec))
    kept = 0
    for recs in groups.values():
        recs.sort(key=lambda fr: str(fr[1].get("requested_at") or ""))
        keeper = recs[-1][1]
        kept += 1
        for f, rec in recs[:-1]:
            plan.append((f, rec, "superseded", keeper.get("review_id")))

    by_reason = Counter(reason for _, _, reason, _ in plan)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"review reconcile {mode}: {len(open_recs)} open record(s); {kept} stay open (one per live "
          f"page); {len(plan)} to close")
    for reason in ("cleared-elsewhere", "superseded", "subject-absent"):
        print(f"  {reason:<18} {by_reason.get(reason, 0)}")
    for f, rec, reason, by in plan[:5]:
        print(f"  e.g. {reason:<18} {rec.get('subject')}" + (f"  (by {by})" if by else ""))

    if args.apply:
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        notes = {
            "subject-absent": "Closed by reconciliation: the page no longer exists at its path or "
                              "under its reshard-proof key. Not a review of any content.",
            "cleared-elsewhere": "Closed by reconciliation: the page exists but no longer requires "
                                 "review; the flag was cleared elsewhere. Not a review of any content.",
            "superseded": "Closed by reconciliation: a newer open request covers the same page "
                          "(one outstanding request per page). Not a review of any content.",
        }
        for f, rec, reason, by in plan:
            note = notes[reason] + (f" Superseded by {by}." if by else "")
            event = {"decision": "dismiss", "state": "dismissed", "decision_by": "reconcile",
                     "decision_at": now, "decision_note": note, "service": "reconcile",
                     "reconcile_reason": reason}
            if by:
                event["superseded_by"] = by
            rec["history"] = list(rec.get("history") or []) + [event]
            rec.update({"state": "dismissed", "decision_by": "reconcile", "decision_at": now,
                        "decision_note": note, "decision_service": "reconcile"})
            f.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"review reconcile: closed {len(plan)} record(s)")
    else:
        print("dry run -- nothing written; re-run with --apply to close these records")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
