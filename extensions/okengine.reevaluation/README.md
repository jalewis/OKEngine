# okengine.reevaluation — dependency-aware reevaluation (CHE core)

The mechanics that make continuous reassessment *dependency-aware* instead of a relabeled
document refresh (`drafts/che-evaluation.md` §3/§6 — internal doc, snapshot-excluded):

1. **Edge index** (`edge_index.py`, this extension, okengine#234) — a `no_agent` lane
   rebuilding `wiki/.reevaluation-edges.json`: every OPEN proposition → every evidence page
   it cites (frontmatter ref fields + `evidence[].source` + body wikilinks). Runs every 6h,
   24 minutes before the regrade cycle.
2. **Dependency-aware selector** (okengine#235, shipped) — walks changed-sources-since-
   watermark through the edge index so the regrade agent gets *"sources THIS proposition
   cites changed"* instead of the all-open × all-recent cross-join.

The `continuous-hypothesis` application profile (#247) now validates that every bound proposition
class is included in this index and has declared reassessment, resolution, and measurement
operations. The profile supplies composition; this extension remains the reusable dependency
mechanism.

**Guardrail:** scheduled sweep, never event-driven — no event substrate, no trigger
dispatcher, no second store (the containment line from the CHE evaluation).

**Domain-agnostic:** proposition types, open statuses, and ref fields are config
(`OKENGINE_REEVAL_*` env); a pack adds its own proposition class (e.g. a CTI diagnostic
type, okengine#236) with zero engine change. The artifact is a machine sidecar in the
`.backlinks.json` class — never canonical content, no governed-write involvement.
