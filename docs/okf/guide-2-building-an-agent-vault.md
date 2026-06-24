# Building an Agent-Maintained Knowledge Vault

Domain-agnostic. Security is the first concrete worked domain; the reference pack is **okpack-sec** (a security-focused LLM-wiki pack, maintained in its own repo).

This guide describes how to build a knowledge vault that an LLM agent maintains autonomously: a directory of markdown files the agent reads, writes, links, and lints over time so that knowledge **compounds** instead of being rediscovered per query. It is the pattern-level companion to this repo's actual engine; a "Reference implementation" callout at the end of each section maps the pattern to the real components, so the guide doubles as build documentation for the engine.

RFC 2119 keywords (MUST / SHOULD / MAY) are used normatively.

---

## 1. Philosophy

### The problem with query-only AI

Most LLM deployments are oracles: an analyst asks, the model answers, the session ends, and everything discovered is forgotten. Every session starts from zero. The model has no memory of yesterday's conclusions and no accumulated model of *this* environment. As Karpathy put it: *"The LLM is rediscovering knowledge from scratch on every question. There's no accumulation. Nothing is built up."*

### The compiled-knowledge approach

Treat the LLM as a **reasoning engine that writes**. When the agent discovers something meaningful — a new entity, a corroborated link, a falsifiable prediction — it writes that *conclusion* to a persistent, structured file with its provenance and confidence. The next session begins with that knowledge already compiled. Investigations compound; the vault gets smarter.

Three layers (the "Karpathy stack"):

| Layer | Role | Mutability |
|---|---|---|
| `raw/` | immutable source dumps (the corpus) | never edited |
| `wiki/` | agent-maintained compiled knowledge | the agent's workspace |
| `schema.yaml` + persona `CLAUDE.md` | the contract + the domain voice | human-owned |

### Why not RAG

RAG embeds document chunks and retrieves semantically similar fragments at query time. It is the wrong tool here, for three reasons:

1. **Most queries are exact-identifier lookups**, not semantic searches. "All pages mentioning vendor X / `CVE-2024-12345` / entity `acme-corp`" is string matching, not cosine similarity. Semantic search adds noise.
2. **RAG requires continuous index maintenance.** Every write re-embeds. At scale with continuous agent writes this is a standing infrastructure cost.
3. **RAG retrieves raw material and re-derives conclusions on every query.** The vault stores the *compiled conclusion* directly — the agent does not re-read the source to know what it already concluded; that conclusion is written, with provenance and confidence.

The vault therefore uses **lexical + hybrid search over the markdown itself** (see §7), not a vector store as the primary retrieval path.

> **Reference implementation.** The engine is a pinned Hermes-Agent (v0.17.0, consumed as a dependency — not forked) + the `cron-plus` scheduler + LLM-wiki governance/maintenance machinery with an OKF-compatible validation floor, driving a fleet of autonomous cron jobs. The three-layer stack is literal: `raw/` (immutable, gitignored, durable storage) → `wiki/` (agent-maintained) → `schema.yaml` + the vault's persona `CLAUDE.md`. No RAG; search is qmd-hybrid + IWE + ripgrep (§7).

---

## 2. Vault structure & namespaces

### Layout

```text
wiki/
├── BUNDLE.md        ← vault manifest + config
├── INDEX.md         ← top-level namespace catalog
├── log.md           ← append-only audit trail
├── HEALTH.md        ← overwritten by the lint engine
├── AGENTS.md        ← behavioral contract for agents
├── HOT.md           ← derived "load-first" working set (optional)
│
├── sources/         ← one page per ingested source
│   └── INDEX.md
├── entities/        ← the things the domain is about (vendors, people, orgs…)
│   └── INDEX.md
├── concepts/        ← segments, patterns, recurring abstractions
│   └── INDEX.md
├── predictions/     ← falsifiable, dated forecasts
│   └── INDEX.md
└── dashboards/      ← derived views (Dataview-style)
    └── INDEX.md
```

