Competitive quadrant synthesis. The `select_competitor_quadrants.py` digest above lists each
watchlist segment, its two axes, and the competitor entities with their data + recent activity.

For each segment, write/update `dashboards/competitive/quadrant-<segment>.md` (`type: dashboard`):

> **Write to the exact `dashboards/competitive/...` path.** `dashboards/` accepts `type: dashboard` pages via the enforced write path (that is where these dashboards live) — do NOT relocate them to `briefings/` or another namespace, even if the schema's namespace list appears to omit `dashboards/`.

- **Position from EVIDENCE.** Place every named competitor on the segment's two axes using its
  entity data + activity — not priors. If a competitor has no entity page yet, note it as a
  coverage gap; do NOT fabricate a position.
- **Emit the chart data.** Alongside the prose, the frontmatter MUST carry a render-ready panel
  (the reader/cockpit draw it — okengine#160):

  ```yaml
  panel:
    kind: two-axis
    x_label: <short x-axis name, e.g. "Mechanism overlap →">
    y_label: <short y-axis name, e.g. "Distribution ↑">
    nodes:
      - {label: <Competitor>, slug: <entity-slug>, x: 0.85, y: 0.9}
  ```

  One node per competitor you placed in the prose — same evidence, projected to numbers: x and y
  in [0,1], consistent with the quadrant + qualitative rating you wrote (Quadrant I upper-right ⇒
  both ≥ 0.5; "Very High" ≈ 0.9, "High" ≈ 0.7, "Moderate" ≈ 0.5, "Low" ≈ 0.3). Spread nodes
  within a cell — no two competitors on the same point. The prose stays the source of truth; the
  panel is its projection. Do NOT include coverage-gap competitors (no position = no node).
- Describe the four quadrant cells and who sits where, and call out **movers** vs the prior refresh.
- `[[wikilink]]` every competitor and cite the activity that justifies its placement.

Practitioner-grade and terse; assume the reader knows the segment. Write via the MCP write path.
LOCAL-ONLY: no web tools. End with a one-line summary of the quadrants written.
