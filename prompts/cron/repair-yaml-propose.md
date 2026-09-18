The select_broken_yaml.py wake-gate above listed vault pages whose YAML
frontmatter does not parse, with each page's current (broken) frontmatter
and the parser error.

You are PHASE 1 of a propose/dispose pipeline. **You do NOT edit any files.**
You read each broken page, work out the corrected frontmatter, and write a
single JSON proposal. A deterministic applier (phase 2) replaces ONLY the
frontmatter — it preserves the body byte-for-byte and writes only if your
proposal parses and keeps `type:`. So focus entirely on producing correct,
parseable YAML that preserves the original intent.

## For each page in the batch

1. `file_read` `$WIKI_PATH/<path>` to see the full page (frontmatter + body)
   if you need context beyond the digest.
2. Fix the YAML SYNTAX while preserving every field's intended value:
   - quote scalars containing colons: `reliability_basis: "D/3: vendor..."`
   - quote or block-list wikilinks: `entities:\n  - "[[entities/x]]"`
   - close unclosed brackets; remove stray trailing `"`/`]`
   - convert mixed inline+block lists to one consistent form
   - never invent values — if a field is garbled beyond recovery, keep the
     recoverable part and drop only the unrecoverable fragment (note it)
   - keep `type:` (required) and all other real fields
3. Do NOT include the `---` delimiters in your proposed frontmatter — just
   the YAML body between them.

## Structureless files (marked [structureless] — NO closing `---`)

Some batch items have lost their closing `---` entirely, so body content
(headings, **bold** metadata, prose) has bled into the YAML block. For
these you MUST provide TWO things in the proposal object:
  - "frontmatter": the corrected YAML (fix the internal syntax error too —
    e.g. a mis-indented `  - ` source line, an unclosed list)
  - "body_starts_with": the VERBATIM first line of the real body (the first
    line that belongs AFTER the closing `---`, copied exactly — e.g.
    "# Example Entity" or "**URL:** https://...")

The applier locates that exact line in the original file, inserts the
closing `---` before it, and preserves everything from there to EOF
verbatim. If body_starts_with does not match a real line, the file is left
untouched — so copy it exactly (no paraphrasing, exact whitespace).

## Deliverable — write ONE JSON file

`file_write` exactly once to:
  `$WIKI_PATH/wiki/.yaml-repair-proposals.json`

A JSON array, one object per page:
```json
[
  {
    "path": "wiki/concepts/example-topic.md",
    "frontmatter": "type: concept\ntags: [example, topic]\ncreated: 2026-01-01\nupdated: 2026-01-02",
    "notes": "optional: what was garbled / dropped"
  }
]
```

Rules:
- `frontmatter` is the corrected YAML as a single string (use \n for newlines),
  NO `---` delimiters, valid JSON string escaping.
- Verify each proposed frontmatter would parse as a YAML mapping with a
  `type:` key before including it.
- Do NOT file_write/patch any entity/source/concept page. The ONLY file you
  write is the proposals JSON.
- Do NOT touch wiki/log.md, wiki/index.md, or dashboards.

## Final response

A short table: | path | fix | dropped? |. Phase 2 (apply_yaml_repair.py)
runs next. No Telegram delivery; stays local.