A **namespace** is a top-level directory holding one category of entities. Each namespace has its own `INDEX.md`, its own write-permission row (§4), and its own lint cadence (§6). Generic example namespaces used throughout: `sources`, `entities`, `concepts`, `predictions`, `dashboards`, plus the `tombstone` pseudo-type (§5).

### One entity, one file

Every entity is exactly **one** markdown file: YAML frontmatter (structured metadata) + a markdown body (narrative, evidence, notes). The filename SHOULD be the entity `id`.

### The 500-entry rule

No `INDEX.md` MUST contain more than **500** entries, and no leaf directory SHOULD exceed 500 files. An agent must be able to read a full index in one context pass; a 50,000-row index is unreadable and defeats the hierarchy. When a directory exceeds 500, it MUST be partitioned into sub-directories, each with its own `INDEX.md`, by one of:

| Strategy | Bucket key | Re-shard when oversized |
|---|---|---|
| `by-type` | the page's `type` | add a first-letter level |
| `by-date` | `{year}/{month}` | split oversized month → `{year}/{month}/{day}/` |
| `by-letter` | first letter of slug | split → second letter |
| `flat` | none | paginate `INDEX-pNN.md` |

> **Reference implementation.** `build_index_tree` builds the hierarchical INDEX; `reshard_oversized` splits leaves past 500 (by-date→day, by-letter→2nd-letter); `reshelve` re-files flat pages into the declared hierarchy *link-preservingly* via the shared `okf_migrate` lib (it rewrites `[[wikilinks]]` so nothing breaks). Partitioning is declared per namespace in `schema.yaml` `partitioning:` — the engine reads it instead of hardcoding layout, which is what makes the *same* engine serve a `by-date` sources tree and a `by-type` entities tree in the same vault. A pack might, for example, declare `entities: by-type` (with `sharded_types: [...]`), `sources: by-date`, `concepts: by-letter`, `predictions: flat`.

---

## 3. Schema & frontmatter

### OKF base

The Open Knowledge Format base requires **one** mandatory field on every page: `type`. Everything else is a discipline layer the pack chooses. A vault is OKF-conformant if every entity file declares a `type`. Reserved structural files (`index.md`, `log.md`, `agents.md`) are exempt from the `type` requirement.

### Universal field set (generic)

These SHOULD appear on every entity; they form the interoperability surface.

```yaml
type:              # MUST. The entity type.
id:                # SHOULD. Globally unique, e.g. {type}-{slug}.
title:             # SHOULD. Human-readable name.
status:            # SHOULD. Type-specific enum (active | draft | retired | tombstoned | …).
created:           # SHOULD. ISO-8601 first-created timestamp.
updated:           # SHOULD. ISO-8601 last-modified timestamp.
version:           # MAY. Integer, increments on every write, starts at 1.
created_by:        # MAY. agent | bootstrap | human:{name} | import:{source}.
last_modified_by:  # MAY. Same value space.
confidence:        # SHOULD. See the confidence model below.
tags:              # MAY. Free classification tags.
```

### Confidence model (agent-settable vs human-only)

Confidence is the agent's epistemic certainty about the **entity itself** (distinct from source trust, below). The load-bearing rule: **agents write suspicions and inferences; humans confirm or refute.**

| Value | Meaning | Who may set |
|---|---|---|
| `suspected` | hypothesis from a pattern/heuristic; uncorroborated | Agent |
| `inferred` | supported by corroborating evidence | Agent (with documented reasoning) |
| `confirmed` | validated by an authoritative source or a human | **Human only** |
| `false-positive` | determined incorrect/benign | **Human only** |
| `under-review` | flagged for human review; indeterminate | Agent (enters review queue) |

A write that sets a human-only value from the agent path MUST be rejected.

### Source trust (one option: Admiralty-style)

