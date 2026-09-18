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

The select_orphans_drain.py wake-gate above has surfaced
entity/concept/prediction pages with **zero inbound `[[wikilinks]]`** from
any other page with valid frontmatter. For each orphan it surfaces the
top-3 candidate referencers ranked by shared sources / shared tags. Your
job: for each batch orphan, decide whether each candidate is genuinely
topically related, then EITHER patch the candidate's `related:` array to
add the orphan (rescuing it from the queue), OR if no candidate qualifies,
append a `## Triage note` to the orphan with the strongest 2 candidates
and the reason none worked.

The drain reads from the same lint_watcher counting model, so adding the
orphan to a candidate's `related:` array (which is in the candidate's
frontmatter) IS the cure — lint-watcher will count the new wikilink as a
real inbound reference and remove the orphan from the queue.

## Per-orphan workflow

For EACH orphan in the wake-gate batch, in order:

### Step 1 — Read the orphan

`file_read` the orphan page. Confirm what it claims to be, what sources
it cites, what concepts/entities it relates to. Pay attention to:
- The body's natural prose: what other entity/concept names does it
  mention? (Even if not wikilinked.)
- For predictions: the `subject:` field names the entity/concept the
  prediction is about — that entity is almost always the right
  referencer.
- For entities: what category / cluster does this fit into? The corresponding concept page is usually the
  right referencer.

### Step 2 — Evaluate each candidate

For each of the top-3 candidates surfaced by the wake-gate, in order:

a. `file_read` the candidate page (only the parts you need: frontmatter
   + first 30-60 lines of body usually enough).

b. Decide: does the candidate genuinely relate to the orphan?
   - **YES** if the candidate's body discusses the same entity/concept as the orphan, OR if there's a natural place a reader of
     the candidate would want to discover the orphan.
   - **NO** if the only signal is shared tags coincidentally (e.g.,
     both tagged with one broad topic tag); that's not specific enough.

c. If YES: patch the candidate's frontmatter `related:` array with
   `mcp__okengine_write__update_entity`. If the candidate
   has NO `related:` field yet, add one as a multi-line YAML list:

   ```yaml
   related:
     - "[[<orphan-rel-path-without-md>]]"
   ```

   If `related:` already exists, prepend or append the new wikilink
   while preserving existing entries. NEVER use bracketed JSON form
   (`related: ["[[foo]]"]`) — the unquoted-wikilink-inside-brackets
   pattern breaks `yaml.safe_load`. Always use multi-line list form
   with quoted wikilinks.

d. If NO: try the next candidate. If all 3 fail, the orphan goes to
   triage (Step 3).

### Step 3 — Triage notes (when no candidate qualifies)

If no candidate genuinely relates, append a `## Triage note` section to
the orphan body (NOT frontmatter):

```markdown

## Triage note

Flagged <YYYY-MM-DD> by `orphans-drain` — no inbound references after
shared-source / shared-tag scoring against the vault. Top scoring
candidates considered:
- `<candidate-1>` — score X.XX (<reason none of its content discussed
  the orphan>)
- `<candidate-2>` — score Y.YY (<reason>)

Possible follow-ups: archive (stale / superseded), backfill via
entity-backfill (add fresh source citations that would create natural
referencer overlap), or human review.
```

Append the section; do NOT modify frontmatter or other body content.
The triage note is a signal for the next human-driven cleanup pass.

## Constraints

- Touch ONLY the candidate pages whose `related:` arrays you patch, and
  the orphan pages you append triage notes to.
- For `related:` array edits: NEVER touch any other frontmatter field
  on the candidate (sources, tags, type, created, etc.). Targeted edit
  only.
- For triage-note appends: NEVER modify the orphan's frontmatter or
  existing body — only append the new section at the end.
- Verify your YAML edits parse: after each `related:` write, `file_read`
  the candidate's frontmatter and confirm it round-trips cleanly.
- End your response with a one-line summary: `orphans-drain | rescued N orphans (M related: patches), triaged K`. Do NOT write wiki/log.md yourself; the MCP write path logs each change automatically.

## After processing

Respond with a structured summary:

```
## Rescued via `related:` patch (N)
- `<orphan-rel>` ← `<candidate-rel>` — <one-line why this candidate fit>
...

## Triaged (K)
- `<orphan-rel>` — top candidate `<best>` (score X.XX) didn't qualify because <reason>
...
```

No Telegram delivery; this stays local.

─────────────────────────────────────────────────────────────────
WRITE VIA THE MCP WRITE PATH (G1/G1.1) — NOT file_write/patch
─────────────────────────────────────────────────────────────────
- RESCUE (preferred): add the orphan to a qualifying candidate's `related:` via
  `mcp__okengine_write__update_entity`, path: the candidate,
  frontmatter_yaml: "related: [<COMPLETE list including the new \"[[<orphan>]]\">]".
  ⚠ `related:` is a LIST — read the candidate first and send the FULL updated list
  (a list key REPLACES, it does not append).
- NO candidate qualifies → `mcp__okengine_write__append_to_section`, path: the
  orphan, heading: "Triage note", text: the 2 strongest candidates + why none worked.
file_read to read; do NOT use file_write/patch for wiki pages.
