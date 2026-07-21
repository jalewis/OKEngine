# okengine.timeline

A first-party, **core** (default-on) extension that renders a reverse-chronological dashboard of
the vault's dated content to `wiki/dashboards/timeline.md`.

- **What:** scans knowledge pages for a date (`published:` for sources, else `updated:` /
  `created:`), groups them by month, and writes a timeline of `[[wikilinks]]` newest-first.
- **Shape:** deterministic `in-gateway` cron script (`build_timeline.py`) — no agent, no network,
  no model spend. Writes the schema-excluded `dashboards/` namespace directly, the same pattern as
  `okengine.contradictions`.
- **Why core:** cheap and useful on any OKF vault; default-ON, opt-out via
  `framework extensions disable <pack> okengine.timeline`.

Config: `max_entries` (default 300) caps the rows. Reads `WIKI_PATH` (vault root) and
`OKENGINE_TIMELINE_MAX_ENTRIES`.

Companion reference extensions: `okengine.contradictions` (deterministic dashboard, core),
`okengine.glossary` (agent, owns a schema type), `okengine.predictions` (multi-op agent lanes).