For domains where source quality matters, rate each **source** on two independent axes (NATO Admiralty Code): **reliability** of the source and **credibility** of the specific claim. This is offered as *one* trust model, not mandatory; a pack MAY use a simpler scalar or none.

| Reliability (A–F) | Credibility (1–6) |
|---|---|
| A completely reliable … F cannot be judged | 1 confirmed … 6 cannot be judged |

**Derivation rule.** When the agent synthesizes a page from multiple sources, it MUST adopt the most conservative composite: lowest reliability letter, highest credibility number, with the rationale documented.

### Typed relationships

Links between entities SHOULD be typed, not bare ID lists — "*observed* X" differs from "*suspected* X". Typed links let the agent reason about connection quality.

```yaml
related:
  - id: entity-acme-corp
    relationship: competes-with     # the relationship type
    confidence: inferred            # confidence in THIS link
    first_seen: 2026-06-08
```

A pack MAY declare allowed `(from-type, to-type) → relationships` pairs. At minimum, bare `[[wikilinks]]` in the body are graph-traversable (§7).

### Per-type required fields via `schema.yaml`

The pack layers per-type required-field lists on top of the OKF base. Keep required lists conservative (only load-bearing fields with high live coverage) so the write-guard never rejects a legitimately-shaped page.

```yaml
# schema.yaml — the contract
version: 1
apply_under: [wiki/]
okf: {required: [type]}            # OKF base
# strict_types is ENGINE-OWNED (engine base-schema; default false = unknown types
# allowed if they satisfy okf.required). Not pack-settable, so it isn't set here.
types:
  source:     {required: [type, source_kind, publisher, published, reliability, credibility]}
  entity:     {required: [type, sources]}
  concept:    {required: [type, sources]}
  prediction: {required: [type, status, confidence, subject, resolves_by]}
  dashboard:  {required: [type, title]}
partitioning: { ... }              # §2
hot_set:      { ... }              # §6
```

> **Reference implementation.** The vault's `schema.yaml` *is* this contract — read by both the write-time guard and the pre-commit gate. OKF base = `{type}`; per-type `required` lists are the pack's discipline. The reference `prediction` type carries `status / confidence / subject / resolves_by` (falsifiable + dated); `source` carries `reliability / credibility` (Admiralty-style). The engine uses generic types (`source`, `entity`, `concept`, `prediction`, `dashboard`, `tombstone`) — there is **no** host/IOC/TTP catalog; those are a security pack's content, not the engine's. The confidence trust model is implemented as a **flag, not a gate**: a hard human-review gate is impractical at scale, so when an agent asserts a categorical confidence verdict (`schema.yaml` `review.confidence_review_values`: `confirmed` / `false-positive` / `refuted`) or sets a review field, the write **lands** and the page is stamped `needs_review: true`, appended to `wiki/_review-queue.md`, and logged — flagged for human review, never blocked. Numeric scores and `low`/`med`/`high` never flag; predictions use numeric confidence with the grade in `status:` (autonomous grading), untouched.

---

## 4. Structural files

| File | Role | Written by |
|---|---|---|
| **BUNDLE.md** | vault manifest: id, owner, version, defaults, entity count, last-lint timestamp | bootstrap + lint |
| **INDEX.md** (top) | catalog of namespaces (not individual files); ≤20 rows | agent on namespace change |
| **INDEX.md** (per-namespace) | catalog of entities in that namespace; ≤500 rows; filterable columns (status, date, summary) | agent / `build_index_tree` |
| **log.md** | append-only audit trail; one line per write | every write |
| **HEALTH.md** | *overwritten* each lint run; current health snapshot + action items | lint engine |
| **AGENTS.md** | the behavioral contract (§5); read at session start | human-owned |
| **HOT.md** | derived "load-first" working set (open predictions, recent sources, recently-updated entities) | `build_hot_set` |

