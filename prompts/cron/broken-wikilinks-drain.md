HARD CONSTRAINT — LOCAL-ONLY, DO NOT USE WEB TOOLS.

This job is local-only: the raw file's content is your sole evidence. Do
NOT call web_search / web_extract / web_crawl or any tool that issues an
outbound HTTP request. Web-search tools draw on a shared, capped paid
budget that a high-frequency drain exhausts quickly, and source-quality
scoring derives from the publisher channel + the raw text itself (external
URL checks don't change it). If a raw file is genuinely incomplete,
document that under "## Status" on the source page and move on — don't
reach for the web. Reading/listing is local-only; every wiki mutation uses
the governed MCP write operations specified at the end of this prompt.

The select_broken_wikilinks_drain.py wake-gate above has surfaced
broken `[[wikilinks]]` in `wiki/**/*.md` whose targets don't resolve to
existing files, sorted by inbound source count. You're applying the
curation decisions: create entity stubs where there's clearly an entity
behind the link, rewrite citing pages where the link is a typo / variant
/ bare-publisher form, and defer genuinely speculative targets for human
review. Operational/report pages (lint-*, log*, triage-*, operational/)
were already filtered from the inbound counts — every shown source is a
real body reference.

## Per-target classification (5 actions)

For EACH target in the wake-gate batch, in order:

### 1. MISSING ENTITY — write the page, not a bare stub

The target is `[[entities/<slug>]]` (or a bare basename that's clearly an
entity name) and no existing page resolves. Read 2-3 of the inbound source
pages — the surrounding context tells you the type AND gives you the
material to write the page.

Per the vault creation standard (CLAUDE.md → "Entity & concept pages —
minimum on creation"): a created entity page should carry a 1-3 sentence
lead saying what it is, plus **>=1 `##` section of real analysis** when
the inbound sources support it. Do NOT mint a name-only stub if the sources
let you write a paragraph.

Action: Create `wiki/entities/<slug>.md` with
`mcp__okengine_write__create_entity`. Frontmatter:

```yaml
---
type: <one of your pack's entity types from schema.yaml>
tags: [<2-4 short tags from context>]
created: <YYYY-MM-DD today>
updated: <YYYY-MM-DD today>
sources:
  - "[[sources/<inbound-1>]]"
  - "[[sources/<inbound-2>]]"
---

# <Display Name>

<1-3 sentence lead: what this entity is, from the inbound context.>

## <Section grounded in the sources — e.g. Activity / Positioning / Why it matters>

<2-5 sentences of real analysis pulled from the citing sources.>
```

**Fallback — bare stub ONLY when the inbound sources genuinely lack usable
material** about the target (you are creating the page solely so the link
resolves). Then add `link_stub: true` to frontmatter and a body line
`Stub — backfilled by broken-wikilinks-drain from N inbound references;
deepen via page-quality-enrich.` so the auditor classes it correctly and
the enrich loop picks it up. Do NOT use this fallback when a paragraph is
writable.

The `sources:` list should include the inbound source pages from the
wake-gate output (paths shown without the .md suffix). YAML must use
multi-line list form (NOT bracketed JSON-style); unquoted wikilinks in
brackets break `yaml.safe_load`.

### 2. CASE-MISMATCH / BARE-PUBLISHER (rewrite-link)

Target like `[[Acme Corp]]` where `entities/acme-corp.md` exists, or
`[[entities/Acme-Corp]]` (case drift). Action: rewrite each citing source
page to use the canonical path `[[entities/acme-corp]]` with
`mcp__okengine_write__patch_entity` — touch ONLY the broken wikilink,
never other content.

Do NOT create a stub.

### 3. TYPO / VARIANT (rewrite-link)

Target like `[[entities/acme-globex]]` where the canonical entity is
`[[entities/acme]]`, or `[[entities/example-vendor]]` where the canonical
is `[[entities/example-vendor-ai]]`. Verify the canonical exists by
listing/reading `wiki/entities/`. Action: rewrite each citing source page
to use the correct path. If the variant is plausibly a different concept
(e.g. composite co-attribution like `acme-globex` could legitimately
mean the post-merger entity vs `acme` standalone), DEFER under §5
rather than guessing.

### 4. ARCHIVED / DELETED TARGET (rewrite-link)

Target like `[[sources/<dated-slug>]]` where the source page no longer
exists (rotated to `_archived/` or removed). Action: check
`wiki/sources/_archived/` for the file. If found, rewrite to the
`_archived/` path. If not found, replace the wikilink with plain text
(remove the `[[ ]]` brackets) — the reference is still meaningful as a
title even without a target.

### 5. DEFER FOR HUMAN REVIEW

Target doesn't fit any of the above:
- Genuinely speculative (no clear stub candidate, no obvious typo)
- Composite forms where the resolution is ambiguous
- Targets where you can't determine the entity type from context

Surface in your final response under "## Items deferred for human review".
Do NOT create a placeholder stub for these.

## Constraints

- DO NOT touch any vault page outside the affected entity stub files and
  the citing source pages identified by the wake-gate.
- DO NOT modify operational pages (lint-*, log*, triage-*, dashboards/,
  operational/) even if they cite the broken target.
- For source-page rewrites, NEVER touch fields other than the exact
  wikilink string being fixed.
- Verify your edits parse: after writing each entity stub, `file_read`
  it back and confirm the YAML is valid (frontmatter delimiters present,
  `sources:` is a multi-line list).
- End your response with a one-line summary: `broken-wikilinks-drain | created N stubs, rewrote M citations, deferred K`. Do NOT write wiki/log.md yourself; the MCP write path logs each change automatically.
- Process targets IN ORDER. If you run out of budget partway through,
  stop cleanly and surface the remaining batch items in the deferred
  section — do NOT skip ahead.

## After processing

Respond with a structured summary:

```
## Created entity stubs (N)
- `entities/<slug>` (type=<X>, <count> inbound sources)
...

## Rewrote citations (M targets, P source pages touched)
- `<broken-target>` → `<canonical-target>` — Q citations rewritten across R source pages
...

## Replaced with plain text (K)
- `<broken-target>` (<count> sources) — target not recoverable, brackets stripped
...

## Items deferred for human review (J)
- `<target>` (<count> sources): <one-line reason>
```

No Telegram delivery; this stays local.

─────────────────────────────────────────────────────────────────
WRITE VIA THE MCP WRITE PATH (G1/G1.1) — NOT file_write/patch
─────────────────────────────────────────────────────────────────
Apply curation with the enforced MCP tools (file_read still reads):
- MISSING ENTITY → `mcp__okengine_write__create_entity`, path "entities/<slug>",
  frontmatter type: <one of your pack's types> + the type's required fields
  (incl sources), body: the lead + >=1 analysis section. Required fields are
  enforced; create refuses dupes.
- TYPO / VARIANT / bare-publisher link in a CITING page → `mcp__okengine_write__patch_entity`,
  path: the citing page, old_string: "[[<broken target>]]", new_string: "[[<correct>]]".
  old_string must be UNIQUE in that page (include surrounding text if the link
  recurs). It preserves the rest of the page and rejects dropping any frontmatter field.
Do NOT use file_write/patch for wiki pages.
Dispose every selected target exactly once and finish with only the selector-provided fenced
`okengine-receipt`, preserving its runner-owned identity fields and item keys.
