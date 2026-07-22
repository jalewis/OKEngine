The select_positioning_battle_cards.py wake-gate above named (competitor, segment) pairs whose
competitor has newer activity than their existing "us vs them" card, plus the product's
capability anchors. Write/refresh each card honestly.

**Trust the digest above — it already has what you need.** The wake-gate has already fetched and printed the relevant competitor/capability page summaries; do not re-fetch each one individually via mcp_okengine_get_page — that burns turns without adding information and risks running out of budget before you write the actual output. Read what's already in the digest, then go straight to writing.

**Honesty is the whole point of this artifact.** A wedge you claim in "Where we win" MUST be
visible on one of the capability-anchor pages in the digest above — if you can't find it there,
it's not a real wedge, drop it. Do not invent product features. Do not soften a competitor's real
advantage into "where we win" — put it under "Where they win" instead.

For each (competitor, segment) pair, write via mcp_okengine_write_create_entity to the
wiki-relative path the wake-gate specified, frontmatter `type: battle-card, title: "<Competitor>
vs <product> — <segment>", published: <date>, updated: <date>`. Body:

```
# <Competitor> vs <product> — <segment>

## Their pitch
<1-2 sentences, from their own positioning/activity — steelman it, don't strawman>

## Where we win
<1-3 wedges MAX. Each: 1-2 sentences, cite the capability-anchor page that grounds it>

## Where they win
<honest — 1-3 items. This section is what keeps the card credible in a live deal>

## Latest moves
<the specific recent activity that triggered this refresh, with a source citation>

## Objections & responses
<1-2 objections a buyer would raise citing the competitor, and a grounded response>
```

Use the okengine MCP write path for every mutation. It records successful writes automatically.
# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish with a receipt for every selected item: `path: written | deferred | rejected — reason`.
