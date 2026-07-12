# The Agent + LLM-Wiki Pattern: Architecture Briefing

*Domain-agnostic; generalized from the security reference briefing; reflects the engine as of 2026-06-15.*

**Purpose:** Describe the general architecture for an autonomous agent backed by an LLM-maintained wiki — applicable to any knowledge domain — and explain how the Open Knowledge Format (OKF) fits as a minimal portability layer. Security threat-hunting and intelligence are referenced as the first concrete worked domain. The final section maps every concept to a working reference implementation.

---

## Executive Summary

A common starting point for "AI over a corpus" is a local or hosted LLM that answers queries against ingested data. This is useful but fundamentally **passive and reactive** — the system only knows what it is asked. The opportunity is to evolve it into an **autonomous agent architecture** that proactively investigates, accumulates knowledge over time, and surfaces things no human thought to query.

The key enabler is a **persistent, agent-maintained wiki** that serves as the agent's long-term memory. This pattern was articulated by Andrej Karpathy in early 2026; OKEngine turns it into a reusable engine for swappable-topic LLM wikis. Google's **Open Knowledge Format (OKF)** later provided useful validation and a tiny markdown + YAML portability floor. The pattern is domain-agnostic: the same engine that maintains a wiki of network assets and threats can, with a different schema and persona, maintain a wiki of market entities, scientific concepts, legal precedents, or product telemetry.

---

## 1. The Problem With Query-Only AI

A query-response loop against an LLM is using a sophisticated tool as a **passive oracle**. The user must already suspect something to ask. This caps the system's value at the user's imagination — and the most valuable insights are often the ones nobody knew to look for.

| Limitation | Operational Impact |
|---|---|
| Reactive, not proactive | Coverage is bounded by user intuition |
| No persistent memory | Each session starts from zero; no accumulated context |
| No autonomous action | The model cannot act on what it discovers |
| Human bottleneck | Scales only as fast as people can formulate questions |
| No cross-session correlation | Patterns spanning days or weeks are invisible |

The shift from a query model to an **agent model** changes the paradigm from *answering questions* to *autonomously pursuing goals*.

---

## 2. What an Agent Architecture Adds

An agent built on the same LLM operates in a continuous loop:

> **Observe → Reason → Act → Observe new state → Reason again**

This mirrors how a skilled human investigator works — following a thread across sources, pivoting on signals, building a case over time. The agent gains capabilities the query model cannot provide:

- **Autonomous hypothesis generation** — identifies patterns without being asked
- **Tool use** — queries external systems, pulls feeds, runs analyses, correlates across sources
- **Persistent threads** — work spans sessions without losing context
- **Proactive escalation** — surfaces high-confidence findings rather than waiting to be queried
- **Continuous coverage** — operates on a schedule, not only when a human is present

None of these are domain-specific. The loop is identical whether the agent is hunting intrusions, tracking vendor moves, or curating a research literature graph; only the *tools* and the *schema* of what it writes differ.

---

## 3. The Wiki as Agent Memory

The agent's most critical component is its **long-term memory** — a persistent store of what it knows, what it has investigated, and what it has concluded. A wiki is the right substrate for this.

### Why a Wiki, Not a Vector Database (RAG)

The dominant approach to giving LLMs access to large document collections is **Retrieval-Augmented Generation (RAG)**: documents are chunked, embedded, and retrieved by semantic similarity at query time. RAG has structural drawbacks for an accumulating agent:

- The LLM **re-derives knowledge from scratch** on every query — there is no accumulation
- Keeping embeddings synchronized with a changing corpus is an ongoing maintenance burden
- RAG retrieves *raw source material*; what an agent needs is **compiled, synthesized understanding** already reasoned over
- Chunking destroys the coherence of structured content (entity records, investigation threads, relationship graphs)

The wiki stores *conclusions, relationships, and context* — not raw inputs. The agent writes once and reads many times. Knowledge **compounds** with every cycle. RAG is explicitly rejected for the memory layer.

### What the Wiki Stores

A single wiki serves several memory functions simultaneously. The labels below are generic; the *examples* are illustrative across domains:

| Memory Type | Contents | Examples (any domain) |
|---|---|---|
| **Declarative** | What the agent knows about the world | Entity inventory, topology, baselines, taxonomies |
| **Episodic** | What the agent has done and found | Investigation logs, prior findings, ruled-out hypotheses |
| **Procedural** | How the agent should respond to patterns | Runbooks, playbooks, escalation criteria |
| **Intelligence** | External context | Source library, actor/competitor profiles, signal catalogs |

### The Karpathy Pattern

