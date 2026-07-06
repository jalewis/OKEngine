Sector battle-card synthesis. The `select_sector_battle_cards.py` digest above lists each watchlist
segment and its competitor entities with data + recent activity.

For each segment, write/update `dashboards/competitive/battle-cards-<segment>.md` (`type: dashboard`)

> **Write to the exact `dashboards/competitive/...` path.** `dashboards/` accepts `type: dashboard` pages via the enforced write path (that is where these dashboards live) — do NOT relocate them to `briefings/` or another namespace, even if the schema's namespace list appears to omit `dashboards/`.
— one card per competitor:

- **Positioning** (one line), **key strengths**, **weaknesses / gaps**, and the **differentiator
  vs the segment** — each grounded in the entity data + recent activity, with `[[wikilinks]]`.
- Skip a competitor with no real evidence rather than inventing strengths.
- Close with a one-paragraph "state of the segment" (who's gaining, who's exposed).

Practitioner-grade and terse. Write via the MCP write path. LOCAL-ONLY: no web tools. End with a
one-line summary.
