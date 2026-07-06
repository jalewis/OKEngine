# okengine.viz — strategic/concept-map visualizations (#156)

A no_agent extension that renders the vault's **concept graph** as a **Wardley map** (evolution ×
value-chain). Optional presentation layer; domain-agnostic; runs on any pack.

## The maturity-axis design

A true Wardley map needs two semantic axes that a bare concept graph can't reliably infer, so the
axes are **declarative with a graph fallback**:

- **x — evolution/maturity.** The concept's `evolution` field (config `evolution_field`), mapped on
  the Wardley scale `genesis < custom < product < commodity` (or a 0-1/0-100 number). **Fallback**
  when a concept lacks it: *ubiquity* — its inbound-reference percentile (heavily-cited ⇒ settled).
- **y — value-chain/foundational.** The concept's `value_chain` field (config `value_field`, 0-1).
  **Fallback:** *entity-coupling* — the percentile of distinct entity pages referencing it
  (deeply-coupled ⇒ load-bearing).

Heuristic-positioned nodes are labelled on the dashboard; enrich concepts with the two fields (a
pack/agent concern) for a true map. No cyber/any-domain knowledge in the extension.

## Output

`wiki/dashboards/wardley.md` (type `dashboard`): a self-declared `panel: {kind: two-axis}` + node
coordinates (the reader's two-axis kind renders the plane — okengine#160 P2) **and** a readable
quadrant breakdown + coordinate table, so the map is useful before the SVG render lands.

Covers the origin system's `wardley-map-refresh` job. Daily, script-only.
