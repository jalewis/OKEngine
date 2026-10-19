**Your first response MUST contain a tool call** (file_read the first gap's subject page, or $WIKI_PATH/CLAUDE.md for the vault's rules) — no prose-only first turns.

The select_gap_fixes.py wake-gate above surfaced open completeness gaps whose rules the pack
marked agent-fixable. Your job: FIX each subject page so the declared expectation is met —
the next audit auto-resolves the gap. You are closing gaps, not writing essays.

## Per-gap workflow

1. **Read the subject page** (file_read). Understand what the unmet expectation needs.
2. **Gather grounding from the vault only.** If the fix needs references (a `basis`, a source
   citation, a vendor link), FIND the real pages: search the corpus for the subject's own
   wikilinks, the entities it names, sources that mention it. **NEVER fabricate a reference:
   a cited page must exist and genuinely support the fix.** If the vault holds nothing that
   honestly satisfies the expectation, SKIP the gap and say so — an unfixable gap is the
   operator's signal, and a fabricated fix is corpus poisoning.
3. **Apply the fix via the MCP write path** (mcp__OKENGINE_WRITER__update_entity), NOT
   file_write. Touch ONLY what the expectation asks: add the missing field, the missing
   wikilink, the missing companion reference. Do not rewrite bodies, do not "improve" pages.
4. **DRAFT MODE** (the wake-gate marks these): judgment-bearing fixes (e.g. a refutation
   criterion) are proposals — set `needs_review: true` on the subject page in the same
   write, and keep the drafted content clearly attributable (a dated line).
5. **Receipt**: dispose every selected gap in the `okengine-receipt` (see the end of this
   prompt). The writer records successful mutations in the engine log automatically; never
   edit the log yourself.

## Constraints

- Fix ONLY the surfaced gaps. Do not tour the queue or other pages.
- Never touch a gap page itself (status changes are the audit lane's and the operator's job).
- Skipping honestly beats fixing badly — the gap stays visible either way.

# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish by returning ONLY the fenced `okengine-receipt` the selector printed above, with exactly one item per selected key (do not add, drop or rename keys). Dispositions: `accepted` or `updated` for an item you actually wrote (list the page in `writes`; the runner supplies the hashes); otherwise `deferred-for-review`, `insufficient-evidence`, `rejected-out-of-scope`, `duplicate` or `failed`, each with a concrete `reason` and no `writes`. A batch in which every item is honestly deferred or rejected is a successful run — never write just to have written. Each key is a GAP page; an accepted fix writes the gap's SUBJECT page (the printed `writes` path), never the gap page. A fixed or drafted gap is `updated`; a gap the vault cannot honestly satisfy is `insufficient-evidence` with the reason.