**log.md format** (normative): `## [{ISO8601}] {action} | {entity-id} | {description}` where action ∈ `create | update | close | promote | flag | lint | ingest | tombstone`.

> **Reference implementation.** All seven exist. `wiki/AGENTS.md` is the authoritative permissions + review contract — it carries the write-permission matrix and review terms directly (the machine-readable matrix itself lives in `schema.yaml` `permissions:`). `HOT.md` is fully derived by `build_hot_set` from the `hot_set:` block in `schema.yaml` (open/active predictions within 30d, recent sources, recently-updated entities, cap 300).

---

## 5. Agent behavioral contract

`AGENTS.md` is the most important file in the vault: it is the contract that makes the vault trustworthy at scale. Any agent touching the vault MUST read it at session start.

### Session-start protocol

1. Read `AGENTS.md` (this contract).
2. Read `HEALTH.md` (current vault state + open action items).
3. Read top-level `INDEX.md` (namespace structure).
4. Read `HOT.md` / the hot-namespace indexes (load the working set).
5. Begin the task.

### Before-write protocol

1. **Dedupe-check** — search for an existing page covering this entity (exact-identifier search). If found, *update* it; never create a duplicate.
2. **Template** — use the correct schema template for the type.
3. **Validate** — all required fields present; confidence within the agent-settable set; source trust derived conservatively.
4. **Update INDEX** — add/refresh the namespace `INDEX.md` row.
5. **Append log** — one `log.md` line.

### Before-answer protocol

1. Traverse top-level `INDEX.md` → relevant namespaces.
2. Read the relevant namespace `INDEX.md`(s).
3. **Filter by frontmatter** before opening any file.
4. Open only the pages identified as relevant.
5. If the answer synthesizes 5+ pages, **write the synthesis back** (to `concepts/` or a working note) so the conclusion compounds.

### Write-permission matrix (per namespace)

Declared in the pack, *enforced* at the write layer:

| Namespace | Create | Update | Delete | Human-only gate |
|---|---|---|---|---|
| `sources/` | yes | yes | tombstone | — |
| `entities/` | yes | yes | tombstone | — |
| `concepts/` | yes | yes | tombstone | — |
| `predictions/` | yes | yes (status/dates) | tombstone | **grading = human-confirmed** |
| `dashboards/` | derived only | derived only | no | — |
| `working/` | yes | yes | yes | — |

The "human-only gate" column is the generic analogue of "humans confirm, agents suspect": e.g. *grading a prediction's outcome* is a human-confirmed action, mirroring a security pack's findings/ = human-only namespace.

