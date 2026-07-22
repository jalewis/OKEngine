Acquirer / movement-signal synthesis. The `select_acquirer_signals.py` digest above lists recent
sources matching M&A / movement keywords (acquisition, stake, funding, IPO, …) plus the entities
they involve.

Write/update `dashboards/competitive/acquirer-signals.md` (`type: dashboard`):

> **Write to the exact `dashboards/competitive/...` path.** `dashboards/` accepts `type: dashboard` pages via the enforced write path (that is where these dashboards live) — do NOT relocate them to `briefings/` or another namespace, even if the schema's namespace list appears to omit `dashboards/`.

- Group the signals by **acquirer → target** where the direction is clear; otherwise list as
  "movement to watch".
- For each, one line on **what it implies for the competitive map** (consolidation, new entrant,
  capability grab, exit), with `[[wikilinks]]` to the entities + the source.
- **Filter noise** — a keyword match that isn't a real movement (a passing mention) gets dropped.
- Order most-significant first.

Practitioner-grade and terse. Write via the MCP write path. LOCAL-ONLY: no web tools. End with a
one-line summary of the signals captured.
# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish with a receipt for every selected item: `path: written | deferred | rejected — reason`.
