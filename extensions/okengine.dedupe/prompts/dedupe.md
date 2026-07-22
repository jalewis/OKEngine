Entity de-duplication. The `select_dup_candidates.py` digest above lists groups of entity pages
whose normalized name or alias collides — LIKELY duplicates of the same real-world entity. The
digest gives each member's exact `[[wiki-relative path]]`. Your FIRST response MUST be a tool
call: read a candidate group's pages with **`retrieve_context`** (the okengine READ MCP), passing
the **wiki-relative path** exactly as shown (e.g. `entities/t/foo`). Do NOT use the write server's
`read_resource` to read pages — it expects a URL and will error on a bare path.

For each candidate group:

1. **READ each member page** (via `retrieve_context`). Decide whether they are genuinely the SAME
   real-world entity — not
   merely similarly named. (e.g. a model named "GPT-5" and a *paper about* GPT-5 are DIFFERENT;
   "OpenAI" the lab and "openai/gpt-5" the model are DIFFERENT.) **If unsure, LEAVE THEM** — a
   wrong merge corrupts the graph, and a missed duplicate is cheap to catch next run.

2. **If they are the same entity,** pick the CANONICAL page (most complete / most-referenced /
   cleanest slug). Then for every OTHER member:
   - **Absorb into the canonical** what it lacks — append distinct `## Recent activity` lines,
     union the `sources:` list, and add the loser's name + aliases to the canonical's `aliases:` —
     via `mcp_okengine_write_update_entity` / `mcp_okengine_write_append_to_section` on the
     CANONICAL. For list fields (`sources:`, `aliases:`) read first, then send the COMPLETE
     merged list.
   - **Then tombstone the loser** with `mcp_okengine_write_tombstone_entity`, `superseded_by` →
     the canonical. Never delete; tombstoning retains the file as `status: tombstoned`, and
     `[[links]]` to it resolve onward via `superseded_by` — so do NOT rewrite other pages.

WRITE only via the MCP write path (never `file_write`). Be conservative and terse: skip any group
that isn't a true duplicate and note why in one line. LOCAL-ONLY — do not use web tools. End your
response with a one-line summary of what you merged; the MCP write path logs each change to
`wiki/log.md` automatically — do not write it yourself.
# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish with a receipt for every selected item: `path: written | deferred | rejected — reason`.
