# okengine.inquiry

Research topics as **declared objects** (okengine#746).

## The problem

A vault had no first-class way to say *"this is what I am researching."* The topic lived in four
surfaces that nothing reconciled:

| surface | what it held | who read it |
|---|---|---|
| `pack.yaml` `mission:` | one sentence of prose | humans only |
| `feeds/feeds.opml` | what actually got collected | `feed_fetch.py` |
| `crons/engine-template-prompts.json` | what the agent was told to look for | the model lanes |
| `wiki/CLAUDE.md` | the persona's standing instructions | the model lanes |

Adding a research topic meant hand-editing all four and hoping they agreed. Nothing validated
that they did. Asking a second question meant doing it again.

## The object

An `inquiry` page **is** the declaration.

```yaml
---
type: inquiry
question: How likely is it that AI causes human extinction, and what would reduce that?
status: open              # open | paused | closed
opened: 2026-09-11
connector: news.article-search
terms:
  - AI existential risk extinction
  - AI alignment superintelligence
  - query: frontier model governance regulation
    label: governance
    params: {source_tier: "2"}
assessments: [assessments/p-doom-estimates-are-elicited-not-measured]
solutions:   [solutions/ai-control-trusted-weaker-supervisor]
---
```

`terms` is load-bearing: it is the single machine-readable statement of what this inquiry
collects, and the collect lane derives from it directly. There is no second copy in a cron
definition, a prompt, or a service's database. Adding another research question is adding
another page — no engine change, no cron edit.

## Lanes

| lane | schedule | does |
|---|---|---|
| `inquiry-collect` | `35 4 * * *` | runs each open inquiry's terms through its declared connector; records per-term yield. no_agent, bounded, idempotent. |
| `inquiry-dossier` | `45 5 * * *` | renders `dashboards/inquiry/<slug>.md` + an index: the question, per-term health, and the grouped evidence. no_agent. |

Collection is delegated to the engine's declarative `source_connector.py` runtime, which owns
the allowed-hosts, private-network, rate-limit, archive and secret-reference contract. This
extension opens no socket of its own and holds no credential.

### Parameter binding

A term's `query` binds to the connector's **single required input**. A connector with several
required inputs must have the rest supplied by `collection_params` (applied to every term) or
the term's own `params` (which win). Precedence, narrowest last:

```
connector defaults  <  inquiry.collection_params  <  term.params  <  the query itself
```

The query binds last on purpose, so a stray param can never overwrite it.

A connector declaring **no** required inputs is a contract error, not a silent full pull: a term
that cannot parameterize its connector would fire the same unfiltered request once per term.

## Two gates, and why there are two

**`framework validate` → `check_inquiries`** is the deploy-time gate. The schema fragment already
makes `question` / `status` / `terms` required, so the write path rejects a structurally
incomplete inquiry. What a schema cannot express is whether the connector an inquiry names
actually exists in this deployment. That gap fails *silently* at runtime: the lane skips the
inquiry, the dossier renders empty, and an operator reads the empty dossier as "nothing is
happening in this field" rather than "I misspelled the connector."

**The dossier's DRY reporting** is the standing detector. Validation catches a term whose
connector does not exist; nothing else catches a term whose connector exists, runs clean, and
answers nothing. A term with no yield for `dry_term_days` (default 14) is called out on its
dossier, so a misjudged query surfaces continuously rather than at the next manual review.

A closed inquiry is exempt from the reachability gate — retiring a connector must not be blocked
by the archived questions that once used it. A **paused** inquiry is still gated, because paused
is temporary.

## TRAP: do not inherit another domain's relevance floor

Collection services score records for their **own** primary domain, and those scores are noise or
worse outside it. Measured on one deployment's news service while designing this extension: the
same story about AI extinction risk scored

| outlet | the service's `security_relevance_score` |
|---|---|
| a tier-2 technology outlet | 0.008 |
| a state broadcaster | 0.886 |

A `min_security_relevance >= 0.5` floor — entirely correct for that service's *security* vault —
would have kept the propaganda copy and dropped the reputable one.

So this lane applies **no** score floor of its own and passes through only what the inquiry
explicitly declares. An inquiry running outside a connector's home domain should leave such knobs
unset. Source-authority tiers deserve the same suspicion: on that same service, two respected
specialist publications sat in the lowest tier alongside content farms, because the tier measures
domain attribution authority rather than topical quality.

## Deliberately NOT a server-side watchlist

Several collection services offer a saved-query object with a delta cursor, which looks like the
natural home for an inquiry's terms. It is not. Storing the terms in the service's database puts
the declaration outside git, outside the pack, and outside every engine gate — re-creating
precisely the split this extension exists to close. The terms live in the page; the lane keeps
its own cursor.

## Enable

```
framework extensions enable <pack> okengine.inquiry
```

Then declare a connector in `<pack>/connectors/`, write an inquiry page under `wiki/inquiry/`,
and run `framework validate <pack>` — it fails loudly if the two do not line up.

## Solutions are remedy assessments, not a new type

The prescriptive half — "what would actually fix this?" — is `okengine.assessments` with
`assessment_kind: remedy`, not a type of its own (okengine#746, decided after weighing both).

The reasoning: a proposed intervention has evidence for and against it exactly like a factual
claim does. The assessments fragment already carries `alternatives`, `adversarial_evidence` with
per-item diagnosticity and source independence, `would_increase_confidence` /
`would_decrease_confidence`, and `consequence`. Duplicating that contract for a second type
would have meant maintaining two copies of the hardest part of the schema.

Only the three things a remedy has that a factual claim does not were added, all **optional** so
no existing assessment page is affected:

| field | holds |
|---|---|
| `remedy_cost` | rough cost or effort |
| `remedy_prerequisites` | what must already be true for it to be available (list) |
| `remedy_decision` | `proposed` / `adopted` / `rejected` / `deferred` |

`remedy_decision` is deliberately **not** a confidence scale. `adopted` says someone decided, not
that the evidence got stronger. A remedy can be well-evidenced and rejected, or adopted on thin
evidence, and a reader has to be able to tell which — so the decision state and the confidence
band stay separate fields with disjoint vocabularies.

### The trap this creates

Reusing the type means **page type alone can no longer bucket a page**. Routing every
`assessment` into the Assessments column would file every remedy under "is this true?" when it
answers "would this work?", and the dossier's Solutions section would read empty forever while
remedies piled up. `_bucket()` checks the kind first for exactly that reason, and
`test_a_remedy_assessment_lands_under_solutions_not_assessments` is the negative fixture.

A pack that owns a `solution` type of its own is still routed correctly; the convention adds a
path rather than replacing one.

## Soft edges

`assessments` / `predictions` / `solutions` group the evidence answering an inquiry
(`solutions` being remedy assessments, above). These are
**soft** conventions with no hard `requires` on the extensions owning those types (the
lacuna↔predictions pattern): an inquiry works with none of them enabled, and gains a populated
dossier section for each one that is. Pages may also attribute themselves the other way by
carrying `inquiry: <slug>` in their own frontmatter; the dossier reads both directions, because
in practice both occur.
