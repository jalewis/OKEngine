# OKEngine executive guide

**Audience:** executives, product leaders, operators, architects, and governance teams  
**Purpose:** explain what OKEngine is, how its parts fit together, and how an
agent-maintained knowledge system turns evidence into governed decision support.

**Documentation baseline:** OKEngine v0.13.7, reviewed 2026-07-23. Machine-readable
manifests remain authoritative where a release changes an inventory or version.

## Executive summary

OKEngine is an **Open Knowledge Engine**: a framework for building continuously
maintained, evidence-backed knowledge systems. Instead of answering a question and
discarding the work, an agent compiles source material into a durable Markdown
knowledge graph, maintains that graph over time, and exposes it through a reader,
an operational cockpit, search, exports, and agent APIs.

The same engine can support cyber-threat intelligence, competitive intelligence,
AI research, vendor risk, or another evidence-heavy domain. A **pack** supplies the
domain model and judgment; OKEngine supplies the governed machinery. Several
compatible packs can be composed as a **bundle**. Optional **extensions** add
reusable analytical operations such as predictions, completeness analysis,
contradiction detection, and risk ranking.

The result is not merely “chat with documents.” It is a living analytical product:

- source evidence remains traceable;
- entities and claims accumulate rather than being rediscovered;
- uncertainty and review status remain visible;
- scheduled work detects change, gaps, contradictions, and drift;
- deterministic code handles mechanical work while models handle bounded judgment;
- every deployment owns its sources, policy, models, and generated knowledge.

## The system in one view

```text
                         DEPLOYMENT POLICY
               sources · models · budgets · review rules
                                  |
                                  v
 SOURCES  -->  RAW EVIDENCE  -->  DOMAIN COMPILATION  -->  KNOWLEDGE GRAPH
 feeds         immutable          pack schema,            sources, entities,
 APIs          capture            persona, prompts        observations, claims
 reports                           and deterministic       assessments, concepts
 notes                              importers              predictions, briefs
                                      |                         |
                                      v                         v
                                GOVERNED WRITES  <----  CONTINUOUS OPERATIONS
                                schema validation       link, resolve, audit,
                                provenance, review      repair, reassess, index
                                field-loss guards       detect gaps and conflicts
                                                              |
                           +----------------------------------+------------------+
                           v                                  v                  v
                      READER / COCKPIT                    MCP / SEARCH         EXPORTS
                      people and teams                    other agents         md/docx/pdf
```

At a practical level:

```text
deployment = Hermes runtime + OKEngine + pack or bundle + enabled extensions
```

## The major components

| Component | Role | Owns |
|---|---|---|
| **Hermes runtime** | Runs the agent and connects model providers and transports | Agent loop, model access, runtime plugins |
| **OKEngine** | Provides domain-neutral knowledge operations and governance | Write path, validation, scheduling, indexing, repair, search, reader, cockpit, MCP, deployment tooling |
| **Pack** | Defines a domain and its analytical judgment | Schema additions, persona, prompts, feeds, importers, domain crons, source policy |
| **Bundle** | Declares a compatible set of packs to install together | Composition recipe; it does not duplicate the component packs |
| **Extension** | Adds an optional analytical capability over a vault | Its operation and, where needed, additive schema |
| **Operator** | Connects deployment-local or private systems | Retrieval, normalization, receipts, and bounded evidence synchronization |
| **Application** | Combines the graph and operations into a user outcome | A workflow such as continuous hypotheses or threat-informed detection |
| **Vault** | Holds one deployment's durable knowledge | Raw evidence, Markdown graph, indexes, operational state, local policy |

The separation matters. The engine should not decide what makes a threat actor
credible or a vendor risky. A pack should not reimplement safe writes, indexing,
or repair. An operator should not silently turn a retrieval system into the
publisher of record. Clear ownership makes the system composable and auditable.

## How knowledge is produced

### 1. Acquire and preserve evidence

Feeds, APIs, reports, repositories, and operator notes are captured under `raw/`.
Raw material is the replayable evidence boundary: processing can improve without
pretending the original input changed.

### 2. Normalize authoritative data