Karpathy articulated this architecture in a widely circulated 2026 note:

> *"Instead of just retrieving from raw documents at query time, the LLM incrementally builds and maintains a persistent wiki — a structured, interlinked collection of markdown files... The knowledge is compiled once and then kept current, not re-derived on every query."*

> *"LLMs don't get bored, don't forget to update a cross-reference, and can touch 15 files in one pass. The wiki stays maintained because the cost of maintenance is near zero."*

The architecture has **three layers**:

1. **Immutable raw sources** — the source data (logs, feeds, filings, papers, telemetry). The agent reads but **never modifies** these.
2. **The agent-maintained wiki** — markdown files the agent writes: summaries, entity pages, threads, findings, dashboards.
3. **The schema / contract** — the machine-readable schema *plus* the persona/behavioral contract (an `AGENTS.md`/`CLAUDE.md` equivalent) that defines how the agent reads, writes, dedupes, and maintains the wiki.

The first and third layers are the durability and governance boundaries; the middle layer is where compounding value lives.

---

## 4. OKF as a compatibility floor

The **Open Knowledge Format (OKF)** is the minimal markdown + YAML convention OKEngine supports for interoperability: one mandatory field (`type`) and portable markdown pages. OKEngine's real contract is broader because it must operate and maintain a live wiki: nearest `schema.yaml`, pack-owned types, enforced writes, tombstones, review flags, tiers, repair drains, and graph tooling.

### What OKF Is

OKF is intentionally minimal. An OKF bundle is:

- A **directory of markdown files**, one file per concept or entity
- Each file carries a small block of **YAML frontmatter** with structured fields
- Files link to each other with standard markdown links, forming a **knowledge graph**
- Two reserved files: `index.md` (navigable catalog) and `log.md` (append-only change history)

The **only mandatory field** in OKF v0.1 is `type`. Everything else is left to the producer. The spec defines the *interoperability surface*, not the content model — which is precisely what makes it domain-agnostic.

```yaml
---
type: entity
title: Example Entity
description: One-line summary of what this page is
tags: [example, illustrative]
timestamp: 2026-06-10T14:22:00Z
---
```

### Why OKF Still Matters

- **Interoperability** — bundles can be consumed by any OKF-compatible agent or tool without translation
- **Tooling ecosystem** — producers, consumers, and search tools can converge on one shape
- **External validation** — OKF independently validates the same markdown+YAML direction that Karpathy's LLM-wiki pattern sparked
- **Consistency** — a single conventional shape keeps the corpus uniform as it grows

### Structural Conventions

| Convention | Purpose |
|---|---|
| `index.md` | Navigable catalog; agent reads this first to find relevant pages |
| `log.md` | Append-only chronological record of agent actions and ingests |
| YAML frontmatter on every file | Enables filtering without reading full content |
| Markdown links between files | Turns the directory into a traversable knowledge graph |
| One concept per file | Keeps files bounded and individually retrievable |

---

## 5. Scaling to 100,000 Files (Overview)

The Karpathy pattern works at hundreds of pages with a flat index. At enterprise scale — 100k files — several structural adaptations are required. The full mechanics are domain-independent; the summary:

- **Hierarchical index tree.** Replace the flat `index.md` with a *tree* of indexes: a top-level index of namespaces, then per-namespace `INDEX.md`, navigated top-down. No index file exceeds ~500 entries; oversized buckets split into sub-buckets with pagination.
- **Namespace partitioning.** Group pages into namespaces (by entity type, by date, by first letter, or flat) so the agent can scope reads and writes. Partitioning strategy is per-namespace, not global.
- **Tiered working set.** Maintain a small **hot set** of pages to load first (active threads, recent entities, current signals) distinct from the warm/cold long tail. Promotion/demotion rules are declared in the schema, not hardcoded.

The point of all three is the same: keep what the agent must read on any given cycle **bounded and relevant**, regardless of total corpus size.

---

## 6. Lint as Proactive Discovery

Karpathy describes a **lint** operation — periodically asking the agent to health-check the wiki for contradictions, orphaned pages, stale claims, and missing cross-references. The insight that generalizes across domains: **the maintenance pass is also a discovery pass.** The same queries that keep the graph clean surface substantive findings.

Generic lint-as-discovery questions an agent can run on a schedule:

- Are there entities in the inventory with no recorded baseline/context? *(coverage gap)*
- Does any entity's current state contradict its recorded baseline? *(anomaly signal)*
- Are there things referenced in notes that never got their own page? *(orphaned knowledge)*
- Has a pattern appeared across multiple entities without a connecting thread? *(missed correlation)*
- Has something dormant for a long period suddenly become active? *(state-change signal)*

