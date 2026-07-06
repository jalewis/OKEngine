# okengine.messaging-synthesis

Generic **vendor positioning / messaging synthesis** over a vault's competitive intel graph —
outbound **content pegs**, honest **positioning battle-cards** ("us vs them", distinct from
`okengine.competitive-analytics`'s competitor-vs-competitor cards), a **value-prop gap** tracker,
and a **messaging-synthesis** meta-layer that compresses the other three into one recommendation.
Ported from the origin system (okengine#152).

## Two-tier model (read this first)

The **synthesis math is generic** (the public adoption layer); the **product identity is yours**
and stays as pack/operator config. **The extension ships ZERO product identity — no vendor name,
no capability claims.** Most okengine vaults are pure market-*observers* with nothing to sell;
this extension is only useful for a vault that also tracks its own product. Absent config, every
operation's wake-gate reports "no product configured" and stays silent — nothing here assumes or
fabricates a vendor identity for the vault it runs in.

## The product anchor (pack/operator config — NOT shipped)

Point `PRODUCT_ANCHOR_PATH` at a YAML file (default `<vault>/config/product-anchor.yaml`):

```yaml
product_name: "Acme Shield"
capability_pages:                     # entity/concept slugs — the source of truth for what
  - concepts/acme-suite-architecture  # capabilities may be CLAIMED. A wedge not visible here
  - entities/vendor/acme-shield       # gets dropped, never invented.
watchlist_segments:                   # keys into okengine.competitive-analytics's watchlist —
  - direct-competitors                # who "we" message against. Requires that extension's
                                       # config/competitive-watchlist.yaml to resolve competitors;
                                       # degrades gracefully (empty segments) if absent.
home_entity: entities/vendor/acme-shield   # optional — if the vault also tracks "us" as an entity
```

An absent/empty anchor is a clean no-op (every op's wake-gate SKIPS). Without
`watchlist_segments` resolving to real competitors, `content-pegs` and `value-prop-gap-refresh`
still run (market-wide / capability-only), but `positioning-battle-cards` has nothing to build
cards against and stays silent too.

## Operations

| Op | What | Needs product anchor | Needs watchlist | Agent? |
|---|---|---|---|---|
| `content-pegs` | turn the week's watchlist-relevant sources into outbound content angles | yes | soft | agent |
| `positioning-battle-cards` | "us vs them" cards per (competitor, segment), drift-gated | yes | yes | agent |
| `value-prop-gap-refresh` | HIGH/MED/LOW capability gaps vs competitor moves, drift-gated | yes | soft | agent |
| `messaging-synthesis` | meta-layer: synthesizes the above 3 into one positioning brief | yes | soft | agent |

All four are wake-gated agent lanes (selector marshals the data → digest → the agent synthesizes;
none write anything the wake-gate didn't ground in a real page). `messaging-synthesis` is the
only one with a *dependency* on the others — its wake-gate only fires when at least one of the
other three has produced something newer than the last brief, so it naturally stays quiet until
the upstream lanes have real deltas to synthesize.

### Honesty rules (the whole point of this extension)

- A claimed wedge (in a battle-card or the messaging brief's "Hero wedge"/"Supporting wedges")
  **MUST be visible on a `capability_pages` anchor** — can't find it there, drop it. No invented
  product features.
- The messaging brief's "What NOT to claim" section **MUST come from `value-prop-gap-refresh`'s
  output, verbatim** — never softened, never invented.
- No customer-count / analyst-rating / "validated by" claims unless a capability-anchor page
  actually states one.

## Outputs

Written as the pack's `briefing`-aliased types (`marketing-pulse`, `battle-card`,
`value-prop-snapshot`, `messaging-brief`) under `briefings/` — most packs already carry these
aliases from the origin-system-derived schema lineage; if yours doesn't, add
`<type>: briefing` entries to `schema.yaml`'s type-alias block (see `okpack-cyber-market` for the
pattern). Writes via `okengine-write`.

## Enable

```bash
framework extensions enable <pack> okengine.messaging-synthesis
# then supply config/product-anchor.yaml (or set PRODUCT_ANCHOR_PATH) naming YOUR product
```

`trust: in-gateway`, `tier: analyze` (runs after the compile lanes). Pairs well with
`okengine.competitive-analytics` (same watchlist format, `watchlist_segments` reads its
`segments:` keys) but doesn't hard-require it — `positioning-battle-cards` just has nothing to
build against without it.

## Not yet (follow-up)

The board-question-corpus integration that the origin system's messaging-synthesis had (matching per-buyer
messaging lines to a pre-curated canonical-phrasing corpus, fed by a `weekly-question-corpus-
extract` lane) is not ported — no such corpus lane exists in okengine yet. Per-buyer translation
(CISO/board/CFO framing) is likewise deferred; the current brief produces one thesis + hero wedge,
not per-audience variants.
