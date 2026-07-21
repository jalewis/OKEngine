# Cross-lane cron ordering (#129)

> **Status:** Implemented. Phase 1 defines and validates the `after:` graph; Phase 2 enforces
> successful-completion freshness atomically at the cron-plus claim boundary.

## Problem

Extension/cron lanes get namespaced ids and duplicate-id failure, but only **wall-clock**
scheduling. A lane that must run **after** another (a scorer after an importer; frontier-watch's
`board→alert`; events' `ledger→score`; critic *after* the deliverable it reviews) can today only
**time-couple** (`17 5` vs `20 5`) — fragile, and not a real dependency: if the upstream slips or
overruns, the downstream runs on stale data and nothing notices.

## The contract: two distinct fields

- **`tier:`** — an *advisory* kickstart-stage hint (which stage a lane belongs to: ingest /
  compile / analyze / deliver …). Already exists; used to order the *first* fill and to read the
  fleet. **Not** a hard dependency.
- **`after: [<job-name>]`** *(new, #129)* — a **hard** cross-job dependency: "this lane consumes
  another lane's output, so it must run after it." Job names are the namespaced ids
  (`<ext-id>:<op>`). This is the real edge a scorer/critic/board lane needs.

## Phase 1 — the declaration + the validation gate *(implemented)*

- `after:` is accepted in the cron op contract (`extension.yaml` operations **and** the #63
  `crons/*.cron.json` drop-ins), validated as a list of job names, and carried onto the
  synthesized job (`extension_compose`).
- `cron_pack_split.validate_ordering(jobs)` builds the `after:` graph over the whole composed
  fleet and **fails loud** on: an `after:` target that names no job, a self-reference, or a
  **cycle** (Kahn topological sort; returns the topo order + errors). It's wired into:
  - **`regen()`** — the deploy refuses to write `cron-plus-jobs.json` with a broken/cyclic graph.
  - **`check`** — the round-trip self-test now also reports the `after:` graph (`✓ … after: graph
    acyclic (N dep edges)`), so CI catches a bad dependency.

This makes `after:` *mean something* — a declared dependency that's verified before it can ship —
without touching the Hermes scheduler. It does **not** yet change *when* jobs run.

## Phase 2 — runtime enforcement *(implemented)*

Two ways to make the order actually hold at runtime, in increasing fidelity:

1. **Topo-staggered schedule assignment (deploy-time, no scheduler change).** From the validated
   topo order, the deploy assigns staggered `expr` times within a shared run window so a lane's
   `after:` deps fire earlier in the same cycle. Cheap and deterministic; still clock-based (a
   gross overrun spills to the next cycle — acceptable for daily fleets, the next run reconciles).
2. **Successful-completion freshness (run-time, implemented).** A due lane with `after: [A]` is
   held at the scheduler's atomic claim boundary until **A completed successfully with output the
   downstream has not already consumed**. Held jobs remain due and retry on the next scheduler
   tick. Claiming an upstream clears its prior success value, closing the start-vs-completion race;
   failed upstream or downstream runs do not consume freshness. Multiple dependencies are an
   all-of gate. Runtime markers are scheduler state and are stripped from source artifacts.

This is deliberately eventual (next scheduler tick), not an immediate DAG dispatch. It is robust
to slips, overruns, failures, and different lane cadences without coupling OKEngine to a full DAG
executor. The deployment still validates missing targets and cycles before this runtime boundary.

## Relationship to #109 (kickstart ordering)

`tier:` is the kickstart-stage axis (the first-fill order); `after:` is the steady-state
data-dependency axis. They compose: kickstart walks tiers; steady-state respects `after:`. #129
owns the latter.
