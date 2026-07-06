#!/usr/bin/env python3
"""operator_dashboard.py — the single operator HOME that rolls up engine + vault health (okengine#60).

The fleet writes ~a dozen specialized dashboards (fleet-health, source-grounding, review-queue,
conformance, kb-health, calibration, …). This aggregates them into ONE page with an overall
🟢/🟡/🔴, a per-area rollup with the headline metric + a drill-down link, and a freshness index
(every dashboard + how long since it refreshed — a stale dashboard means its cron stopped).

Deterministic (no_agent): reads each dashboard's frontmatter (title/updated) + a targeted headline
from the few KEY ones; degrades gracefully if a dashboard's format is unfamiliar. Writes
wiki/dashboards/operator.md.

Env: WIKI_PATH (/opt/vault) · OPERATOR_STALE_HOURS (36)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
DDIR = WIKI / "dashboards"
OUT = DDIR / "operator.md"
STALE_H = float(os.environ.get("OPERATOR_STALE_HOURS", "36"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)


def _read(p: Path):
    try:
        t = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    m = _FM.match(t)
    if not m:
        return {}, t
    fm = {}
    for line in m.group(1).splitlines():
        km = re.match(r'^([a-z_]+):[ \t]*"?([^"\n]*)"?', line)
        if km:
            fm[km.group(1)] = km.group(2).strip()
    return fm, m.group(2)


def _age_h(updated: str):
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2}):(\d{2})", str(updated))
    if m:
        try:
            dt = datetime(int(updated[:4]), int(updated[5:7]), int(updated[8:10]),
                          int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except ValueError:
            return None
    m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(updated))
    if m2:
        dt = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)), tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return None


def _num(rx, body, default=None, grp=1):
    m = re.search(rx, body)
    return m.group(grp) if m else default


def main() -> int:
    if not DDIR.is_dir():
        print("operator-dashboard: no dashboards/ yet")
        print(json.dumps({"wakeAgent": False}))
        return 0
    dashes = {}
    for p in sorted(DDIR.glob("*.md")):  # glob-ok: dashboards/ is a flat derived dir, not a sharded namespace
        if p.name in ("operator.md", "INDEX.md") or p.name.startswith(("_", "INDEX")):
            continue
        fm, body = _read(p)
        dashes[p.stem] = {"title": fm.get("title") or p.stem, "updated": fm.get("updated", ""),
                          "age": _age_h(fm.get("updated", "")), "body": body}

    # --- per-area rollup from the KEY dashboards (graceful if absent) ---
    rollup = []   # (status, area, metric, link)

    def add(area, link, status, metric):
        rollup.append((status, area, metric, link))

    fh = dashes.get("fleet-health")
    if fh:
        b = fh["body"]
        bad = sum(int(_num(rf"{k}: \*\*(\d+)\*\*", b, "0")) for k in ("stale", "errored", "off-model"))
        ok = _num(r'ok: (\d+)', b, '?')
        add("Fleet (cron lanes)", "fleet-health",
            "🔴" if bad else "🟢", f"{ok} ok, {bad} need attention")
    g = dashes.get("source-grounding")
    if g:
        pct = _num(r"grounded: \*\*\d+\*\* \((\d+)%\)", g["body"], "?")
        st = "🟢" if pct != "?" and int(pct) >= 80 else ("🟡" if pct != "?" and int(pct) >= 50 else "🔴")
        add("Knowledge grounding", "source-grounding", st, f"{pct}% of claims cite a real source")
    rq = dashes.get("review-queue")
    if rq:
        n = _num(r"\*\*(\d+) item\(s\) awaiting", rq["body"], "0")
        add("Human review queue", "review-queue", "🟡" if int(n) else "🟢", f"{n} awaiting a human")
    cf = dashes.get("conformance")
    if cf:
        viol = _num(r"source-refs-are-pages: \*\*(\d+)\*\*", cf["body"], "0")
        add("Conformance", "conformance", "🟡" if int(viol) else "🟢", f"{viol} content-rule violations")
    kb = dashes.get("kb-health")
    if kb:
        add("KB health", "kb-health", "🟢", "see dashboard")

    # --- freshness: a dashboard not refreshed in STALE_H means its cron stopped ---
    stale = [(n, d) for n, d in dashes.items() if d["age"] is not None and d["age"] > STALE_H]

    worst = "🔴" if any(s == "🔴" for s, *_ in rollup) else \
            ("🟡" if (any(s == "🟡" for s, *_ in rollup) or stale) else "🟢")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Operator — engine & vault health"', f"updated: {now}",
         "rail_top: true", "---", "",
         f"# Operator dashboard — {now}", "", f"## Overall: {worst}", ""]
    if rollup:
        L += ["| Area | Status | Headline | Detail |", "|---|---|---|---|"]
        for status, area, metric, link in rollup:
            L.append(f"| {area} | {status} | {metric} | [{link}]({link}.md) |")
        L.append("")
    if stale:
        L += [f"## ⚠ Stale dashboards (no refresh in >{int(STALE_H)}h — cron may be down)", ""]
        L += [f"- [{d['title']}]({n}.md) — {int(d['age'])}h ago" for n, d in stale] + [""]
    L += ["## All dashboards", "", "| Dashboard | Updated |", "|---|---|"]
    for n, d in sorted(dashes.items()):
        fresh = "" if d["age"] is None else f" ({int(d['age'])}h ago)"
        L.append(f"| [{d['title']}]({n}.md) | {d['updated']}{fresh} |")
    L.append("")
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"operator-dashboard: overall {worst}, {len(rollup)} areas, {len(stale)} stale -> "
          "wiki/dashboards/operator.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
