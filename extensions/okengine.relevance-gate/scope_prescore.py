#!/usr/bin/env python3
"""scope-prescore — deterministic half of the relevance gate (okengine#167). no_agent, no model.

Scans source pages in the lookback window against `pack_config.scope` and:
  - flags CLEAR out-of-scope pages (`off_scope: true` + which terms matched — reversible, never a
    delete) — clear means: out-terms hit, ZERO in-terms hit. Anything softer stays untouched;
  - leaves AMBIGUOUS pages (no term hit either way) for the scope-classify lane;
  - writes `dashboards/scope-audit.md` — the "what got filtered and why" view that makes the
    boundary a visible, tunable dial instead of a guess (the operator widens/narrows the ONE
    scope config, not per-page fights).

No scope declared -> no-op LOUDLY (this gate never invents a boundary). Err-toward-keep is
structural: in-terms always win, both-sides terms count as in, uncertain is kept.

Env: WIKI_PATH (/opt/vault) · SCOPE_LOOKBACK_DAYS (7; 0 = the whole corpus, for backlog passes)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
LOOKBACK = int(os.environ.get(
    "SCOPE_LOOKBACK_DAYS",
    os.environ.get("OKENGINE_RELEVANCE_GATE_SCOPE_LOOKBACK_DAYS", "7"),
))


def _recent(fm: dict, cutoff: date | None) -> bool:
    if cutoff is None:
        return True
    for k in ("ingested", "created", "published"):
        v = fm.get(k)
        s = str(v)[:10] if v else ""
        try:
            return datetime.strptime(s, "%Y-%m-%d").date() >= cutoff
        except ValueError:
            continue
    return False


def main() -> int:
    scope = scope_lib.load_scope(VAULT)
    if scope is None:
        print("scope-prescore: NO pack_config.scope declared in schema.yaml — the gate will not "
              "invent a boundary. Declare scope (statement/in_scope/out_of_scope/on_uncertain) "
              "to activate. No-op.")
        print(json.dumps({"wakeAgent": False}))
        return 0
    in_t, out_t = scope_lib.compile_scope(scope)
    cutoff = None if LOOKBACK == 0 else date.today() - timedelta(days=LOOKBACK)

    flagged, ambiguous, kept_in, seen = [], [], 0, 0
    already = 0
    for p in (VAULT / "wiki" / "sources").rglob("*.md"):
        if p.name.startswith(("_", ".")) or p.stem.upper().startswith("INDEX"):
            continue
        fm, blob = scope_lib.page_blob(p)
        if not blob or not _recent(fm, cutoff):
            continue
        seen += 1
        if fm.get("off_scope"):
            already += 1
            continue
        ins, outs, out_terms = scope_lib.score(blob, in_t, out_t)
        rel = p.relative_to(VAULT / "wiki").as_posix()[:-3]
        if ins > 0:
            kept_in += 1
        elif outs > 0:
            reason = f"out-of-scope terms {out_terms[:4]} with zero in-scope signal (scope-prescore)"
            if scope_lib.flag(p, reason):
                flagged.append((rel, out_terms[:4]))
        else:
            ambiguous.append(rel)

    # hand the ambiguous middle to scope-classify as a QUEUE — classify must not re-scan the
    # corpus (a 36k-source vault made the scan alone blow cron-plus's ~120s script timeout).
    queue = VAULT / "wiki" / ".scope-queue.json"
    queue.write_text(json.dumps({"generated": date.today().isoformat(),
                                 "ambiguous": ambiguous}), encoding="utf-8")

    dash = VAULT / "wiki" / "dashboards" / "scope-audit.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    L = ["---", "type: dashboard", 'title: "Scope audit (relevance gate)"', f"updated: {today}",
         "---", "", f"# Scope audit — {today}", "",
         f"_The relevance gate's visible dial (okengine#167). Window: "
         f"{'whole corpus' if cutoff is None else f'last {LOOKBACK}d'} · {seen} source(s) scanned "
         f"· {kept_in} clearly in-scope · {len(flagged)} flagged off_scope this run · {already} "
         f"previously flagged · {len(ambiguous)} ambiguous (queued for scope-classify). Flags are "
         f"REVERSIBLE frontmatter markers — nothing is deleted. Tune by editing "
         f"`pack_config.scope`, not by fighting pages._", ""]
    L += [f"**Scope:** {scope.get('statement', '(no statement)')}", ""]
    if flagged:
        L += ["## Flagged off-scope this run", "", "| Page | matched out-terms |", "|---|---|"]
        L += [f"| [[{r}]] | {', '.join(t)} |" for r, t in flagged]
        L.append("")
    if ambiguous:
        L += [f"## Ambiguous (no term signal either way — scope-classify decides, uncertain stays)",
              ""] + [f"- [[{r}]]" for r in ambiguous[:50]]
        if len(ambiguous) > 50:
            L.append(f"- … and {len(ambiguous) - 50} more")
        L.append("")
    dash.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"scope-prescore: {seen} scanned · {kept_in} in · {len(flagged)} flagged · "
          f"{len(ambiguous)} ambiguous -> dashboards/scope-audit.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