Deterministic importers convert bounded, well-structured sources into conformant
pages without an LLM. Examples include ATT&CK records, CISA KEV entries, Sigma
detections, and structured research indexes. Importers are preferable whenever
the source semantics are explicit.

### 3. Apply bounded model judgment

Models classify prose, extract candidate facts, connect evidence, and synthesize
readable analysis. Prompts are deliberately scoped: a model proposes or updates a
specific artifact under schema and write constraints; it is not given unlimited
authority over the corpus.

### 4. Write through governance

Agent writes pass through the enforced write service. It validates the composed
schema, protects reserved files and existing fields, applies namespace rules,
records provenance, and tombstones rather than silently deleting knowledge.

### 5. Assemble and assess

Multiple source-owned observations can be reconciled into a canonical entity.
Claims and assessments retain evidence, confidence, status, and review state.
Ambiguous identity matches remain candidates or quarantined evidence rather than
being promoted into canonical facts.

### 6. Maintain continuously

Scheduled operations refresh indexes and backlinks, reshelve and reshard pages,
find broken references and duplicates, re-evaluate claims, grade predictions,
surface knowledge gaps, and repair recoverable drift. Backfill and repair queues
make missed work eventually convergent rather than permanently lost.

### 7. Deliver decisions, not just documents

The reader presents individual pages and their provenance. The cockpit organizes
briefs, predictions, gaps, review queues, and operational views. MCP and search
allow other agents to reuse compiled knowledge. Markdown, DOCX, and PDF exports
support ordinary human workflows.

## The core knowledge concepts

| Concept | Meaning |
|---|---|
| **Raw evidence** | The preserved input received from a source before synthesis |
| **Source** | A citable publication, dataset, report, or record with publisher and provenance |
| **Observation** | A source-owned representation of what one source says about a subject |
| **Entity** | The canonical representation of a real subject in the domain |
| **Concept** | A reusable idea or analytical frame that can connect many entities and sources |
| **Claim / assessment** | An evidence-backed analytical statement with confidence and lifecycle state |
| **Prediction** | A falsifiable, time-bounded claim that can later be graded |
| **Finding** | A notable result produced by analysis or audit |
| **Briefing** | A time-oriented synthesis for a defined audience and decision cadence |
| **Dashboard** | A computed view over the graph, not a second system of record |
| **Knowledge gap** | A missing, stale, weakly supported, or structurally incomplete area worth investigating |
| **Receipt** | A machine-readable record of a run, its inputs, outcome, counts, and failures |

Not every domain uses every type, and packs can add domain types. The engine owns a
small universal core; pack schemas add types such as actor, vulnerability, model,
vendor, product, incident, or competitor.

## Evidence, confidence, and review

OKEngine separates four questions that conventional wikis often blur:

1. **What did a source say?** Preserved as source or observation evidence.
2. **What subject does it belong to?** Handled by identity resolution, with
   ambiguity kept visible.
3. **What does the system assess?** Expressed as a claim or canonical field with
   provenance and confidence.
4. **Has the result cleared policy?** Expressed through review and verification
   state.

Source quality is policy, not model self-certification. A model may extract a
publisher or candidate grade, but authoritative-source tiers and automatic
verification rules are human-maintained. Low-confidence assessments can still be
shown as suspected or provisional when policy allows; uncertainty should be
communicated, not erased.

Typical states include:

- **candidate / needs review** — useful but not yet cleared;
- **auto-verified** — deterministic evidence policy was satisfied;
- **human-verified** — an authorized reviewer accepted it;
- **quarantined** — retained but excluded from canonical projections;
- **superseded / tombstoned** — historical, replaced, or withdrawn without
  destroying the audit trail.

## Deterministic work and model work

| Prefer deterministic code for | Use a model for |
|---|---|
| Fetching and parsing structured APIs | Interpreting unstructured prose |
| Schema and invariant validation | Classifying domain relevance |
| Exact identifiers and bounded joins | Proposing semantic relationships |
| Indexes, backlinks, counts, and receipts | Synthesizing an analytical narrative |
| Known transformations and migrations | Extracting candidate claims under uncertainty |
| Health checks and queue management | Critiquing or comparing evidence |