In a security domain these become threat-hunt and signal-detection queries; in a research domain they become gap-analysis queries. The *engine mechanism* is identical — schedule a lint, let the agent reconcile and report. Running lint on a cadence (or triggering it after N new ingests) is a natural fit for an autonomous loop and is where the architecture earns its keep.

---

## 7. Schema Extension: The Domain Contract

OKF v0.1 requires only `type`. A **domain extension** defines the entity types, the mandatory fields per type, and the link semantics relevant to that domain's knowledge graph. This is the one place the architecture is intentionally domain-specific — and it is **data, not code**.

A domain extension declares, at minimum:

- **Types** — the page types this domain uses (OKF mandates only `type`; the extension adds the rest)
- **Required fields per type** — what frontmatter must be present for a page of each type to be valid
- **Link semantics** — which frontmatter fields are typed references into the graph (and to what)
- **Conventions** — confidence/status vocabularies, identifier formats, partitioning and hot-set rules

```yaml
# Illustrative — a generic "entity" type in a domain extension
type: entity
required_fields: [id, title, status, last_updated]
link_fields:
  related_entities: entity
  sources: source
status_values: [active, retired, unknown]
confidence_values: [confirmed, inferred, suspected]
```

Because the extension is a declarative contract, the engine reads it rather than hardcoding any domain knowledge. Swapping the contract (plus the persona and the feeds) is what turns one engine into a different domain's second brain.

---

## 8. Search at Scale: Not RAG, Not Pure Grep

At 100k files, the hierarchical index handles *structured* navigation. For *unstructured* search across the full wiki — finding every page that mentions a specific identifier or concept — a lightweight local search tool is needed. This is **distinct from RAG**.

The distinction matters: RAG embeds chunks and retrieves by semantic similarity, re-deriving relevance per query. What's needed here is **exact and ranked text search over structured markdown** — closer to `grep` with ranking than to a vector database.

| Option | Approach | Best for |
|---|---|---|
| **Indexed manifest + ripgrep** | Pre-built inverted index of key identifiers → file paths | Exact identifier lookup |
| **BM25 full-text search** | Ranked keyword matching with partial matches | Broader keyword queries with ranking |
| **Hybrid (BM25 + light vector + rerank)** | Lexical first, vector for concept-level recall, reranked | Concept-level queries across narrative content |

For the majority of queries — lookups for specific identifiers — the first two options suffice and avoid all embedding infrastructure. Hybrid search is available when semantic recall over narrative content becomes necessary. The knowledge-*graph* (markdown links) is a separate, complementary access path: traverse references rather than search text.

---

## Reference implementation

The rest of this briefing is pattern. This section grounds it: the project this document lives in **is a working reference implementation** of the agent + LLM-wiki pattern, built to be domain-agnostic. The reference **domain pack** is **okpack-cti** (a security-focused LLM-wiki pack, maintained in its own repo) — the engine itself carries no domain knowledge.

### Engine vs. domain pack

The implementation splits cleanly into two layers:

| Layer | What it is | Domain-specific? |
|---|---|---|
| **Engine** | The runtime (a pinned Hermes-Agent — the exact pin is in `engine-manifest.yaml`, consumed as a dependency, not forked), the `cron-plus` scheduler, LLM-wiki governance/maintenance machinery, OKF-compatible validation, retrieval + graph tooling, and deploy tooling | No — fully domain-agnostic |
| **Domain pack** | Per deployment: `schema.yaml`, the content `wiki/`, the persona `CLAUDE.md`, feeds, data, cron job definitions + prompts, and `.env` | Yes — this *is* the domain |

The boundary is explicit and versioned:

- `engine-manifest.yaml` enumerates exactly which files constitute the engine layer.
- `cron-tiers.yaml` classifies every scheduled job as **engine** (domain-agnostic maintenance), **engine-template** (engine logic, per-pack prompt), or **domain** (pack-owned).
- The engine is versioned (see `engine-manifest.yaml` `engine_release`); a domain pack pins `engine.version`.
- `framework_init` scaffolds a fresh domain pack against the current engine.

This is the mechanism that makes the pattern *deployable* rather than a one-off: a quickstart (`docs/deploy-a-new-domain.md`) walks the same engine into a new domain by authoring only the pack.

### Concept → implementation map

