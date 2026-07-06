# okengine.frontier-watch

Applied capability-frontier + demand/supply **whitespace** lane (okengine#147). Surfaces
capabilities the market wants (referenced by many `sources/`) but few players supply (few
`entities/`), writes low-trust `whitespace-thesis` pages in its own `frontier/` namespace, and
briefs on the open frontier.

**Method (generic; feeds/segments are pack config).** Per capability (`concepts/<slug>`):
demand = distinct source pages referencing it, supply = distinct entity pages referencing it. A
candidate is demand-rich + supply-thin (`min_demand` / `max_supply`). The agent confirms the
demand is real and the supply gap is a market gap (not missing data), then writes a thesis with
a demand/supply-measured confidence.

**Relationship to okengine.lacuna (soft edge, no hard dependency).** lacuna is the generic
structural-gap *primitive* (names the *force* keeping a cell empty); frontier-watch is the
applied *lane*. When a whitespace-thesis has a nameable force AND lacuna is enabled, it's flagged
`lacuna_candidate: true` for lacuna's rigorous treatment — the same soft-convention pattern as
lacuna↔predictions. The thesis stands alone when lacuna isn't enabled.

**Built on the #63 cron drop-in model** — ops live in `crons/*.cron.json` (one op per file):
- `whitespace-sweep` — wake-gated (`select_whitespace.py`) weekly analyze op.
- `frontier-brief` — weekly brief over the `frontier/` theses.

Opt-in (spends model budget). Owns the `whitespace-thesis` type + `frontier` namespace.

**Deferred (follow-ups):** dedicated frontier-feed ingest (`frontier-feeds.opml` →
feed-fetch/ingest/map-refresh), the whitespace board + alert lanes, Telegram delivery wiring,
and the strict feed→ingest→sweep→board→alert ordering (needs okengine#129 `tier:`/`after:`;
today the schedules are clock-staggered).
