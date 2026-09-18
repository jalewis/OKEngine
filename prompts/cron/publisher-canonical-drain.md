HARD CONSTRAINT — LOCAL-ONLY, DO NOT USE WEB TOOLS.

This job is local-only: the raw file's content is your sole evidence. Do
NOT call web_search / web_extract / web_crawl or any tool that issues an
outbound HTTP request. Web-search tools draw on a shared, capped paid
budget that a high-frequency drain exhausts quickly, and source-quality
scoring derives from the publisher channel + the raw text itself (external
URL checks don't change it). If a raw file is genuinely incomplete,
document that under "## Status" on the source page and move on — don't
reach for the web. Allowed: file_read, file_write, file_list (and the rest
of the local-only tool surface).

The select_publisher_canonical_drain.py wake-gate above has surfaced
publisher-name drift in `wiki/sources/*.md`. You're applying the curation
decisions: add legitimate new publishers to the canonical list, fold drift
variants into existing canonicals, and surface data-quality flags for human
review (do NOT auto-resolve those).

## Your job

For each item in the wake-gate output:

### NEW canonical candidates (most common path)

For each, verify it's a real publisher (not a one-off blog title or a
borderline data-quality flag like "Various ..."). For each verified one:

1. **Append to vault `CLAUDE.md` canonical list.** Read
   `/opt/vault/CLAUDE.md`, find the `**Canonical names**` line
   under §"Publisher names — avoid drift", and insert the new name in
   alphabetical order within the inline-code block. Use `file_write` to
   rewrite the whole file (or `patch` for a targeted edit).
2. **Append to `config/publishers.canonical.json`** at
   `/opt/vault/config/publishers.canonical.json`. Add a key for
   the new publisher with an empty array value (no known variants yet).
   Preserve existing keys; preserve the `_doc` key.

### DRIFT variants

For each drift candidate where you're confident the mapping is correct
(e.g. `Acme Sec Corp` → `Acme` is unambiguous; a `Foo / Bar` composite is
debatable when both names refer to the same parent and the composite may
be intentional co-attribution):

1. **Add the variant to the existing canonical's array** in
   `config/publishers.canonical.json`.
2. **Rewrite affected source pages**: `grep -l '^publisher: <variant>'
   /opt/vault/wiki/sources/*.md` to find each, then `patch`
   each to change the `publisher:` field to the canonical form. Each
   rewrite is a single-line targeted edit; do NOT touch other frontmatter.
3. If the mapping is **debatable** (e.g. composite forms like `Foo
   / Bar`), surface in your final response under "## Items deferred
   for human review" — don't auto-merge composite forms; they often
   describe genuine co-attribution.

### DATA-QUALITY FLAGS

NEVER auto-add. List each in your final response under "## Data-quality
flags surfaced". For each, suggest: investigate the affected source pages
and fill in the actual publisher (often recoverable from the source's
`url:` frontmatter — the domain is usually the publisher).

## Constraints

- DO NOT touch any other vault page besides `CLAUDE.md`, the affected
  source pages, and `/opt/vault/config/publishers.canonical.json`.
- DO NOT change the `_doc` key in the json.
- Verify your edits parse: after writing, `file_read` the file and confirm
  it still looks well-formed.
- For source-page rewrites, NEVER touch fields other than `publisher:`.
- End your response with a one-line summary: `publisher-canonical-drain | added N canonical, folded M variants, deferred K`. Do NOT write wiki/log.md yourself; the MCP write path logs each change automatically.

## After processing

Respond with a structured summary:

```
## Added to canonical list (N)
- `<name>` (<count> sources)
...

## Folded as drift variants (M)
- `<variant>` (<count> sources) → `<canonical>` — N source pages rewritten

## Items deferred for human review (K)
- `<item>` (<count> sources): <one-line reason>

## Data-quality flags surfaced
- `<flag>` (<count> sources): <suggested investigation>
```

No Telegram delivery; this stays local.