> **Reference implementation.** The namespace write-permission **matrix** is declared in `schema.yaml` `permissions:` (default `create/update: true`, **`delete: false` everywhere → tombstone, not hard-delete**; a namespace may set `create/update: false` to mark it human-authored), exposed via `tools.schema_validator.governing_policy` (walk-up), and enforced in the `okengine-write` MCP server. The "human-only gate" column is a **flag, not a hard gate** — there is no blocking human-approval step; the trust model flags categorical verdicts for review (see §3) rather than rejecting them. Enforcement rides on the MCP write tools plus the existing cron toolset scoping (`enabled_toolsets` per job) and the failure-path toolset guard (the script-failure agent is report-only — its `terminal`/`file`/`code_execution` tools are stripped so a failed data-collection script can't trigger destructive "fixes").

---

## 6. Conformance, validation & maintenance

### The write-guard

A schema validator MUST run **before** any write lands. It discovers the governing `schema.yaml` by **walking up** from the target file (like `.editorconfig`), then checks OKF base (`type`) + the per-type required fields. It is **fail-open on unexpected errors** (a broken schema.yaml must never brick writes) but **fail-closed on an actual violation** (returns a reason → the write is rejected). No `schema.yaml` in the ancestry → validation is simply off for that tree — which is exactly what makes the engine multi-domain: drop a `schema.yaml` in any vault root and conformance turns on for that vault only.

### Field-loss guard

A write that would *drop* curated frontmatter from an existing page MUST be blocked. Agents re-emit pages; a regression that silently strips a hand-set field is a data-loss bug, not an edit.

### Pre-commit gate

The same validator runs as a pre-commit gate over changed files, so non-conformant pages never enter version control.

### Tombstone-on-delete

Entities are **never hard-deleted**. A "deletion" converts the page to a `tombstone`: `status: tombstoned`, a `tombstone_reason` (`duplicate | expired | out-of-scope | false-positive`), `superseded_by`, and the original type/description. This preserves inbound-link integrity and history.

```yaml
type: tombstone
id: entity-acme-old
status: tombstoned
tombstone_reason: duplicate
superseded_by: entity-acme-corp
original_type: entity
```

### Rolling / incremental lint (never full-scans)

A single full scan of a large vault is expensive and unnecessary. The lint engine runs on a **rolling schedule**, distributing work across time:

| Cadence | Scope |
|---|---|
| Daily | files modified in the last 24h; time-sensitive checks (review-due, stale open items) |
| Weekly | cross-reference / broken-link validation; orphan detection (zero inbound links) |
| Monthly | deep schema sweeps; archival/decay passes |

Every lint run **overwrites `HEALTH.md`** with the current snapshot + action items, and **appends one line to `log.md`**.

> **Reference implementation.** `tools/schema_validator.py` is the write-guard (returns `schema_reject_reason`), the pre-commit gate, **and** the rolling `schema-drift-lint` cron (whole-vault via the same walk-up). The field-loss guard blocks writes that drop curated frontmatter (an edit may not drop an existing frontmatter key); the `okengine-write` MCP server adds **body-preserving surgical edits** — `patch_entity(path, old_string, new_string)` (exact one-shot find/replace, must match once) and `append_to_section(path, heading, text)` (append into a `## heading` block) — plus a reserved-file guard that refuses engine-managed structural files. A fleet of rolling *drains* keeps the vault clean incrementally — `broken-wikilinks-drain`, `orphans-drain`, `repair-yaml-propose`/`-apply`, `repair-broken-frontmatter`, `normalize-entity-schema`, `sanitize-frontmatter-updated`, `schema-type-drain`, `schema-classify-drain` — each takes a bounded batch per tick rather than scanning everything. **Tombstone-on-delete:** `tombstone_entity` is the sanctioned removal (sets `status: tombstoned` + `superseded_by`, file retained), `permissions.default.delete: false`, and no drain hard-deletes a knowledge page (the only `unlink` sites are derived-artifact/temp cleanup).

---

## 7. Tooling: search without RAG, knowledge-graph, MCP

### Tiered search

| Tier | Mechanism | Use |
|---|---|---|
| Exact | **ripgrep** | identifier lookups (vendor name, CVE, exact ID) — sub-second across 100k files |
| Ranked | **BM25** (e.g. tantivy) | keyword-phrase queries |
| Hybrid | **BM25 + vector + rerank** | conceptual queries where lexical alone misses synonyms |

The hybrid tier uses a *local* embedding/rerank model over the markdown, indexed on a schedule — it is an accelerator, **not** a RAG pipeline (it returns whole compiled pages, not re-derived chunks).

### Knowledge graph

A markdown KG tool parses the vault's `[[wikilinks]]` for backlink traversal, orphan detection, subtree retrieval, and graph export — **read-only** for the agent (find/retrieve/tree/stats/export; refuses mutating ops).

### MCP server as the agent interface

The agent interacts through an **MCP server** rather than raw shell. Direct filesystem access burns context on `grep`/`find`, is error-prone, and *bypasses the contract*. The MCP server is where schema validation, the write-permission matrix, and log-append are enforced unbypassably:

- **Read tools:** `search` (exact + ranked), `get_page`, `find_references`, `list_*`.
- **Write tools:** `create_entity` (validate → write → version bump → update INDEX → append log), `update_entity`, `tombstone_entity`, `flag_for_review`.

> **Reference implementation.** Search = `qmd` (hybrid BM25+vector+rerank, wrapped by `kb_search.py`) + `IWE` (markdown KG, wrapped read-only by `kb_graph.py`) + ripgrep. No RAG, by design. `okengine-mcp` is the **read-only** MCP server (streamable-http + bearer auth) — `search` / `get_page` / `find_references` / `list_*`. `okengine-reader` is the human web reader over the same vault. The MCP **write path**: a separate local stdio server **`okengine-write`** (`okengine-mcp/write_server.py`, wired as `mcp_servers.okengine-write`) exposes `create_entity`/`update_entity`/`tombstone_entity`/`flag_for_review`/`patch_entity`/`append_to_section`, each schema-validated against the governing `schema.yaml` and log-appending; writer crons run on it, with Hermes' `file`-tool `schema_validator` kept as the shape backstop. A pre-built identifier→path manifest (Tier-A inverted index) is deliberately **not built** — ripgrep + qmd already serve exact + ranked lookup (optional).

---

## 8. Ingest & source classes

The agent must turn external material into compiled pages. Three source classes:

| Class | What it is | Pipeline |
|---|---|---|
| **Bundle** | a corpus drop (feeds, files, an export) | drop → classify → extract → dedup → write |
| **Query** | a live question to an external retrieval API | retrieve → ingest from *cited* material, not from synthesis |
| **Enrichment** | a backfill that fills gaps on existing pages | select underspecified pages → fetch → patch fields |

### raw → wiki pipeline

```
feeds (OPML) → fetch → raw/ (immutable)
            → wake-gated ingest agent → sources/ → entities / concepts / predictions
                                         (relevance-triaged; off-topic dropped)
```

The agent reads from cited source material and writes compiled pages with provenance; it does **not** treat its own synthesis as a source.

### Bootstrap (cold-start)

Convert a heterogeneous document directory into a conformant vault:

1. **Drop-zone** — place all source documents in `/ingest/`.
2. **Classify** — route each file by type (structured / prose / visual) to an extractor.
3. **Extract** — structured files map fields directly; prose files use the LLM with a schema-enforcing prompt. All extracted entities start `confidence: inferred`, `created_by: bootstrap`.
4. **Dedup** — resolve duplicate entities across documents via canonical-key matching; merge, recording each source.
5. **Write** — emit pages, generate every `INDEX.md`, write the structural files, produce a coverage-gap report.

> **Reference implementation.** Implemented source class: **feeds**. An OPML feed list drives the generic `feed_fetch.py` → `raw/` → a wake-gated ingest agent → `sources/` → relevance-triaged `entities`/`concepts`/`predictions`. Trust: sources carry `reliability`/`credibility`; predictions are falsifiable + dated + carry `confidence`. The Query class is realized through a domain intelligence-query wrapper (ingest from cited articles, never from the synthesis itself); Enrichment is the family of backfill drains (`entity-backfill`, `concept-backfill`, `page-quality-enrich`, vendor-frontmatter backfill, …).

---

## 9. Deployment: engine / pack split

A deployment = **a versioned engine + one pinned domain pack**. The engine is domain-agnostic runtime + KB machinery; the pack is everything domain-specific.

```
engine @ vX.Y.Z   +   <your pack>   →   a live second brain
```

**What's in the pack** (one version-controllable directory):

- `schema.yaml` — the contract (types, partitioning, hot-set)
- `wiki/` — the vault content
- persona `CLAUDE.md` — the domain voice + curation rules
- feeds (OPML), data, the domain crons, `.env`

**What's in the engine:**

- the LLM runtime + scheduler
- the LLM-wiki governance stack (validator/write-guard, drains, plus the OKF `type` floor)
- structure machinery (index-tree, reshard, reshelve/migrate, hot-set, health)
- the search/graph tooling and the MCP query surface

**Schema-as-contract.** The engine hardcodes no layout, no namespaces, no types — it reads `schema.yaml` (walk-up). Swap the pack, get a different second brain on the same engine.

**Cron tiers.** Jobs are classified `engine` (domain-agnostic machinery, ships unchanged), `engine-template` (generic mechanism / wake-gate script ships with the engine; the prompt comes from the pack), and `domain` (pure pack content). This is the seam that lets the engine ship its half of the fleet and the pack supply the rest.

**Scaffolding.** A new pack is bootstrapped, then the engine version is pinned.

> **Reference implementation.** The engine/pack boundary is declared in `engine-manifest.yaml` (the pinned Hermes-Agent stays at the repo root for clean upstream tracking, so the manifest *is* the logical boundary). Cron classification lives in `config/cron-tiers.yaml` (engine / engine-template / domain). The branded `framework` CLI scaffolds a new pack — `python scripts/framework.py init` (which calls `framework_init.py`) — with `framework validate` (`scripts/framework_validate.py`) as the pre-deploy pack check; the engine is versioned (`v0.2.0`) and a pack pins `engine.version`. Operator quickstart: `docs/deploy-a-new-domain.md`. The reference pack is **okpack-sec** (a security-focused LLM-wiki pack, maintained in its own repo).

### Scheduler mechanics (cron-plus)

The autonomous loop runs on `cron-plus`: subprocess-per-job (true parallelism), each job with its own model/provider and `enabled_toolsets`. Two patterns matter:

- **`no_agent: true`** — a pure-script job that writes its output as a side effect and never needs the LLM. The script *is* the job; no agent is constructed (no prompt, no tool loop, no token spend).
- **wake-gate** — a script emits `{"wakeAgent": true|false}`; on `true` the agent runs the real work, on `false` the tick is silent. This keeps the LLM off the critical path until there's actually something to do.

> **Reference implementation.** A fleet of cron-plus jobs; pure-script jobs carry `no_agent: true`; the rest are wake-gated (script gate → agent works) or always-on agent jobs. The engine *exceeds* this guide on deployability (engine/pack split, the `framework` CLI, versioned engine, a public reference pack — okpack-sec, a separate repo — plus multi-domain-in-one-vault via walk-up schema), on search (qmd hybrid + IWE + ripgrep), and on the conformance stack (walk-up multi-domain validator + rolling drains). The conformance machinery covers the full pattern:

| Item | Engine mechanism |
|---|---|
| MCP as the enforced **write** path | `okengine-write`: `create_entity`/`update_entity`/`tombstone_entity`/`flag_for_review`/`patch_entity`/`append_to_section` |
| `wiki/AGENTS.md` permissions contract + namespace **write-permission matrix** | `schema.yaml` `permissions:` |
| **Confidence trust model** (flag-not-gate) + **tombstone-on-delete** | `tombstone_entity` |
| **Hot/warm/cold tiers** | derived (`tier_lib.tier_of`) + `tier-refresh` cron + `--tier` filter |
| Pre-built identifier→path manifest (Tier-A inverted index) | optional — ripgrep + qmd already serve it |

None are domain-specific; each lands in the engine and every pack inherits it.

---

## References

| Resource | URL |
|---|---|
| Karpathy LLM Wiki gist (origin of the pattern) | https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f |
| IWE — markdown knowledge graph | https://github.com/iwe-org/iwe |
| qmd — local hybrid search | https://github.com/tobi/qmd |
| MCP specification | https://modelcontextprotocol.io/ |
| RFC 2119 (MUST/SHOULD/MAY) | https://www.rfc-editor.org/rfc/rfc2119 |
| Deploy a new domain (operator quickstart) | `docs/deploy-a-new-domain.md` |
