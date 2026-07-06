# okengine.glossary

First-party extension: a **domain glossary**. The lexical companion to concepts — where
*concepts* group entities by recurring theme, *terms* define the vocabulary itself.

It's the reference example for the parts of the extension model the other first-party
extensions don't exercise:

| Area | How glossary tests it |
|------|------------------------|
| **Bring-your-own-schema — Own** (#133) | the `schema/glossary.schema.yaml` fragment OWNS a new `term` type + `glossary` namespace, folded into the composed schema (owner `ext:okengine.glossary`) only when enabled |
| **Scoped MCP write to an own namespace** (#132) | the agent writes `glossary/<slug>` pages of its own type, validated against the composed schema |
| **`config:` block** | `min_references` — define a term once N pages link it |
| agent operation + bundled prompt + `tier:` hint | (shared with predictions) |

## How it works

Mirrors `concept-backfill`: a page **seeds** a term by linking `[[glossary/<slug>]]`. The
daily wake-gate `select_undefined_terms.py` counts those links; once a slug reaches
`min_references` (default 3) and has no page yet, the agent runs with the bundled prompt and
writes the definition page (`type: term`) — grounded in what the vault already says, never
invented. Disabling the extension stops the lane and removes the `term` type from the composed
schema (existing pages remain, now untyped-but-present).

## When to use it (and when not)

**Enable it when** the vault is **jargon-heavy** — AI research, security, medicine, law —
and a reader (or the agent) benefits from a grounded, cross-linked definition for an acronym
or term-of-art. It's **demand-driven**: only terms that are actually referenced enough
(`min_references`) get a page, so effort tracks real usage instead of pre-defining a
dictionary. Good for **onboarding** (look up unfamiliar terms) and for giving the agent one
canonical definition to link instead of re-explaining a term across pages.

**Skip it when:**
- **The persona won't seed `[[glossary/<slug>]]` links.** Nothing populates a glossary
  unless ingest/curation tags terms — without that instruction in the pack's `CLAUDE.md`, it
  stays empty. Adopt it *with* a persona rule to tag terms, not before.
- **`concepts` already covers your need.** Concepts synthesize a page from accrued
  `[[concepts/<slug>]]` links too — *themes that group entities* vs. *definitions of
  vocabulary*. The distinction is real but thin; one mechanism is often enough.
- **The domain isn't jargon-heavy** — a glossary of obvious terms is noise.

It's **opt-in** (an agent lane, so it spends model budget) — unlike core
`okengine.contradictions`. It's also the engine's reference example of a **schema-owning**
extension (it owns the `term` type), so it doubles as the template for higher-value
own-schema extensions (a domain scorer, an importer that owns its raw type).

## Enable

```
framework extensions enable <pack> okengine.glossary
# the pack must not already own a `term` type or `glossary` namespace (own = new ids only)
```

Opt-in (it spends model budget) — unlike core `okengine.contradictions`.