| Pattern concept (this briefing) | Reference implementation |
|---|---|
| Autonomous agent loop | pinned Hermes-Agent (the pin is in `engine-manifest.yaml`, consumed as a dependency) running observe→reason→act with tool use |
| Runs continuously, not on demand | `cron-plus` scheduler — dozens of scheduled jobs maintain the wiki (a composed reference deployment measured ~90+; the engine-only floor is ~53 — see `config/engine-crons.json`) |
| Three Karpathy layers | immutable `raw/` → agent-maintained `wiki/` → `schema.yaml` + persona `CLAUDE.md` |
| The schema/contract (Layer 3) | `schema.yaml` — read by the engine, never hardcoded |
| OKF "only `type` is mandatory" | `schema.yaml` `types` declares page types + required fields; engine enforces OKF's `type` floor |
| Hierarchical index tree | `build_index_tree` — top INDEX of namespaces, per-namespace `INDEX.md`, 500-entry rule, auto-shard of oversized buckets, pagination |
| Namespace partitioning | `schema.yaml` `partitioning` — per-namespace by-letter / by-date / by-type / flat, with reshard thresholds |
| Tiered working set (hot set) | `schema.yaml` `hot_set` — the load-first working set |
| Reserved structural files | generated: `INDEX.md`, `log.md`, `AGENTS.md`, `BUNDLE.md`, `HEALTH.md`, `HOT.md` |
| Lint as proactive discovery | rolling maintenance **drains** + the `wake-gate` cron pattern (below) |
| Search without RAG | `qmd` (local hybrid BM25 + vector + rerank) + `IWE` (markdown knowledge-graph) + `ripgrep`; RAG explicitly rejected |
| Schema extension = domain contract | the domain pack's `schema.yaml` (one per deployment) |

### Multi-domain in one vault

The conformance validator (`tools/schema_validator.py`) **walks up** from any page to the nearest `schema.yaml`. A subtree can therefore carry its own `schema.yaml` and behave as its own domain — multiple domains can coexist in one vault, each validated against its own contract. A production deployment exercises this with a root domain and a related sub-domain living side by side in one vault.

### Conformance stack

OKF conformance is enforced, not assumed:

- **Write-guard** — `tools/schema_validator.py` validates writes against the nearest `schema.yaml`
- **Pre-commit gate** — blocks non-conformant pages before they land
- **Rolling `schema-drift-lint`** — a scheduled job that catches drift the write-guard missed
- **Field-loss guard** — detects edits that silently drop required/curated fields
- Plus many **rolling maintenance drains** (link repair, reshelving flat writes, frontmatter normalization, etc.) — these are the engine's instantiation of *lint-as-proactive-discovery*

### `cron-plus` execution model

- **Subprocess-per-job** — true parallel execution, isolation per job.
- **`no_agent` flag** — pure-script jobs skip the LLM entirely: the script *is* the job (no prompt, no tool loop, no token spend).
- **Wake-gate pattern** — a cheap script decides whether to wake the agent. The script gates; the agent only does expensive reasoning when there's actually work. This is how a fleet of dozens of jobs runs cheaply: most ticks are script-only.

### Filing and self-healing

- `build_index_tree` maintains the hierarchical INDEX described in §5.
- The **`reshelve`** drain re-files flat or misplaced writes into the correct namespace, **link-preserving**, so the agent can write loosely and the engine normalizes placement.
- Oversized buckets auto-shard against the `partitioning` thresholds.

### Consumption surfaces

- **`okengine-mcp`** — a **read-only** MCP query surface (`search`, `get_page`, `find_references`, `list_*`) so *other* agents can consume the corpus as a knowledge service.
- **`okengine-write`** — a separate local **stdio** MCP server (`okengine-mcp/write_server.py`, distinct from the read-only `okengine-mcp/server.py`) wired into the gateway as `mcp_servers.okengine-write`. It exposes 6 schema-validated write tools — `create_entity`, `update_entity`, `tombstone_entity`, `flag_for_review`, `patch_entity`, `append_to_section` — each validating against the governing `schema.yaml` before writing and appending a `log.md` line.
- **`okengine-reader`** — a human-facing web reader over the same vault.

### Deployability

The implementation is built to be re-deployed into new domains, not just operated as one:

- `framework_init` scaffolds a new domain pack.
- The engine is versioned (see `engine-manifest.yaml`); packs pin `engine.version`.
- Quickstart: `docs/deploy-a-new-domain.md`.
- **The reference pack** is **okpack-cti** (a security-focused LLM-wiki pack, maintained in its own repo). A production deployment further proves the same engine spans multiple domains by hosting a root domain plus a related sub-domain in one vault.

---

*This document generalizes the security reference briefing into the domain-agnostic LLM-wiki pattern and maps it to the engine in this repository.*
