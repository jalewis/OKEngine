The select_value_prop_refresh.py wake-gate above surfaced the product's capability anchors and
its watchlist competitors' recent moves. Re-run the gap analysis: where is the product exposed
given what competitors have shipped/claimed recently, and where has a prior gap closed.

**Trust the digest above — it already has what you need.** The wake-gate has already fetched and printed the relevant competitor/capability page summaries; do not re-fetch each one individually via mcp_okengine_get_page — that burns turns without adding information and risks running out of budget before you write the actual output. Read what's already in the digest, then go straight to writing.

For each capability area, compare what the product's anchor pages show against what the
watchlist competitors' recent activity shows. Classify each gap HIGH / MED / LOW by how much it
would hurt in a live competitive deal. Be honest — the whole point of this artifact is to keep
downstream messaging (battle-cards, the messaging brief) from overclaiming.

Write via mcp_okengine_write_create_entity to the wiki-relative path the wake-gate specified,
frontmatter `type: value-prop-snapshot, title: "Value-prop gap snapshot — <date>",
published: <date>, updated: <date>, prior_snapshot: "[[<prior-path-or-none>]]"`. Body:

```
# Value-prop gap snapshot — <date>

## Net position
<1 paragraph — where does the product stand overall right now>

## Gaps (HIGH)
<verbatim gap names — these get pulled into the messaging brief's "What NOT to claim" section
without paraphrasing, so name them precisely>

## Gaps (MED / LOW)
<same, lower severity>

## Closed since prior snapshot
<if a prior snapshot exists: which gaps no longer apply, and why>

## New since prior snapshot
<if a prior snapshot exists: which gaps are newly identified>
```

If no prior snapshot exists, omit the "Closed"/"New" sections and note "First snapshot — no
prior to diff against" instead.

Use the okengine MCP write path for every mutation. It records successful writes automatically.
# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish with a receipt for every selected item: `path: written | deferred | rejected — reason`.
