#!/usr/bin/env python3
"""scope-classify — the model half of the relevance gate (okengine#167). no_agent; calls the
model DIRECTLY through the vendored llm_lib (reasoning off by default — the call-discipline
contract), NOT an agent session: single-label classification needs no tools, no vault writes
beyond the flag, and a cheap local model (qwen-class) is sufficient.

Consumes the QUEUE scope-prescore writes (wiki/.scope-queue.json — classify never
re-scans the corpus; the scan alone blew the ~120s script timeout on a 36k-source vault), asks the model
in-scope / out-of-scope / uncertain against the operator's scope statement, and:
  - out-of-scope -> reversible `off_scope: true` flag (propose/dispose: the model only labels;
    this script holds the pen deterministically);
  - in-scope / uncertain -> KEPT (on_uncertain: keep is the contract — a model that can't commit
    defers to the operator, never guess-flags);
  - writes `dashboards/scope-classify.md` — the verdict log.

Needs OKENGINE_LLM_BASE_URL + OKENGINE_LLM_MODEL (deployment .env). Absent -> no-op LOUDLY
(prescore still covers the clear cases).

Env: WIKI_PATH (/opt/vault) · SCOPE_CLASSIFY_BATCH (12)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_lib  # noqa: E402
import llm_lib    # noqa: E402  (vendored — reasoning-off default, truncation raises)

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
BATCH = int(os.environ.get(
    "SCOPE_CLASSIFY_BATCH",
    os.environ.get("OKENGINE_RELEVANCE_GATE_SCOPE_CLASSIFY_BATCH", "12"),
))


def main() -> int:
    scope = scope_lib.load_scope(VAULT)
    if scope is None:
        print("scope-classify: no pack_config.scope — no-op (see scope-prescore).")
        print(json.dumps({"wakeAgent": False}))
        return 0
    if not os.environ.get("OKENGINE_LLM_BASE_URL") or not os.environ.get("OKENGINE_LLM_MODEL"):
        print("scope-classify: OKENGINE_LLM_BASE_URL / OKENGINE_LLM_MODEL not set — the model "
              "half is OFF (prescore still flags clear cases). Set both in the deployment .env "
              "to activate. No-op.")
        print(json.dumps({"wakeAgent": False}))
        return 0

    statement = scope.get("statement", "")
    out_desc = "; ".join(str(x) for x in (scope.get("out_of_scope") or [])[:6])
    queue_f = VAULT / "wiki" / ".scope-queue.json"
    if not queue_f.is_file():
        print("scope-classify: no queue (wiki/.scope-queue.json) — run scope-prescore first. No-op.")
        print(json.dumps({"wakeAgent": False}))
        return 0
    queue = json.loads(queue_f.read_text(encoding="utf-8"))
    pending = list(queue.get("ambiguous") or [])

    # Own the clock: the host cron budget (HERMES_CRON_SCRIPT_TIMEOUT) kills the PROCESS with zero
    # progress; under load a local model runs 30-60s/call, so we stop OURSELVES early, save the
    # queue, and exit 0 — every run makes progress no matter how slow the box is.
    budget = int(os.environ.get("SCOPE_TIME_BUDGET", "300"))
    start = time.monotonic()

    flagged, kept, uncertain, errors = [], [], [], 0
    examined = 0
    rest = []
    it = iter(enumerate(pending))
    for i, rel in it:
        if examined >= BATCH or time.monotonic() - start > budget:
            rest.extend(pending[i:])      # everything untouched goes straight back
            break
        p = VAULT / "wiki" / f"{rel}.md"
        if not p.is_file():
            continue
        fm, blob = scope_lib.page_blob(p)
        if not blob or fm.get("off_scope"):
            continue
        examined += 1
        prompt = (f"A knowledge vault tracks: {statement}\nOut of scope: {out_desc}.\n\n"
                  f"Source page — slug: {p.stem}\ntitle: {fm.get('title') or ''}\n"
                  f"excerpt: {blob[:300]}\n\nIs this source IN scope for the vault?")
        try:
            verdict = llm_lib.classify(prompt, ["in-scope", "out-of-scope"],
                                        timeout=60, retries=0)   # a stuck call re-queues, never stalls the run
        except llm_lib.LLMError as e:
            errors += 1
            rest.append(rel)              # transient error: keep it queued
            if errors >= 3:
                print(f"scope-classify: stopping after 3 model errors ({e}) — progress saved")
                rest.extend(r for _, r in it)
                break
            continue
        if verdict == "out-of-scope":
            if scope_lib.flag(p, "model-classified out-of-scope vs pack_config.scope (scope-classify)"):
                flagged.append(rel)
        elif verdict == "in-scope":
            kept.append(rel)
        else:
            uncertain.append(rel)         # on_uncertain: keep — the operator's queue, not a guess

    queue["ambiguous"] = rest
    queue_f.write_text(json.dumps(queue), encoding="utf-8")

    dash = VAULT / "wiki" / "dashboards" / "scope-classify.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    L = ["---", "type: dashboard", 'title: "Scope classify (relevance gate — model verdicts)"',
         f"updated: {today}", "---", "", f"# Scope classify — {today}", "",
         f"_Model verdicts on the ambiguous middle ({examined} examined, batch cap {BATCH}): "
         f"{len(flagged)} flagged off_scope · {len(kept)} in-scope · {len(uncertain)} uncertain "
         f"(KEPT — review below). The model only labels; the deterministic script holds the pen; "
         f"flags are reversible._", ""]
    for title, rows in (("Flagged off-scope", flagged), ("Uncertain — kept, operator review", uncertain)):
        if rows:
            L += [f"## {title}", ""] + [f"- [[{r}]]" for r in rows] + [""]
    dash.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"scope-classify: {int(time.monotonic()-start)}s used of {budget}s budget · {examined} examined · {len(flagged)} flagged · {len(kept)} in · "
          f"{len(uncertain)} uncertain-kept · {errors} model error(s) -> dashboards/scope-classify.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
