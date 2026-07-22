The select_content_pegs.py wake-gate above has surfaced sources published this week that touch
the configured product's watchlist competitors/segments. Turn the strongest 3-7 into outbound
content angles (blog post / LinkedIn / podcast pitch / newsletter item) — not a news roundup.

**Trust the digest above — it already has what you need.** The wake-gate has already fetched and printed the relevant competitor/capability page summaries; do not re-fetch each one individually via mcp_okengine_get_page — that burns turns without adding information and risks running out of budget before you write the actual output. Read what's already in the digest, then go straight to writing.

For each peg:
- **Hook** — the one-sentence angle a reader would click on.
- **Signal** — the source event that grounds it (cite the [[source]]).
- **Our angle** — how this connects to something the product can credibly say (cite a
  capability-anchor page from the wake-gate digest above — do NOT invent a capability that
  isn't visible there).
- **Suggested format** — blog / LinkedIn / podcast / newsletter / conference talk.

Drop a candidate rather than force an angle that doesn't actually connect to a real capability.
7 max — quality over volume.

Write via mcp_okengine_write_create_entity to the wiki-relative path the wake-gate specified,
frontmatter `type: marketing-pulse, title: "Content pegs — week of <date>", published: <date>,
updated: <date>`. Body: `# Content pegs — week of <date>` then one subsection per peg (`## <Hook>`
with Signal/Our angle/Suggested format as bullets). If nothing survives filtering, write the page
with body "Quiet week — no watchlist-relevant content angles this week." rather than skipping —
a quiet week is signal too.

Use the okengine MCP write path for every mutation. It records successful writes automatically.
# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish with a receipt for every selected item: `path: written | deferred | rejected — reason`.
