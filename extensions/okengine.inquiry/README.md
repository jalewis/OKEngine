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
| `inquiry-collect` | `35 4 * * *` | runs each open inquiry's terms through its declared connector; records per-term yield and which records each term returned (the evidence ledger). no_agent, bounded, idempotent. |
| `inquiry-dossier` | `45 5 * * *` | renders `dashboards/inquiry/<slug>.md` + an index: the question, per-term health, collected-vs-compiled evidence, and the grouped analysis. no_agent. |
| `inquiry-analyze` | `50 6 * * *` | **agent lane.** Answers each open inquiry's standing questions as `okengine.assessments` pages from the evidence its terms collected; reassesses when new evidence compiles. Receipt-enforced per question. |

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

## Evidence provenance

Collection is only useful if something downstream can ask *"what evidence does this inquiry
have?"* A connector record does not say which query fetched it, and one record is routinely
returned by several terms, so the answer cannot be a field on the record or on the compiled
source page.

`inquiry-collect` therefore keeps a ledger, `.okengine/inquiry/evidence-ledger.json`, mapping
each archived record directory (`raw/connectors/<archive>/records/<native-id>`) to every
`<inquiry>::<term>` that returned it, with first/last seen dates. A compiled source page joins
back through the `raw:` path the compile lane already carries. `inquiry_lib.evidence_by_term()`
is the single implementation of that join; the dossier uses it, and any lane that reasons over
an inquiry should too.

- **Deterministic, not model-carried.** The compile lane is a model; a provenance field it is
  asked to copy is dropped the day the model changes (okengine#194).
- **`new` vs `returned`.** A recency-sorted, capped query returns mostly the same page every
  day. The dossier shows records the term had *never* returned before, not the daily page size
  (the old cumulative `total_records` reported 650 for a term holding 50 distinct records).
- **Needs a per-record archive.** A connector with `archive.enabled: false` has no record path
  a source can cite, so nothing is attributed.
- **Not attributable:** a source compiled from a whole search-*response* page
  (`raw/connectors/<archive>/raw/<digest>`) holds every result of one query and maps to no single
  record. It is left unattributed, never guessed.
- **Starts at the first run after deploy.** Records collected earlier carry no term
  provenance; terms re-attribute whatever they still return.

## Answering the question: `inquiry-analyze`

Collection and a dossier do not answer anything. `inquiry-analyze` is the lane that does: it turns
each **standing question** into an **assessment** (`okengine.assessments`) with competing
hypotheses, adversarial evidence, and a stated confidence, from the evidence the inquiry's own terms
collected.

Standing questions are declared on the inquiry page. Without them, the headline `question` stands
in:

```yaml
questions:
  - Do frontier safety frameworks bind in practice?          # key derived from the text
  - key: external-testing                                     # explicit key: reword freely
    question: Is external pre-deployment testing expanding or contracting?
```

- **One page per question, named by the selector.** `assessments/<inquiry>-<key>`. The model never
  names the page, so a rerun updates it instead of creating a second one (#54). Rewording a question
  keeps its key and therefore its page.
- **When it wakes.** A question with no assessment wakes once the inquiry has
  `analyze_min_sources` attributed, compiled sources. An assessed question wakes again when
  evidence it **cites** moves — a cited page rewritten after it, or a page in its `sources`
  removed (read from `okengine.reevaluation`'s dependency sidecar; if that is absent the digest
  says cited changes are undetectable) — or when `analyze_min_new` sources have compiled since
  it was last written. Moved evidence outranks new evidence.
- **Belief-change provenance.** A reassessment never erases the prior judgment: it adds a dated
  `## Revisions` entry with the old and new claim and confidence and the evidence that caused
  the change. `would_increase_confidence` / `would_decrease_confidence` are required fields.
- **Evidence** is `inquiry_lib.evidence_by_term()`, the same join the dossier renders.
- **What the write path enforces.** The contract confines the lane to `assessment` pages in
  `assessments/`, requires `sources` and `subject_ref`, and rejects any that do not resolve, so an
  invented citation fails the write. Pages land `needs_review: true`.
- **Receipts, not write counts.** Completion is `per-selected-item`. `insufficient-evidence` is a
  correct disposition for a question the collected material cannot discriminate on, not a failed run.
- **`adversarial-evidence-review`** (okengine.assessments, 07:12) reads the result the same morning
  and flags confidence moves that rest on manipulable evidence.
- **Start narrow.** `analyze_batch` defaults to 1. Grade the first runs by how each question was
  disposed, then widen.

## Steering discovery: `fields`

An inquiry can name the **concept fields** it bears on, for `okengine.lacuna` to map:

```yaml
fields: [safety-alignment, concepts/t/transparency-training-methods-for-interpretability-research]
```

- **Declared, not inferred.** On the first live vault the inquiry's evidence linked no concept at
  all, so deriving fields from evidence would have nominated nothing.
- The **dossier lane** writes each open inquiry's fields to `.okengine/inquiry/lacuna-nominations.json`
  daily, rewriting the file whole so a withdrawn field stops steering discovery. A field with no
  concept page is not nominated and `framework validate` FAILs it (same class as a missing connector).
- **Lacuna** ranks inquiry fields first and may map one below its `min_density`, down to
  `inquiry_min_density` (3), with an explicit EXTRAPOLATION warning. It still needs a real concept
  page and respects `reanalyze_days`. `framework validate` WARNs if fields are declared without
  `okengine.lacuna` enabled.

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
framework extensions enable <pack> okengine.assessments   # to ANSWER standing questions
```

Then declare a connector in `<pack>/connectors/`, write an inquiry page under `wiki/inquiry/`,
and run `framework validate <pack>` — it fails loudly if the two do not line up.

Answering standing questions needs **`okengine.assessments`**, because the answers are assessment
pages. Without it, collection and the dossier still run; `framework validate` WARNs that open
inquiries cannot be answered, and the inquiry-analyze wake-gate stays asleep and says why rather
than waking to have every write rejected.

**Why opt-in, not core** (okengine#854 step 4, decided): the lanes are inert without inquiry pages,
but the schema fragment is not — a core inquiry would put an `inquiry` type and namespace into
every vault's governing schema and make a composed-schema artifact permanent in every pack (no
core extension owned schema before). One `enable` makes it available to any pack.

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
