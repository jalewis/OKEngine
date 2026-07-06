# OKEngine application catalog

Where the engine's core pattern can become a vertical pack, and which reusable
extensions already carry each vertical. Companion to
[`docs/authoring-a-pack.md`](authoring-a-pack.md) and
[`docs/authoring-an-extension.md`](authoring-an-extension.md); tracked as
okengine#171. This is a roadmap document, not a commitment — verticals here are
described generically; any concrete deployment supplies its own ontology,
persona, and sources as a pack.

## The reusable pattern

Every vertical below is the same pipeline with a different ontology:

```text
ingest -> normalize -> link -> score -> forecast -> brief -> audit -> expose (reader/MCP)
```

A vertical is a good fit when the problem has: many changing sources, many
related entities, recurring judgment calls, provenance requirements, and
repeatable operator workflows ("brief me / rank this / what changed / what
next"). The pack supplies the domain half (schema, persona, feeds, crons); the
engine and tier-1 extensions supply the operations.

## What already exists (the real primitives)

Seventeen tier-1 extensions ship in `extensions/` today. Grouped by pipeline
stage:

| Stage | Extension ids | What they own |
|---|---|---|
| ingest scope | `okengine.relevance-gate` | flag off-thesis source pages against operator-owned scope config (deterministic pass + cheap-model pass); never deletes |
| graph hygiene | `okengine.dedupe`, `okengine.embeddings` | name/alias duplicate detection + merge proposals; semantic near-duplicate candidates (the sidecar exemplar) |
| link / structure | `okengine.glossary`, `okengine.viz` | term synthesis from `[[glossary/*]]` references; strategic maps (evolution × value-chain) over the concept graph |
| score / analyze | `okengine.competitive-analytics`, `okengine.events`, `okengine.lacuna`, `okengine.frontier-watch` | quadrants/battle-cards/acquirer-movement signals (watchlist is pack config); scored append-only event ledger (event types + weights are pack config); structural-gap discovery with fill proposals; demand/supply whitespace theses |
| forecast | `okengine.predictions` | falsifiable dated forecasts: candidate filing, grading at resolution, re-grading on new evidence |
| audit | `okengine.grounding`, `okengine.contradictions`, `okengine.completeness`, `okengine.critic` | claim-vs-citation audits; ACTIVE/EMPTY/RESOLVED contradiction dashboard; pack-declared completeness rules → explainable gap queue; wake-gated LLM critique of the flagship deliverable |
| expose / present | `okengine.timeline`, `okengine.messaging-synthesis` | reverse-chronological vault-wide dated-content dashboard; positioning/battle-card/value-prop synthesis over a competitive graph |

Engine facilities that are **not** extensions but count as coverage:

- **briefing** — the engine-template `daily-brief` lane
  (`scripts/cron/select_daily_brief.py`): a generic what-changed digest
  (window sources, entity churn, due predictions, new completeness gaps); the
  pack supplies only the brief's voice as a prompt. Do not build a briefing
  extension per vertical.
- **ingest/backfill machinery** — the engine-template raw/entity/concept
  backfill selectors, feeds, cron tiers, budget guard.
- **conformance + enforced write path** — `tools/schema_validator.py` and the
  MCP write server; every vertical's provenance/review guarantees ride on
  these for free.
- **read surfaces** — reader, cockpit, read-query MCP.

## Extension gaps (generic families still missing)

- **risk-ranking** — target-relative, evidence-backed, explainable ranking of
  actors/vendors/targets against a configured entity/sector/asset profile.
  **Shipped as `okengine.actor-risk-ranking` (#170)** — deterministic, over the
  precomputed backlink graph, ontology-by-config (the vendor-risk pack reuses it
  for `vendor` pages). Four of the six commercially sharp verticals want it, and
  now have it.
- **entity-resolution** — canonical IDs, alias convergence, cross-source and
  cross-pack identity. `okengine.dedupe` + `okengine.embeddings` cover
  in-vault duplicates; nothing yet owns "these five spellings across three
  feeds are one organization." Needed hard by any vertical whose entities
  arrive named inconsistently (vendors, litigants, agencies, companies).
- **deadline-calendar** — forward-looking dated obligations (comment periods,
  filing deadlines, contract renewals, resolution dates) as a first-class
  queue. `okengine.predictions` grades dated claims and `okengine.events`
  ledgers the past; nothing renders "what is due, when, owned by whom."
- **per-entity timeline** — `okengine.timeline` is one vault-wide dashboard;
  matter/vendor/account-scoped durable event histories (litigation dockets,
  vendor incident histories) are a distinct render over the same data.
- **dependency-map** — typed graph traversal + rendering for
  depends-on/supplies/composed-of chains (supply tiers, service topologies).
  `okengine.viz` maps concepts on strategic axes; it does not walk dependency
  edges.

Rule of thumb from the issue, kept: prefer extending one of these generic
families over minting a per-vertical extension.

## Coverage matrix

Existing = ids from `extensions/` plus the engine facilities above (engine
briefing/ingest assumed everywhere and not repeated). Gap names are the
generic families above; `risk-ranking*` = `okengine.actor-risk-ranking`, **now shipped** (#170).

| Vertical | Existing coverage (extension ids) | Missing |
|---|---|---|
| Regulatory radar | relevance-gate, events, timeline, predictions, glossary (terms of art), grounding, completeness, contradictions | deadline-calendar, entity-resolution (agencies/instruments), per-entity timeline |
| Vendor / supply-chain risk | events, timeline, predictions, completeness (stale-vendor rules), relevance-gate, grounding, dedupe, embeddings, contradictions | risk-ranking* (near-exact fit), entity-resolution, dependency-map, per-entity timeline |
| Personal board / exec intel | competitive-analytics, messaging-synthesis, events, predictions, frontier-watch, timeline, critic | risk-ranking*, entity-resolution |
| M&A target radar | competitive-analytics (acquirer/movement signals exist today), frontier-watch, lacuna, predictions, events, timeline, dedupe | risk-ranking* (fit-scoring variant), entity-resolution |
| Sales account intelligence | competitive-analytics, messaging-synthesis (battle cards exist today), events, timeline, predictions, relevance-gate | risk-ranking* (churn/renewal variant), entity-resolution; real value gated on private CRM feeds |
| Litigation / legal matter | timeline, events, contradictions (argument conflicts), predictions, grounding, completeness, glossary | per-entity timeline (per-matter), deadline-calendar, entity-resolution (parties/counsel/judges) |
| Scientific field observatory | frontier-watch, lacuna, contradictions (replication conflicts), glossary, grounding, embeddings, timeline, viz | entity-resolution (labs/authors) |
| Public health / biosecurity | events, timeline, predictions, grounding, contradictions, completeness | risk-ranking*, per-entity timeline; heavy review constraints |
| City / local government | events, timeline, completeness, relevance-gate, dedupe | entity-resolution (contractors/parcels), dependency-map, deadline-calendar |
| Investment thesis vault | predictions (calibration is the product), contradictions (disconfirmation queue), events, grounding, critic, timeline | risk-ranking*, deadline-calendar (earnings/filings); advice-framing constraints |
| Engineering architecture memory | completeness (ownership/staleness rules), glossary, timeline, events, contradictions, viz | dependency-map, per-entity timeline (per-service) |
| Incident / postmortem learning | events (near-exact fit), timeline, completeness (runbook/detection-gap rules), embeddings (similar-incident retrieval), predictions | per-entity timeline, dependency-map |
| Policy / geopolitical scenario | events, predictions, contradictions, frontier-watch, grounding, timeline | risk-ranking*, entity-resolution; hardest source-quality/bias burden |
| Talent / hiring market | events, timeline, frontier-watch (skill clusters), competitive-analytics, predictions | entity-resolution; person-data handling constraints |
| Grant / funding opportunity | relevance-gate (fit gating), lacuna (topic whitespace), completeness, timeline, predictions | deadline-calendar (near-blocking), entity-resolution |
| Narrative / media monitoring | contradictions, events, timeline, grounding (source reliability), relevance-gate, embeddings | entity-resolution, risk-ranking*; overclaim-of-intent risk |
| Product feedback brain | contradictions (sales-vs-support narratives), events, completeness, lacuna, embeddings, relevance-gate | entity-resolution (customers/segments); value gated on private feeds |
| Standards / protocol watch | events, timeline, predictions (adoption forecasts), glossary, frontier-watch, completeness | deadline-calendar (comment/ballot windows) |

Reading the matrix: coverage is uniformly wide — the audit and dashboard layers
serve everything — so the discriminators are (1) whether the *scoring* step is
carried by an existing analytic (`okengine.competitive-analytics`,
`okengine.events`) or the shipped `okengine.actor-risk-ranking`, (2) whether public sources
deliver value before private feeds, and (3) review-safety of the output
framing.

## Selection criteria (condensed)

Prioritize when **all** of: sources change frequently; users already pay for
monitoring/analysis; provenance matters; the entity graph is dense; the
workflows are recurring brief/rank/forecast; human review is acceptable; value
appears from public/open sources before private feeds; existing generic
extensions carry most of the work.

Deprioritize when **any** of: one-shot search suffices; no durable graph;
proprietary data is required before any value exists; users need transaction
execution rather than analysis; high-stakes outputs cannot be framed as
decision support.

## First bets (ranked)

Scored against the matrix on (a) reuse of existing machinery, (b) clarity of
buyer, (c) safety of framing. The issue's six "commercially sharp" candidates
were the pool; three survive.

### 1. Vendor / supply-chain risk

The heaviest reuser: `okengine.events` (incident ledger), `okengine.timeline`,
`okengine.predictions`, `okengine.completeness` (vendor-record staleness),
`okengine.relevance-gate`, `okengine.grounding`, `okengine.dedupe` +
`okengine.embeddings` — and the shipped `okengine.actor-risk-ranking` (#170) is *almost exactly*
this vertical's core scoring step (target-relative, evidence-backed,
explainable, organization-scoped). Buyers (procurement, GRC, security,
resilience) already pay for vendor monitoring. Framing is safe: ranking
organizations against an operator-configured dependency profile is decision
support, not advice about people.

- **Target users:** procurement, GRC, security, resilience teams.
- **Entity types:** vendor, product, component, contract, location, incident,
  vulnerability, dependency (typed edge or page).
- **Source plan:** public first — vendor newsrooms/status pages, vulnerability
  advisories, breach disclosures, sanctions/watchlist publications, financial
  press feeds; later private — contract registries, internal asset inventories
  as operator config.
- **Top workflows:** "what changed for vendors we depend on" (daily-brief);
  vendor risk ranking (#170 lane); escalation briefing on material ranking
  change; replacement-candidate lookup; dependency-chain walk.
- **First dashboard:** vendor risk rankings with drivers, confidence, missing
  evidence, and freshness — the #170 dashboard with a vendor ontology.
- **Extensions:** existing as above; gaps — risk-ranking (#170),
  entity-resolution, dependency-map, per-entity timeline.
- **Review constraints:** no person-scoring; sanctions/litigation claims must
  carry citations (grounding-audited) before appearing in rankings; ranking
  changes that would trigger escalation get human review.

### 2. Regulatory radar

Second-widest reuse and the least ambiguous buyer pain (compliance teams
staring at rule churn). `okengine.events` + `okengine.timeline` carry the
what-changed record; `okengine.predictions` carries "likely rule movement";
`okengine.glossary` is unusually valuable (regulatory terms of art);
`okengine.contradictions` catches guidance that conflicts across issuers;
`okengine.grounding` + the write path give the provenance a compliance
artifact needs. Main gap is deadline-calendar — comment periods and effective
dates are the product's spine — which is a generic family, not a one-off.

- **Target users:** compliance teams, legal ops, policy analysts, trade
  associations.
- **Entity types:** instrument (law/rule/guidance), issuer/agency, enforcement
  action, obligation, impacted product/activity, deadline, comment period.
- **Source plan:** public first — official registers/gazettes, agency
  rulemaking feeds, enforcement press releases, consultation portals; later —
  operator-supplied jurisdiction/product scope config narrowing via
  `okengine.relevance-gate`.
- **Top workflows:** "what changed this week" per jurisdiction (daily-brief);
  obligation map per product; deadline calendar; rule-movement predictions;
  contradiction review across issuers.
- **First dashboard:** the deadline calendar (due obligations + open comment
  periods, owner, days remaining, staleness flags).
- **Extensions:** existing as above; gaps — deadline-calendar,
  entity-resolution (agency/instrument aliases), per-entity timeline
  (per-instrument history).
- **Review constraints:** outputs are monitoring/decision support, never legal
  advice — persona and reader banners must say so; obligation interpretations
  require human review before the pack marks them confirmed; every obligation
  links its instrument text.

### 3. M&A target radar

Chosen over sales-account intelligence because its scoring step partially
exists *today*: `okengine.competitive-analytics` already computes
acquirer/movement signals, quadrants, and battle-cards over an
operator-supplied watchlist, and `okengine.frontier-watch` + `okengine.lacuna`
supply the whitespace/strategic-fit layer. Public sources (funding, hiring,
patents, partnerships, distress signals) deliver value before any private
data — sales-account intelligence, by contrast, is gated on private CRM
feeds, which the selection criteria explicitly deprioritize. Buyer (corporate
development, private equity, venture studios) is budgeted and recurring.

- **Target users:** corporate development, private equity, venture studios,
  strategy teams.
- **Entity types:** company, sector/segment, funding event, patent/filing
  signal, partnership, key person (public role only), acquirer, thesis.
- **Source plan:** public first — funding/registry feeds, hiring-page deltas,
  patent publications, press/partnership announcements; operator watchlist
  (segments, axes, fit criteria) as pack config per the
  competitive-analytics pattern.
- **Top workflows:** target ranking by strategic fit; target dossier
  ("what changed for this company"); likely-to-sell / likely-to-buy forecasts
  (`okengine.predictions`); whitespace scan for un-watched segments.
- **First dashboard:** ranked target list per segment with fit drivers,
  movement signals, confidence, and evidence links.
- **Extensions:** existing — competitive-analytics, frontier-watch, lacuna,
  predictions, events, timeline, dedupe; gaps — risk-ranking (#170,
  fit-scoring variant), entity-resolution (company aliases across feeds).
- **Review constraints:** no valuation or investment advice — fit analysis
  only; person pages restricted to public roles; distress-signal claims
  require multiple independent sources (grounding-audited) before affecting a
  ranking.

Runners-up, and why not now: **sales-account intelligence** (private-feed
gated; revisit when a deployment supplies CRM ingest), **engineering
architecture / incident memory** (excellent internal fit and heavy
`okengine.events` reuse, but sources are internal rather than feed-driven —
better as a pack pattern doc than a first commercial bet), **litigation**
(strong pull, but human-review-first framing plus per-matter timeline +
deadline-calendar gaps make it a second-wave candidate once those families
exist).

## What we deliberately do not build

From the issue's deprioritize list and gotchas, made policy:

- **No "generic AI analyst" demos.** If there is no durable graph and no
  recurring workflow, it is not an OKEngine application.
- **No verticals requiring real-time transaction execution** (trading,
  bidding, dispatch). The engine is analysis and decision support.
- **No verticals whose only value needs hidden proprietary data on day one.**
  Packs must start on public/open sources; private feeds are a later
  operator input.
- **No unreviewed high-stakes advice surfaces** — legal, medical/public
  health, financial, or person-specific outputs ship review-first or not at
  all (see [`docs/human-review.md`](human-review.md)).
- **No opaque scores.** Every ranking ships drivers, evidence links,
  confidence, and explicit unknowns (#170 sets the bar).
- **No private seeds in extensions.** Watchlists, targets, and scope live in
  pack/operator config; extensions ship zero seeds (the
  competitive-analytics rule, generalized).
- **No per-vertical extension sprawl.** A new vertical justifies a new
  extension only when no generic family (risk-ranking, entity-resolution,
  deadline-calendar, per-entity timeline, dependency-map) covers the
  operation.
- **No filesystem side-doors.** All generated pages go through the enforced
  MCP write path; dashboards carry freshness indicators or they do not ship.

## Follow-ups

PRD-level issues to open from this catalog (drafts accompany okengine#171):

1. Vertical PRD: vendor / supply-chain risk pack
2. Vertical PRD: regulatory radar pack
3. Vertical PRD: M&A target radar pack

Each depends on #170 (risk-ranking) for its scoring lane; regulatory radar
additionally motivates the deadline-calendar family, and all three motivate
entity-resolution.
