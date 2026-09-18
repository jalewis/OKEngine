Structural-gap discovery (lacuna prompting). The `select_lacuna_field.py` digest above lists
dense, not-recently-analyzed concept clusters — each a **field** mapped from the REAL graph.
FIRST response MUST be a tool call. Pick the ONE field you can map most honestly and run the
6-step procedure over its actual subgraph (read the concept page and its referencing entities/
sources via the okengine read tools — map the field from the DATA, never from memory).

The six steps (each becomes a section of the page). Write each section header to pair the method
term with a plain restatement, so a practitioner who's never heard "lacuna" can follow it — e.g.
`## The hidden axis — what the field is really optimizing for`, `## The lacuna — the missing
<plain noun phrase>`, `## The force keeping it empty — <plain reason>`:

1. **Map the field** — read the cluster's pages and lay out the existing approaches densely
   enough to see the shape. Cite real pages with `[[wikilinks]]`.
2. **Find the hidden axis** — what does this whole field secretly optimize for?
3. **Locate the lacuna** — the cell the geometry implies but nothing in the cluster occupies.
4. **Name the force keeping it empty** — the incentive / accounting model / measurement system /
   tooling limit that literally can't represent the missing thing. THIS IS LOAD-BEARING: if you
   cannot name a specific force grounded in the cited data, **DEFER the entire field** (no page).
   A gap with no named force is a boring coverage gap, not a lacuna.
5. **Sort** — empty because *undiscovered*, or empty because *everything that tried there fails*?
6. **Propose the fill + confidence** — the fill must concretely engage the force you named in
   step 4 (HOW it overcomes that specific constraint — the signal/noise problem, the incentive,
   the missing measurement), not restate the gap or stay abstract. Derive confidence from the
   surround density the digest reported (a thick fabric ⇒ strong inference; a thin patch ⇒
   flagged extrapolation), NOT from a gut feel.

Guardrails (the engine's containment rule): a lacuna is an INFERENCE, not verified knowledge.
**Do NOT create or edit any `entity`/`concept`/`source` page.** Beware the contrarian-inversion
trap — a "just invert the field's premise" answer is rhetoric, not a structural gap; ground the
emptiness in what the cited pages actually do and don't cover. Keep the analysis on ONE coherent
thesis: if a sub-point sits on a different vector (e.g. an initial-access tactic when the gap is
post-access), either tie it in explicitly or leave it out — don't pad with adjacent-but-off-thesis material.

If the field yields a real lacuna, write ONE page at `lacuna/<slug>` (`type: lacuna`) via
`mcp__okengine_write_okengine_lacuna__create_entity`, frontmatter:
- `title` (the gap as a noun phrase), `field_mapped` (the cluster, e.g. `[[concepts/<slug>]]`),
  `hidden_axis`, `force` (the named force), `fill` (the proposal), `discovered_vs_failed`
  (`undiscovered` | `everything-fails`), `confidence` (`low`|`medium`|`high`),
  `surround_density` (copy the digest's measured string), `needs_review: true`,
  `see_also` (the `[[concepts/...]]`/`[[entities/...]]` pages you mapped), and `sources` you cited.
- Open the body with the one-line caveat *"A structural inference grounded in the cited subgraph
  — not a verified claim."*, then a one-sentence plain-language **TL;DR** (the gap and why it
  matters, jargon-free, so a reader gets the point before the method framing), then the six
  glossed sections in order.

Soft predictions edge — ONLY if a `predictions/` namespace exists in this vault (i.e.
okengine.predictions is enabled) AND the fill is genuinely testable ("the cell fills when force Y
weakens via trigger Z by date D"). Reserve enough tool turns for the write sequence:
1. Create the Lacuna page **without** `prediction_candidate` or `fill_trigger`. Its inference
   stands alone even if a later write is rejected or the iteration budget runs out.
2. After the Lacuna create is accepted, file a falsifiable, dated prediction at
   `predictions/<slug>` via `mcp__okengine_write_okengine_lacuna__create_entity` (include a
   `## What would refute this` section and `subject: [[lacuna/<slug>]]`). Pass valid YAML mapping
   frontmatter; use a block scalar (`|`) for prose values with colons, quotes, or line breaks.
   If the write rejects, correct and retry while budget remains.
3. **Only after the prediction create is accepted**, update the existing Lacuna page via
   `mcp__okengine_write_okengine_lacuna__update_entity` to add `fill_trigger` and
   `prediction_candidate: predictions/<slug>`. The write contract rejects a candidate link
   whose target does not exist. If the prediction cannot be written, leave the Lacuna page
   without that link and report the rejected prediction in the final receipt.

If predictions is not enabled or the fill isn't testable, skip this — the Lacuna page stands alone.

When filing, use exactly these fields — no substitutes, no extras: `made_on` (today, ISO date),
`resolves_by` (ISO date), `horizon`, `confidence` (numeric 0.0-1.0), `status: open`, `subject`.
Compute `horizon` from `(resolves_by - made_on).days` — do not pick by feel:
short if ≤90, medium if 91-365, long if 366-1825, strategic if >1825. A structural inference like
a lacuna fill is rarely a short-horizon claim; double-check the arithmetic before defaulting to
`medium` out of habit.

LOCAL-ONLY (no web tools). End with a one-line summary: the field analyzed and whether a lacuna
was written or the field deferred. If a Lacuna write is accepted, give separate artifact receipts
for `lacuna/<slug>` and `predictions/<slug>`: accepted, rejected with the write error, or not
attempted with the reason. Do not call an accepted Lacuna a successful prediction. If the
prediction is accepted but the Lacuna link update rejects, report that update separately.
# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish with a receipt for every selected item: `path: written | deferred | rejected — reason`.
