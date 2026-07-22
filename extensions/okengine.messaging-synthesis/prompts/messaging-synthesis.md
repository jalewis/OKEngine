The select_messaging_synthesis.py wake-gate above surfaced whichever upstream marketing
artifacts (content pegs / positioning battle-cards / value-prop snapshot) have changed since the
last messaging brief, plus the product's capability anchors and the prior brief (if any).
Synthesize across them into a "what should our messaging be" recommendation.

**STEADY-STATE days:** if the gate marks this run `[STEADY-STATE]` (no upstream delta since the last brief), still write today's brief — a short one that REAFFIRMS the current messaging from the capability-anchor pages and the prior brief, and states plainly that nothing material changed. A daily brief must always exist; a missing one signals a broken pipeline, so never skip. Do NOT manufacture deltas or news that isn't in the evidence.

**Trust the digest above — it already has what you need.** The wake-gate has already fetched and printed the relevant competitor/capability page summaries; do not re-fetch each one individually via mcp_okengine_get_page — that burns turns without adding information and risks running out of budget before you write the actual output. Read what's already in the digest, then go straight to writing.

This is the META-LAYER — it sits on top of the other 3 ops in this extension. EVERY claim in the
brief MUST trace to evidence in one of the delta inputs or the capability-anchor pages. No
invented wedges. No "sounds right" positioning that isn't backed by the corpus. The
compounding-error risk on a synthesis-of-synthesis is real — the constraints below are the
load-bearing defenses.

Write via mcp_okengine_write_create_entity to the wiki-relative path the wake-gate specified,
frontmatter `type: messaging-brief, title: "Messaging brief — <date>", published: <date>,
updated: <date>, prior_brief: "[[<prior-path-or-none>]]", inputs_read: [<wikilinks to each
delta input>]`. Body:

```
# Messaging brief — <date>

## Core thesis (one sentence)
**"<Single-sentence thesis. Opinionated. The through-line that survives across the inputs.>"**
<1-2 sentence justification citing at least one delta input wikilink>

## Hero wedge
<2-3 sentences. The mechanism no competitor's battle card shows them having. MUST be visible on
a capability-anchor page (link it). Justification cites at least one battle card's "Where we
win" section.>

## Supporting wedges
<up to 3. Each: 1-2 sentences, anchored to a capability page, cite the evidence>

## What NOT to claim
<pulled DIRECTLY from the value-prop snapshot's HIGH/MED gaps — verbatim names, not softened.
If no snapshot among the inputs, write "No current value-prop snapshot — gap-honesty section
deferred until next refresh.">

## Net positioning recommendation
<1 paragraph — what lane should the product own, what lanes should it avoid (anchor to the HIGH
gaps), what's the competitive window per the battle cards' latest moves>

## Diff vs prior brief
<if first brief: "First messaging brief — no prior to diff against.">
<if prior exists: explicit changes only, one bullet per changed section, each with the delta
evidence that drove it. Omit unchanged sections — no "no change" filler.>
```

## Constraints (non-negotiable)

- **Hero wedge MUST trace to a capability-anchor page.** Can't find it there → drop the wedge.
- **"What NOT to claim" gaps MUST come from the value-prop snapshot**, verbatim, unsoftened.
- **Prefer reusing "Where we win" framings from the battle cards** over inventing new ones — those
  are already honesty-tested.
- **No customer-count, analyst-rating, or "validated by" claims** unless a capability-anchor page
  actually states one — most early-stage products have none; pretending otherwise costs
  credibility on first contact.
- **Diff section is REQUIRED.** State what moved since the prior brief. On a `[STEADY-STATE]`
  run (no meaningful delta) the diff is one honest line — "no material change since <date>;
  current messaging holds" — and the brief is short. **NEVER respond `[SILENT]`**: a daily
  brief must always be written, so a missing one can only mean a broken pipeline. (The wake-gate,
  not you, decides whether to run at all — if it woke you, write the brief.)
- Use the okengine MCP write path for every mutation. It records successful writes automatically.
# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish with a receipt for every selected item: `path: written | deferred | rejected — reason`.