This division controls cost and reduces failure modes. Model output is treated as
untrusted proposed content until it passes structural and policy gates.

## Packs and composition

A pack is a reusable **definition**, not a populated intelligence database. It
usually contains:

- `pack.yaml` ownership and dependency metadata;
- `schema.yaml` domain types, fields, merge rules, and source policy;
- a persona and bounded prompts;
- feed and importer definitions;
- scheduled domain operations;
- conformance fixtures and validation rules;
- optional empty scaffolding or example content.

Packs compose only when ownership is compatible. Type, namespace, cron, alias, and
dependency conflicts fail before deployment. The active schema is conceptually:

```text
engine core + installed pack schemas + enabled extension schemas
```

A bundle is a tested composition recipe. It owns no duplicate domain model; it
selects compatible packs and establishes the installation shape.

## Current public pack catalog

This table is a human-readable view of the machine-readable
`okpacks-library/catalog.json`. The catalog remains the authority for names,
status, source location, and engine pins.

| Pack | Purpose | Role |
|---|---|---|
| `okpack-example` | Minimal generic starter showing the supported pack shape | Example |
| `okpack-competitive` | Competitors, products, markets, segments, deals, and strategic signals | Flagship generic pack |
| `okpack-ai-research` | Models, labs, methods, benchmarks, papers, and predictions | Domain pack |
| `okpack-vendor-risk` | Vendors, products, components, contracts, incidents, dependencies, and risk ranking | Domain pack |
| `okpack-cti` | Installs the complete STIX-aligned cyber-threat-intelligence set below | Bundle |
| `okpack-threat-actors` | Actors, campaigns, malware, tools, techniques, aliases, and attribution assessments | CTI component |
| `okpack-vuln` | Vulnerabilities and actively exploited CVEs | CTI component |
| `okpack-threat-landscape` | Cross-report metrics, publishers, themes, and landscape trends | CTI component |
| `okpack-indicators` | Atomic indicators and adversary infrastructure | CTI component |
| `okpack-detections` | Sigma detections, ATT&CK mitigations, and technique coverage | CTI component |
| `okpack-incidents` | Security incidents and involved identities | CTI component |

### How OKCTI is assembled

**OKCTI** is a deployment of OKEngine using the `okpack-cti` bundle, operational
source configuration, selected extensions, and private/local operators. The bundle
composes the six CTI component packs into one graph. A source about a campaign can
therefore inform an actor, vulnerability, indicator, detection, incident, and
landscape view without creating six disconnected knowledge bases.

The deployment's generated wiki is not part of the public pack. Its enabled feeds,
private evidence, source policy, model choices, review decisions, and accumulated
knowledge belong to that deployment.

## Extensions, operators, and applications

### Extensions

Extensions are optional operations over the graph. The engine currently supplies
families for:

- relevance and scope gating;
- duplicate and semantic-neighbor discovery;
- grounding, contradiction, completeness, and critique;
- event ledgers, timelines, and visual maps;
- predictions and re-evaluation;
- gaps, frontier signals, and messaging synthesis;
- actor or target-relative risk ranking.

Extensions should use stable read and governed write surfaces and declare what they
read, write, and require. They add capability without changing domain identity.

### Operators

Operators integrate deployment-local systems that do not belong in a public pack:
private databases, licensed sources, internal APIs, or organization-specific
workflows. They must preserve the true publisher, write bounded evidence, record
receipts, fail closed on malformed responses, and avoid leaking endpoints or
credentials into exported knowledge.

### Applications

Applications turn reusable graph operations into an outcome-oriented workflow.
Examples include continuous hypothesis management and threat-informed detection.
They are product experiences over the same governed knowledge, not separate data
silos.

## Trust and governance model

OKEngine is designed around visible controls rather than implicit trust:

- **Provenance:** important claims link back to evidence.
- **Schema:** pages have declared types and validated fields.
- **Ownership:** engine, pack, extension, and operator boundaries are explicit.
- **Review:** uncertainty and verification state remain visible.
- **Non-destructive history:** tombstones and supersession preserve auditability.
- **Fail-closed writes:** malformed or ambiguous existing content is not casually
  rewritten.
