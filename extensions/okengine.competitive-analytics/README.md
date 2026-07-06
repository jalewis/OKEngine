# okengine.competitive-analytics

Generic **competitive / market-structure analytics** over a vault's entity graph — competitor
**quadrants**, sector **battle-cards**, and **acquirer / movement signals**. (okengine#146)

## Two-tier model (read this first)

The **math is generic** (the public adoption layer); the **edge is yours** and stays as
pack/operator config. **The extension ships ZERO competitor seeds.** You supply a watchlist; the
extension turns it into analysis. Acceptance: it runs unchanged on any pack that supplies a
watchlist (e.g. `okpack-fintech`).

## The watchlist (pack/operator config — NOT shipped)

Point `WATCHLIST_PATH` at a YAML file (default `<vault>/config/competitive-watchlist.yaml`):

```yaml
segments:
  llm-providers:
    label: "Frontier LLM providers"
    competitors: [openai, anthropic, google-deepmind, meta-ai]   # entity slugs under entities/
    axes: {x: "model capability", y: "enterprise adoption"}
  inference-chips:
    label: "Inference silicon"
    competitors: [nvidia, broadcom, groq, cerebras]
    axes: {x: "perf/$", y: "ecosystem maturity"}
```

An absent/empty watchlist is a clean no-op (the quadrant/battle-card ops SKIP). `acquirer-signals`
needs no watchlist — it scans recent sources market-wide (keywords via `MOVEMENT_KEYWORDS`).

## Operations

| Op | What | Needs watchlist | Agent? |
|---|---|---|---|
| `competitor-quadrants` | position each segment's competitors on its two axes | yes | agent |
| `sector-battle-cards` | head-to-head cards (positioning / strengths / gaps) per segment | yes | agent |
| `acquirer-signals` | M&A / movement signals from recent sources, market-wide | no | agent |
| `discover-competitors` | propose off-watchlist competitor CANDIDATES from the ingested graph | partial | **no_agent** |

The first three are wake-gated agent lanes (selector marshals the data → digest → the agent synthesizes).

### `discover-competitors` — turn "I list my rivals" into "name myself, see the field"

Deterministic (no LLM). Surfaces companies the vault **already knows** (entities created by ingest)
that **aren't on your watchlist yet**, ranked by evidence, into
`dashboards/competitive/discovery.md`. Honest by construction: it **proposes candidates** — it never
fabricates a quadrant position and never auto-edits the watchlist; you promote the real ones.

Signals (all from the vault): **co-occurrence** (cited in the same `source` pages as your home company
/ tracked competitors), **segment match** (its `segment` is one you watch), **prominence** (how many
sources reference it), and **alternatives-language** (named in competitive phrasing near your home/tracked
names in source bodies — "alternatives to X", "X vs Y", "switch from X" — which catches rivals that
have **no entity yet**; shown in a separate lower-confidence section). Set an optional anchor in the watchlist:

```yaml
home: my-company          # your company's entity slug — anchors co-occurrence (optional)
segments: { … }
```

Env: `DISCOVERY_TYPES` (default `competitor,company,vendor,organization,identity`), `DISCOVERY_TOP`
(25), `DISCOVERY_MIN_SCORE` (1). No candidates yet just means add broader `feeds/` and let ingest run.

## Outputs

Written as the pack-owned **`dashboard`** type under **`dashboards/competitive/`** — so no schema
fragment is required (every pack already declares `dashboard`). Reads entities via the query MCP;
writes via `okengine-write`.

## Enable

```bash
framework extensions enable <pack> okengine.competitive-analytics
# then supply config/competitive-watchlist.yaml (or set WATCHLIST_PATH)
```

`trust: in-gateway`, `tier: analyze` (runs after the compile lanes; sequenced by schedule — #129
would formalize the dependency).

## Not yet (follow-up)

`value-prop-gap`, `stealth-discovery`, `trend-transitions`, `competitor-movement-ledger`, and a
`weekly-competitive-wrapper` are deferred (see #146), as is the formal #129 ordering. Companion:
`okengine.lacuna` (#145) is the *empty-cell* view to this *occupied-map* view.
