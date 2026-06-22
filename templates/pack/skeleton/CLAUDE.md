# {{PACK}} — {{TITLE}}: persona & curation rules

This is the guidance the engine's cron agents read at runtime (`$WIKI_PATH/CLAUDE.md`).
It is the *domain voice + ingest/curation workflow* for **{{PACK}}** — distinct from the
engine repo's dev/ops docs. The machine-readable contract is `schema.yaml` (types,
partitioning, hot_set, permissions, review, tier); this file is the human judgment that
fills it.

## Mission

> TODO: one paragraph — what compounding knowledge vault is this maintaining, ingesting
> from what sources, to answer what questions for whom? Keep it concrete.

Maintain a compounding, agent-curated knowledge vault for {{TITLE}}: ingest open
{{DOMAIN}} sources and compile them into a durable, cross-linked graph of <your core
entity types> — so a reader can answer the domain's key questions from the vault
instead of re-reading the feed.

## Positioning

- **Filter, not feed.** Most feed items are noise or restate known facts. Compress
  signal into structured pages; do not mirror the feed. A source with nothing new gets
  a thin source page (for the dedupe/provenance trail) and no new entities.
- **Compounding KB, not RAG.** Compile once into entities and *maintain* them over time
  (new sightings append to an entity, not a new page).
- **Audience:** > TODO: name the expert reader. Assume expertise — skip 101
  explanations; capture the specific, actionable detail.

## Knowledge-graph memory (query before you write)

The vault IS your memory — query it before you create, so you compound instead of
duplicating. The engine serves read-only graph tools over the okengine MCP:

- **`search`** — find existing pages by topic/term. Run it BEFORE creating any page.
- **`find_references(target)`** — pages matching `target` + their resolved
  references/backlinks. Use to catch a subject that already exists **under an alias**,
  and to see what already links to it.
- **`retrieve_context(path)`** — a page with its graph neighbourhood (one hop). Use
  before editing a page to see what it connects to and wire in missing cross-links.
- **`graph_stats()`** — orphans (pages nothing links to) + most-referenced hubs.

Rule of thumb: **SEARCH before CREATE; RETRIEVE before EDIT.** Update the existing page
(matched by name OR alias) rather than minting a near-duplicate. A page nothing links to
— and that links to nothing — is barely worth more than the feed it came from.

## Ingest workflow (sources → entities)

Process each raw item in the digest IN ORDER. Read `schema.yaml` for the exact required
fields per type.

1. **Source page first (dedupe + provenance).** Create `wiki/sources/<YYYY>/<MM>/<slug>.md`
   (`type: source`). Set **`raw:`** to the exact raw path — this is the dedupe key. Set
   `publisher`, `published`, `url`, and your `source_kind`.
2. **Score every source.**
   > TODO: define your source-quality rubric. okpack-sec uses Admiralty
   > `reliability` (A–F, the channel) + `credibility` (1–6, the claim) + `tlp` +
   > `bias_flags`. Adapt to your domain's notion of trust, or simplify to a single
   > `confidence`. Add the scoring fields to `source.required` in schema.yaml if you
   > want the gate to enforce them.
3. **Extract entities** (under `wiki/entities/`, bucketed by type). Create an entity
   when it is **worth tracking over time**; skip one-off mentions.
   > TODO: list your domain entity types + the identity field each needs (mirror
   > schema.yaml `types`).
4. **Cross-link.** Link related entities with `[[wikilinks]]` — to pages that exist or
   you create in this batch. The graph is the value. **One deliberate exception — concept
   links:** when a page exhibits a recurring cross-cutting theme, tag it
   `[[concepts/<slug>]]` *even if that concept page does not exist yet*. Those dangling
   concept links are the signal the `concept-backfill` cron uses to synthesize the concept
   page once a slug accrues enough inbound references — so link the theme on every page that
   exhibits it. Do **not** create the concept page yourself here.
5. **Update, don't duplicate.** A new sighting of an existing entity appends to its
   `## Recent activity` and bumps `updated:` + adds the new source to `sources:` —
   never a second page for the same entity.
6. **Findings are HUMAN-AUTHORED.** You may *surface* candidate findings in your run
   summary, but you must NOT create or edit `wiki/findings/` pages (`schema.yaml`
   `permissions.findings` is human-only — the write path refuses it).
   > TODO: delete this rule if your domain has no human-only gate.
7. **File predictions** for explicit, falsifiable, dated forward claims a source makes.
   No falsifiable claim → file none. Never invent one.

## Predictions

`type: prediction` requires `status` (open|confirmed|refuted|partial|expired-ungraded),
`confidence` (0.0–1.0), `subject` (`[[entity/...]]`), `resolves_by` (date). Every
prediction MUST have a `## What would refute this` section — refuse to file without it.

## Confidence trust model (G3 — flag, not gate)

Assert a **numeric** `confidence` (0.0–1.0) or `low`/`medium`/`high` freely. The
categorical verdicts in `schema.yaml` `review.confidence_review_values` are
review-flagged: asserting one lands the write but stamps `needs_review: true`.

## Write discipline

- Write via the **enforced MCP write path** (`create_entity` / `update_entity` /
  `patch_entity` / `append_to_section`), not raw `file_write`. Each validates against
  `schema.yaml` and logs to `wiki/log.md`.
- **Never delete a knowledge page** — `tombstone_entity` (retains the file as
  `status: tombstoned`). Dedup/merge = tombstone the loser with `superseded_by`.
- Keep required fields present (the gate rejects non-conformant writes).
- Respect any TLP / sensitivity convention your domain uses.

## Domain pointers

- **Taxonomy:** the `types` in `schema.yaml`.
- **Standards:** > TODO: list the canonical external IDs/standards your domain anchors
  on (e.g. an ID scheme, a grading system, a classification).
- **Concepts** capture cross-cutting patterns that group many entities. You don't author
  them during ingest — you *seed* them: tag pages with `[[concepts/<slug>]]` (step 4) and
  `concept-backfill` synthesizes the page once enough pages link the same slug. A concepts
  namespace that lags far behind entities usually means pages aren't being tagged.
