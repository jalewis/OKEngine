# okengine.dedupe

A first-party, **opt-in** extension that finds likely **duplicate entities** and proposes merges.

- **Detect (deterministic, wake-gate):** `select_dup_candidates.py` scans `wiki/entities/**` and
  groups pages whose normalized name collides, or whose name matches another page's `aliases:`.
  Prints the candidate groups + the `{"wakeAgent": bool}` gate — no candidates, no agent run.
- **Merge (agent lane):** `prompts/dedupe.md` reviews each group, confirms a true duplicate, and
  merges conservatively — absorb the loser's aliases/sources/activity into the canonical, then
  `tombstone_entity` the loser with `superseded_by`. All writes go through the enforced MCP guard.
- **Why opt-in (not core):** it spends model budget and *mutates* the graph, so it's enabled
  deliberately (`framework extensions enable <pack> okengine.dedupe`), unlike the read-only core
  dashboards (contradictions, timeline).

This complements the engine's *structural* canonicalization (`canonical-assemble`,
`normalize-entity-schema`) — dedupe is the **"same entity, different name"** semantic layer.
Detection is name/alias-based today; the `okengine.embeddings` sidecar adds semantic candidates.

Config: `max_groups` (default 25) caps groups surfaced/merged per run.
