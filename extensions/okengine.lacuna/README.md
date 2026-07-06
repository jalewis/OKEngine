# okengine.lacuna

First-party extension: **structural-gap discovery** (lacuna prompting). The most
*differentiated* capability in the parity map — everything else is table-stakes KB machinery;
this generates new **coordinates** (whitespace, research directions, positioning), not verdicts.

## What a lacuna is

A **lacuna** is a structural gap in the vault's knowledge field — *a cell the geometry of the
existing knowledge implies should exist, but which nothing occupies* — together with a **named
force** that explains why it stays empty. It is **not** a coverage gap ("we haven't written
about X yet" — that's concept-backfill / glossary territory). The difference is the
load-bearing **force**: an incentive, accounting model, measurement system, or tooling limit
that *literally can't represent* the missing thing. No named force ⇒ a boring undiscovered
topic, not a lacuna.

Asking a model for "a novel idea" regresses to the mean (the novel point and the most-probable
point pull in opposite directions). The 6-step procedure forces output to a structural **edge**
instead:

1. **Map the field** — densely enough to see its shape.
2. **Find the hidden axis** — what the field secretly optimizes for.
3. **Locate the lacuna** — the implied-but-empty cell.
4. **Name the force keeping it empty** — *load-bearing*; no force ⇒ defer.
5. **Sort** — empty because *undiscovered*, or because *everything there fails*?
6. **Propose the fill + confidence** — from measured surround density, not a vibe.

## Why the engine version beats the raw prompt

The raw prompt maps the field from a model's **averaged recall** and asks it to introspect on
density (step 6) — which it can't do reliably. An OKEngine vault has the field **already
mapped**, so this extension:

| Upgrade | How |
|---------|-----|
| Map from the **real graph**, not recall | `select_lacuna_field.py` maps each field from a concept cluster — the pages that link `[[concepts/<slug>]]` |
| Density becomes **measurable** | the cluster's distinct-referencing-page count (with a per-namespace breakdown) is the `surround_density` the agent records — a metric, not a guess |
| Defeats the **contrarian-inversion trap** | grounding the gap in actual structural emptiness in *cited* pages, not rhetorical "opposite pole" inversion (its own learnable average) |

## The three engine constraints (containment)

1. **Containment.** A lacuna is an *inference*, not verified knowledge. It **never** writes
   canonical `entity`/`concept`/`source` pages — it **owns** the `lacuna` type + namespace
   (bring-your-own-schema, #133) and writes only there, always `needs_review: true`, low-trust.
2. **Verification via predictions (soft edge).** Lacuna finds gaps but can't tell you whether
   the floor holds. *When `okengine.predictions` is also enabled*, a testable fill ("fills when
   force Y weakens via trigger Z by date D") is emitted as a **prediction candidate** into
   `predictions/**` — exactly how `candidate-watch` files candidates — and predictions grades it
   for free. **No hard `requires`:** if predictions is absent, lacuna pages stand alone.
3. **Generic — no domain coupling.** Ships only the method; all market vocabulary stays in pack
   config. Runs unchanged on any pack.

## How it works

Weekly, the wake-gate `select_lacuna_field.py` ranks concept clusters by density, drops any
analyzed within `reanalyze_days`, and surfaces the densest unanalyzed field(s) (with their
measured density). The agent runs the 6 steps over that field's real subgraph and — only if it
can name a real force — writes one `lacuna/<slug>` page (`type: lacuna`). It **defers** any
field where the gap has no nameable force. Coverage is not a goal.

### The page

Frontmatter mirrors the method 1:1: `field_mapped`, `hidden_axis`, `force` (load-bearing),
`discovered_vs_failed`, `fill`, `confidence`, `surround_density`, `needs_review: true`, plus
`see_also`/`sources` for the cited subgraph and (soft edge) `fill_trigger` +
`prediction_candidate`. The body opens with a caveat — *"a structural inference grounded in the
cited subgraph, not a verified claim"* — then the six sections.

### In the reader

The reader is domain-agnostic, so `lacuna/` and `type: lacuna` surface with **zero reader
changes** (like glossary's `term`). The low-trust read comes from the `confidence` and
`surround_density` chips (both surface as primary metadata) plus the body caveat. A pack that
wants to *showcase* lacuna can pin it via `rail_top_section: {label: SYNTHESIS, namespaces:
[lacuna]}` or expose a by-kind group via `display_groups` in its `schema.yaml` — both already
supported by the reader; this extension ships neither.

## Config

| key | default | meaning |
|-----|---------|---------|
| `min_density` | 8 | a field must have ≥ N referencing pages to be worth mapping (a thin patch ⇒ extrapolation) |
| `reanalyze_days` | 90 | don't re-analyze the same field within this window (rotation) |
| `batch_size` | 3 | how many of the densest unanalyzed fields to surface per run (the agent writes at most one) |

## When to use it (and when not)

**Enable it when** the vault has a **dense, well-mapped concept graph** and you want disciplined
whitespace/positioning/research-direction discovery — and especially alongside
`okengine.predictions`, which turns testable fills into graded forecasts (the verification the
method admits it lacks).

**Skip it when** the concept graph is **thin** (few clusters reach `min_density` ⇒ every gap is
extrapolation), or you only want verified knowledge — lacuna deliberately produces low-trust
*coordinates*, and testing whether the floor holds stays the operator's job.

**Opt-in** (an agent lane, so it spends model budget) — unlike core `okengine.contradictions`.

## Enable

```
framework extensions enable <pack> okengine.lacuna
# the pack must declare `concept` + `entity` types (schema_refs) and must not already
# own a `lacuna` type or namespace (own = new ids only)
```