- **Receipts and observability:** scheduled work records outcomes and failures.
- **Budget controls:** deterministic and wake-gated jobs avoid unnecessary model
  calls.
- **Deployment control:** operators choose sources, models, secrets, schedules,
  and exposure.

This is decision-support infrastructure. High-impact conclusions still require
review appropriate to their domain and consequence.

## Operating model

| Cadence | Typical work |
|---|---|
| Continuous / frequent | Feed collection, relevance filtering, raw processing, indexes |
| Daily | Briefing, changed-entity review, broken-link and queue repair |
| Weekly | Full source reconciliation, gap analysis, prediction review, health audits |
| Event-driven | New authoritative evidence, identity conflict, failed invariant, human review |
| Release-time | Pack composition, schema validation, migrations, conformance, deployment smoke tests |

Fleet health answers whether the machinery ran. Knowledge-quality views answer
whether the graph is supported, current, complete, and decision-useful. Both are
necessary; operational green alone does not prove analytical quality.

### Merge is not fleet deployment

An engine change merged to private CI is available to every pack, but it does not
automatically rebuild or restart every deployed pack. Each deployment records an
engine commit in `.hermes-data/engine-runtime.yaml`; fleet governance must compare
that marker with the approved private CI commit, roll the same engine through every
enabled pack, and run post-deploy verification per pack. “Merged,” “deployed to one
pack,” and “deployed fleet-wide” are three different states and must be reported
separately.

The checkout path used to build a deployment is an operator-controlled input. A
production rollout should resolve `ENGINE_DIR`, verify its remote, commit and dirty
state, and refuse an unapproved checkout before building. Repository location is
not a substitute for commit provenance; the recorded commit is the durable fleet
comparison key.

## What OKEngine is not

- It is not a model and does not depend on one model vendor.
- It is not a conventional RAG chatbot; retrieval is layered over durable memory.
- It is not an ungoverned autonomous writer.
- It is not a single cyber product; cyber intelligence is one pack composition.
- It is not a guarantee that every generated assessment is true.
- It is not a replacement for source licensing, security controls, or accountable
  human judgment.

## Measures of success

An effective deployment should improve:

- time from new evidence to an updated decision surface;
- evidence coverage and citation integrity;
- percentage of canonical entities with current, reviewed assessments;
- queue convergence and repair latency;
- prediction calibration;
- knowledge-gap closure;
- duplicate and broken-reference rates;
- model cost per useful update;
- reuse of compiled knowledge across people, briefs, and agents.

## Authoritative references

| Need | Reference |
|---|---|
| Short product and architecture overview | [`overview.md`](overview.md) |
| Engine versus pack ownership | [`engine-domain-boundary.md`](engine-domain-boundary.md) |
| Universal and pack-defined types | [`core-types-and-extensions.md`](core-types-and-extensions.md) |
| Pack composition | [`design/composable-okpacks.md`](design/composable-okpacks.md) |
| Extension architecture | [`design/extension-system.md`](design/extension-system.md) |
| Application patterns | [`application-catalog.md`](application-catalog.md) |
| Human review | [`human-review.md`](human-review.md) |
| Model write constraints | [`model-write-contracts.md`](model-write-contracts.md) |
| Audit and repair | [`audit-and-repair.md`](audit-and-repair.md) |
| Testing and invariants | [`testing-and-audit.md`](testing-and-audit.md) |
| Cost and model budgeting | [`operating-cost.md`](operating-cost.md) |
| Machine-readable engine boundary | [`../engine-manifest.yaml`](../engine-manifest.yaml) |
| Public packs and machine-readable catalog | `okpacks-library/README.md` and `catalog.json` |

## Keeping this guide current

This document explains the stable system model. When a release changes a
load-bearing boundary—engine versus pack ownership, composition, review semantics,
write governance, or delivery surfaces—it must update this guide.

The pack list above must be checked against `okpacks-library/catalog.json` whenever
the catalog changes. Pack-specific implementation detail belongs in the pack
README and manifests; this guide should retain the executive purpose and system
relationship rather than duplicating every field or cron.
