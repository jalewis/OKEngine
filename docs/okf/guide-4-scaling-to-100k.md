# Guide 4 — Scaling an Agent-Maintained Wiki to 100,000 Files

Domain-agnostic scaling reference.

**Document Type:** Architecture / scaling reference
**Scope:** The structural, operational, and schema considerations for scaling a wiki-based agent memory to ~100,000 files, with an OKF-compatible portability floor. It covers the wiki layer only — not the broader agent orchestration. Each strategy carries an **In this engine** note describing how the Hermes-Agent + cron-plus + OKEngine stack actually implements it.

---

## Background: The Pattern We Are Scaling

The **LLM Wiki pattern**, articulated by Andrej Karpathy, describes a persistent,
agent-maintained knowledge base structured as a directory of markdown files with
YAML frontmatter. The agent reads from and writes to this wiki across sessions,
*compiling* knowledge rather than re-deriving it from raw sources on every query.
OKF is a later, minimal portability convention that fits this shape; OKEngine's
scaling concerns are driven by operating the live wiki, not by OKF itself.

At small scale (hundreds of files) the pattern works with a flat `index.md` and minimal tooling. At ~100,000 files several structural assumptions break down and require deliberate architectural decisions. The examples below use neutral object kinds — **entities** (the things the domain tracks), **sources** (ingested raw material), **concepts** (cross-cutting patterns/segments), and **predictions** (falsifiable, dated claims) — with security (hosts/IOCs/TTPs/investigations) as one illustrative instantiation. The strategies are domain-independent.

> **This engine is built for domain-agnostic scaling.** It performs link-preserving flat→hierarchical migration at scale (the IWE reference count holds constant across the move), and it can run **multiple domains in one vault** — a root domain and related sub-domains, each with its own `schema.yaml`, resolved by a walk-up validator.

---

## The Core Failure Modes at Scale

Being precise about what actually breaks at 100k files:

**Navigation blindness.** A flat `index.md` works because the agent reads the entire index in one context pass. At 100k files the index is itself a document the agent cannot fully consume — it loses the ability to know what exists before deciding what to open.

**Write inconsistency and duplication.** If the agent cannot verify what already exists before writing, it creates duplicate pages for the same entity. Duplicates and contradictions compound into a corrupted KB.

**Maintenance intractability.** Lint operations — contradictions, orphaned links, stale entries, schema drift — require scanning the wiki. A full scan at 100k files is not feasible in one agent pass and must be redesigned as a distributed, incremental workflow.

**Retrieval noise.** Even fast lexical search (`ripgrep`) over 100k files returns enough incidental matches to fill the context window with irrelevant results before the relevant ones surface.

Every strategy below addresses one or more of these.

---

## The Open Knowledge Format (OKF) Baseline

OKF v0.1 defines a portable, interoperable baseline that maps cleanly onto the
LLM-wiki pattern. An OKF bundle is:

- A **directory of markdown files**, one file per concept or entity
- Each file carries **YAML frontmatter** with a small set of structured fields
- Files link to each other with standard markdown links, forming a **knowledge graph**
- Reserved structural files: `index.md` (navigable catalog) and `log.md` (append-only history)

OKF requires exactly **one field**: `type`. Everything else is left to the producer. The spec defines the interoperability surface, not the content model.

```yaml
---
type: <string>          # REQUIRED. The only mandatory OKF field.
title: <string>         # Recommended. Human-readable name.
description: <string>   # Recommended. One-line summary.
tags: [<string>, ...]   # Optional. Free-form classification.
timestamp: <ISO 8601>   # Optional. Last meaningful update.
---
```

Keeping an OKF-compatible floor buys three things at scale: **tooling compatibility** (any OKF-aware producer/consumer/search tool can consume a projected bundle), **interoperability** (bundles from different producers can merge or query with minimal translation), and **schema discipline** (the frontmatter convention is what makes large-scale filtering, navigation, and lint tractable).

> **In this engine.** Every page is markdown + YAML, one file per entity, with the OKF `type` field mandatory. `schema.yaml` declares `okf.required: [type]`; a **walk-up validator** finds the nearest governing `schema.yaml` for any file (so multiple domains coexist in one vault, each with its own schema). The vault keeps `index.md`-equivalent structural files and an append-only `log.md`. This is the conformance baseline the rest of the stack builds on.

---

## Strategy 1 — Hierarchical INDEX Tree

### The problem
A flat index listing all 100k files is unreadable in a single pass. Even summarized, a 100k-entry index exceeds any practical context window.

### The solution
Replace the flat index with a **tree of indexes** the agent traverses top-down. The top-level INDEX lists **namespaces only** (10–20 entries). Each level has its own `INDEX.md` covering only its namespace and bounded in size. The agent reads the top INDEX (small), navigates to the relevant category INDEX (small), then drills to the page.

```
wiki/
├── INDEX.md                  ← top-level: namespaces only (entities/ sources/ concepts/ predictions/ ...)
├── entities/
│   ├── INDEX.md              ← per-type subcategory list
│   ├── <type-a>/INDEX.md     ← one line per entity (bounded at ~500)
│   └── <type-b>/INDEX.md
├── sources/
│   ├── INDEX.md
│   └── <year>/<month>/INDEX.md   ← by-date partitioning (recency axis)
├── concepts/
│   ├── INDEX.md
│   └── <a-z>/INDEX.md            ← by-letter partitioning
└── predictions/
    └── INDEX.md
```

Each INDEX entry is one line: a markdown link, a type tag, a status, and a one-line summary. The agent reads the index, identifies relevant entries, and opens only those.

```markdown
# Index: entities/<type>

| File | Type | Status | Summary |
|---|---|---|---|
| `entity-foo.md` | vendor | active | Primary platform vendor, segment X |
| `entity-bar.md` | vendor | active | Acquirer, three deals in 18mo |
```

### Sizing rule — the 500-entry rule
**No index file exceeds 500 entries.** When a category grows past 500, it splits into subcategories (or paginates). This keeps every index readable in one pass regardless of total wiki size.

> **In this engine.** `build_index_tree` auto-discovers namespaces from `schema.yaml` and regenerates the INDEX tree; the top INDEX is namespaces-only. The 500-rule is enforced via `reshard_over: 500` in `schema.yaml` (with `reshard_by`), and oversized leaves paginate into `INDEX-pNN.md`. The `reshelve` job re-files flat pages into the hierarchy. The flat→hierarchical migration (`okf_migrate`/`reshelve`) builds this tree while holding the IWE reference count constant — links survive the move.

---

## Strategy 2 — Typed Entity Schema with Mandatory Frontmatter

### The problem
At scale the agent must determine *what a file is* and *whether it's relevant* **without reading its full content**. Without structured metadata it must open files to understand them — an O(n) operation that does not scale.

### The solution
Every file carries **typed YAML frontmatter** the agent filters on before opening. This serves three purposes at once: the agent filters by `type`, `status`, `tags`, dates, etc. before reading (cutting files-opened-per-query dramatically); lightweight tooling (grep, jq, shell) queries the wiki with no LLM involved; and lint identifies stale entries, orphaned links, and schema violations in seconds.

A typed schema names the object kinds the domain tracks and the mandatory fields per kind. In the neutral object-kind terms used above that is roughly:

| Type | Description | Namespace |
|---|---|---|
| `entity` (e.g. `vendor`, `threat-actor`, `malware`) | A tracked organization or agent | `entities/{type}/` |
| `source` | One ingested raw item, with provenance | `sources/{year}/{month}/` |
| `concept` | Cross-cutting segment / pattern / theme | `concepts/{a-z}/` |
| `prediction` | Falsifiable, dated claim with grading lifecycle | `predictions/` |

For a **security** pack the same machinery instantiates `host`, `user`, `service`, `subnet`, `ioc`, `ttp`, `actor`, `baseline`, `investigation`, `finding`, `runbook` — each with its own required-field set and namespace. The point is the *mechanism* (typed, mandatory, filterable frontmatter), not any one domain's type list.

A representative full schema (domain-neutral entity):

```yaml
---
type: vendor
id: entity-foo
title: Foo Systems
description: Primary platform vendor in segment X
status: active                # active | dormant | acquired | defunct | unknown
first_seen: 2024-03-15
last_updated: 2026-06-10
tags: [segment-x, platform, public]
linked_concepts: [concept-segment-x-consolidation]
linked_predictions: [pred-2026-0042]
linked_sources: [src-2026-06-10-foo-q2]
confidence: confirmed         # confirmed | inferred | suspected
---
```

### Partitioning
Within a namespace, large type-sets partition by a dimension that matches the dominant query: **by-letter** (entities/concepts), **by-date** (sources — recency is the natural axis), or **by-type**. Partitioning keeps each leaf index under the 500-rule and gives search/lint a cheap pre-filter.

> **In this engine.** Per-type required fields are declared in `schema.yaml` and enforced by a **write-guard** on the `file` tool plus a **schema-drift-lint** rolling check. Partitioning modes (`by-letter`, `by-date`, `by-type`, `flat`) are configured per-type in `schema.yaml` with `reshard_over`/`reshard_by`. Sources partition by-date, concepts by-letter, entities by-type — the recency axis for sources is structural, not a tag (see Strategy 5).

---

## Strategy 3 — Namespace Partitioning by Write Permission

### The problem
The agent will make mistakes. If it writes an incorrect conclusion and later retrieves it as fact, errors compound. Without write-permission scoping, a single hallucinated entry corrupts stable ground-truth.

### The solution
Partition the wiki into namespaces with explicitly different write policies, enforced by the agent's behavioral contract and ideally by the write path itself.

| Namespace | Agent write policy | Human review |
|---|---|---|
| `entities/` | Create/update with schema validation; must check for existing entry before creating | Required to modify confirmed entries |
| `sources/` | Append-only on ingest; immutable after write | Not required |
| `concepts/` | Draft/update; cannot set `confidence: confirmed` | Required to promote to confirmed |
| `predictions/` | Create draft + update tracking fields | **Human-only** for final grading/resolution |
| `working/` | Free read/write/delete (scratch) | Not required |

The `confidence` field doubles as a within-namespace write gate: the agent may freely write `suspected`/`inferred`; promotion to `confirmed` (or to `false-positive`) requires human approval or a documented evidence threshold. In a security pack the analogous human-confirmed namespace is `findings/`; another common one is **prediction grading** (a graded prediction is human-confirmed).

> **In this engine.** The `file`-tool **write-guard** validates against `schema.yaml`, and **per-job cron toolset scoping** limits what each scheduled agent can touch (the script-failure path additionally strips `terminal`/`file`/`code_execution` so a report-only agent can't mutate). On top of that, the explicit **per-namespace write-permission matrix** is a first-class, enforced artifact: declared in `schema.yaml` `permissions:` (default `create/update: true`, **`delete: false` everywhere → tombstone, not hard-delete**; a namespace may set `create/update: false` to mark it human-authored), exposed via `tools.schema_validator.governing_policy` (walk-up), and enforced in the `okengine-write` MCP server. `wiki/AGENTS.md` is the authoritative permissions + review contract. The "human-only gate" is realized as a **flag, not a hard gate** — there is no blocking human-approval step; categorical confidence verdicts are flagged for review (see Strategy 4) rather than rejected (prediction grading stays autonomous via numeric confidence + `status:`).

---

## Strategy 4 — Tiered Storage by Access Frequency

### The problem
Not all 100k files are equally relevant to a given session. Loading everything into consideration on every query is wasteful and noisy.

### The solution (the reference pattern)
Classify files into tiers by recency and operational relevance, keeping the working set small. These are the **pattern's reference numbers** — adjust to corpus shape:

| Tier | Contents | Approx. count | Agent behavior |
|---|---|---|---|
| **Hot** | Open work, entities with activity in last 7 days, active items | ~500 | Always in session context; index pre-loaded |
| **Warm** | Active in last 90 days, recently closed work, recent sources | ~10,000 | Loaded on demand via index navigation |
| **Cold** | Retired entities, historical work, expired items, old baselines | ~90,000 | Accessed only when explicitly needed; access noted in `log.md` |

Promotion/demotion rules (new activity → promote toward hot; inactivity past a recency window → demote toward cold) live in the behavioral contract and run automatically. In the reference design the tier is a frontmatter tag (`tags: [tier-hot]`) so index/search can filter without reading content.

> **In this engine.** A derived **`HOT.md`** load-first set exists, generated by `build_hot_set` — the agent loads it first each session. The hot/warm/cold tiers are also realized, and — true to the design stance — **derived, not stored**: `scripts/cron/tier_lib.py` `tier_of()` computes a page's tier from recency at query time (hot ≤ `hot_days` 30, warm ≤ `warm_days` 365, else cold; by-date `sources/` derive the date from the path; open predictions floor at hot), so a page self-promotes/demotes as it ages with nothing written onto it. Thresholds live in `schema.yaml` `tier:`. `scripts/cron/tier_refresh.py` is a `no_agent` cron (`tier-refresh`, daily) writing `wiki/operational/tier-distribution.md` (per-namespace hot/warm/cold counts + run-over-run promotion/demotion deltas), and a `--tier`/`tier=` filter is available on `kb_search` and the `okengine-mcp` `search` tool.

---

## Strategy 5 — Plausible File Distribution at Scale

A representative ~100k distribution (security instantiation shown; the shape generalizes — a structured-entity inventory plus a sources corpus dominates the count):

| Namespace | File count | Notes |
|---|---|---|
| structured entities (hosts/users/services or vendors/...) | ~45,000 | one page per tracked entity |
| identifier/source library (IOCs or ingested sources) | ~30,000 | highly structured; rarely opened in full |
| concepts/segments/patterns | ~2,500 | cross-cutting |
| open + closed work threads (investigations/...) | ~15,000 | mostly closed/archival |
| predictions / findings | ~1,000 | falsifiable or human-validated |
| baselines / runbooks | ~6,000 | reference material |
| working/ | ~300 | ephemeral scratch |
| **Total** | **~100,000** | |

Two namespaces (the entity inventory and the source/identifier library) account for ~70% of the count. Both are highly structured and amenable to frontmatter filtering — the agent reads the index, filters on frontmatter, and opens only the specific page it needs. This is *why* Strategies 1–3 pay off: the bulk of the corpus is never opened.

> **In this engine.** The migration files pages into exactly this shape — `entities/{type}/`, `sources/{year}/{month}/`, `concepts/{a-z}/`, `predictions/` — with the INDEX tree and 500-rule applied throughout. The distribution above is the trajectory; the structure is in place and validated at scale.

---

## Strategy 6 — Search Without RAG

### The problem
The hierarchical index handles *structured* navigation. The agent also needs *unstructured* search — finding every page that mentions a specific identifier (an org, a ticker, an IP, a hash, a CVE) without knowing the namespace in advance.

### Why not embeddings-RAG
RAG embeds chunks into a vector space and retrieves by semantic similarity. For this kind of wiki the majority of queries are lookups for **specific identifiers** — exact strings, not semantic concepts. Semantic similarity is the wrong mechanism, and embedding infrastructure adds maintenance burden without value for the dominant query type. **Reject embeddings-RAG.**

### The right approach: ranked lexical search over structured markdown

| Query type | Example | Tool tier |
|---|---|---|
| Exact identifier lookup | a ticker, an IP, `SHA256:4a5c...` | **Tier A** — identifier manifest + ripgrep |
| Partial / fuzzy match | name pattern, partial hash | Tier A regex, or Tier B |
| Keyword phrase | "segment X consolidation pricing" | **Tier B** — BM25 |
| Concept / semantic | "credential access on tier-0 assets" | **Tier C** — qmd hybrid |

- **Tier A — identifier manifest + ripgrep.** A pre-built inverted index mapping key identifiers → file paths, rebuilt incrementally on write; `search_wiki <id>` returns ranked paths. Fast, deterministic, no ML.
- **Tier B — BM25.** Ranked keyword search across the full wiki (tantivy/whoosh-class) for phrase/concept queries.
- **Tier C — qmd.** Local hybrid BM25 + lightweight vector + LLM re-rank, CLI + MCP, for concept queries over narrative content.

> **In this engine.** Retrieval runs on **qmd** (hybrid BM25 + vector + rerank, Tier C — which subsumes Tier B), **IWE** (markdown knowledge-graph: backlinks, graph export, traversal over the `[[wikilinks]]`), and **ripgrep**. Embeddings-RAG is explicitly rejected per the engine's design. The **Tier-A identifier manifest is optional** — qmd + ripgrep already serve exact + ranked lookup, so the manifest is revisited only if qmd latency/cost becomes a constraint.

---

## Strategy 7 — Incremental Rolling Lint (Never Full-Scan)

### The problem
A full lint at 100k files is intractable in a single agent pass — "check everything" means opening every file.

### The solution
Distribute maintenance across time. Each run covers a **bounded, targeted subset**; never the whole corpus at once. Append each run to `log.md` with a structured, parseable prefix.

Reference rolling schedule:

```
Daily   — files modified in last 24h: schema/links/required fields
        — open work items > 7 days stale: flag
        — items past their TTL/recency window: flag for expiry
        — random ~0.1% cold-tier sample (~100 files): schema + link integrity

Weekly  — one namespace cross-reference sweep (verify linked_* exist)
        — entries with status: unknown > 30 days: flag
        — duplicate detection within one namespace
        — baselines/sources past re-validation window

Monthly — completeness audits across a type (e.g. graded predictions, closed work)
        — currency checks (entity pages reflecting newest linked items)
```

Log format (consistent prefix → greppable):

```markdown
## [2026-06-14] lint | daily | 247 files checked | 3 issues found
- STALE: pred-2026-0071 — open, no update in 12 days
- EXPIRY: src-2026-05-... — past recency window
- BROKEN_LINK: entity-foo.md → linked_prediction pred-2026-0055 missing
```

> **In this engine.** Lint is rolling and distributed across the cron fleet, never a full scan. **`schema-drift-lint`** runs whole-vault via the walk-up validator on a rolling cadence, alongside many targeted drains: `broken-wikilinks`, `orphans`, the `repair-*` family, `normalize`/`sanitize` frontmatter jobs, and a **field-loss detector**. Each writes its findings into the audit trail. The maintenance burden is spread across the cron fleet rather than concentrated in one pass — which is exactly the 100k-scale prescription.

---

## Strategy 8 — Agent Behavioral Contract

### The problem
At scale, behavioral consistency is a functional requirement. Without explicit rules the agent creates duplicates, writes to wrong namespaces, and emits inconsistent frontmatter that breaks downstream tooling.

### The solution
A formal contract — `AGENTS.md` in the wiki root (OKF convention) — defining the rules for every read and write. The reference design treats this as *the most important single document in the wiki*.

**Before writing any new page:** (1) search the namespace index for an existing page on this entity; (2) if found, update it — never duplicate; (3) if not, create from the correct `type` template; (4) validate all required frontmatter; (5) update the namespace `INDEX.md`; (6) append to `log.md`.

**Before answering any query:** (1) read top-level INDEX → identify namespaces; (2) read the namespace INDEX(es); (3) filter by frontmatter before opening; (4) open only the identified pages; (5) file valuable synthesis back as a new page.

**Confidence promotion:** agent may promote `suspected → inferred` with documented reasoning; `inferred → confirmed` requires human approval or an authoritative automated source; any `→ false-positive` requires human approval.

**Deduplication:** flag both candidates with `tags: [duplicate-candidate]`, add to a dedup queue, never silently merge, resolve via review.

**Write-permission matrix:** per-namespace create/update/delete + human-only gates (Strategy 3).

> **In this engine.** `wiki/AGENTS.md` exists at the wiki root as the authoritative permissions + review contract — so the before-write / before-answer / dedup discipline is in force via the contract and the write-guard. The formal **confidence trust model** and the explicit, *enforced* **permission matrix** are first-class artifacts: the matrix lives in `schema.yaml` `permissions:` (enforced in the `okengine-write` MCP server), and the `schema.yaml` `review:` block makes categorical confidence verdicts **flag** the page `needs_review: true` + append to `wiki/_review-queue.md` + log, never block — plus **tombstone-on-delete**: `tombstone_entity` sets `status: tombstoned`/`superseded_by` and retains the file rather than `rm`.

---

## Structural Files

OKF defines `index.md` and `log.md`; the engine generates a richer set, all at every relevant level:

- **`INDEX.md`** — the navigable tree (Strategy 1). Top level lists namespaces with file counts; leaves list pages.
- **`log.md`** — chronological, append-only audit trail of every create/update/lint action.
- **`AGENTS.md`** — the behavioral contract (Strategy 8).
- **`BUNDLE.md`** — pack/bundle manifest.
- **`HEALTH.md`** — KB-health snapshot (coverage, drift, broken-link counts).
- **`HOT.md`** — derived load-first set (Strategy 4).

Top-level INDEX example:

```markdown
# Wiki Index

| Namespace | Description | File Count | Last Updated |
|---|---|---|---|
| `entities/INDEX.md` | Tracked organizations and agents | 45,000 | 2026-01-15 |
| `sources/INDEX.md` | Ingested raw material, by date | 30,000 | 2026-01-15 |
| `concepts/INDEX.md` | Segments, patterns, themes | 2,500 | 2026-01-15 |
| `predictions/INDEX.md` | Falsifiable dated claims | 1,000 | 2026-01-14 |
```

> **In this engine.** INDEX tree, `log.md`, `AGENTS.md`, `BUNDLE.md`, `HEALTH.md`, and `HOT.md` are all generated and maintained by cron jobs. This is *ahead* of the bare OKF structural minimum (`index.md` + `log.md`).

---

## Tooling Ecosystem

| Tool | Role | Status |
|---|---|---|
| **IWE** | Markdown knowledge-graph — backlinks, graph export, traversal over `[[wikilinks]]`; LSP/CLI/MCP | ✅ in use (read-only wrapper `kb_graph.py`) |
| **qmd** | Local hybrid BM25 + vector + rerank search; CLI + MCP | ✅ in use (wrapper `kb_search.py`) |
| **ripgrep** | Fast lexical / regex search; sub-second across 100k files | ✅ in use |
| **okengine-reader** | Human web browsing/review interface over the vault | ✅ in use |
| **okengine-mcp** | MCP query surface for the agent | ✅ read-only; ✅ write tools (`okengine-write`) |

> **In this engine.** The read stack: IWE + qmd + ripgrep for retrieval, `okengine-reader` for human review, and `okengine-mcp` exposing `search`/`get_page`/`find_references`/`list_*`. The **write path through MCP**: a separate local stdio server **`okengine-write`** exposes `create_entity`/`update_entity`/`tombstone_entity`/`flag_for_review`/`patch_entity`/`append_to_section`, each validating against the walk-up `schema.yaml` and appending to `log.md` before write. Writer crons run on this enforced MCP write path; the `file`-tool schema validator is kept as the shape backstop.

---

## Engine mechanism map

How each strategy maps to a concrete engine mechanism:

| # | Strategy | Engine mechanism |
|---|---|---|
| — | OKF conformance baseline | `schema.yaml` `okf.required:[type]`; walk-up validator; one-file-per-entity; link graph; structural files |
| 1 | Hierarchical INDEX tree (500-rule) | `build_index_tree`, `reshard_over:500`, `INDEX-pNN.md` pagination, `reshelve` |
| 2 | Typed schema / mandatory frontmatter | per-type required fields in `schema.yaml`; write-guard; drift-lint; by-letter/by-date/by-type partitioning |
| 3 | Namespace partitioning by write permission | write-guard + per-job toolset scoping + explicit matrix (`schema.yaml` `permissions:`, enforced in `okengine-write`) |
| 4 | Tiered hot/warm/cold storage | derived `HOT.md` via `build_hot_set`; derived tiers (`tier_lib.tier_of`) + `tier-refresh` cron + `--tier` filter |
| 5 | Search without RAG | qmd (hybrid) + IWE + ripgrep; RAG rejected; identifier manifest optional |
| 6 | Incremental rolling lint | `schema-drift-lint` + drains (broken-wikilinks, orphans, repair-*, normalize, sanitize, field-loss); spread across the cron fleet |
| 7 | Agent behavioral contract | `wiki/AGENTS.md` is the authoritative permissions + review contract; before-write/before-answer/dedup in force; confidence flag-model + formal matrix |
| 8 | Structural files | INDEX tree, log, AGENTS, BUNDLE, HEALTH, HOT all generated |
| 9 | Tooling ecosystem | IWE + qmd + ripgrep + okengine-reader + okengine-mcp (read); MCP write path via `okengine-write` |

The engine is also *ahead* of the bare reference design on deployability (engine/pack split, versioned engine, a public reference pack — okpack-sec, a separate repo — plus multi-domain-in-one-vault via walk-up schema), search, the INDEX/500-rule tree, and structural files. Every mechanism above is **pattern-level and domain-agnostic** — each lands in the engine and every pack inherits it.

---

## Key References

| Resource | URL |
|---|---|
| Karpathy LLM Wiki gist (origin of the pattern) | https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f |
| IWE — markdown knowledge graph | https://github.com/iwe-org/iwe |
| qmd — local hybrid search | https://github.com/tobi/qmd |
| Is Grep All You Need? (lexical vs semantic) | https://www.llamaindex.ai/blog/is-grep-all-you-need-lexical-vs-sematic-search-for-agents |

*Scoped to the wiki scaling and schema layer. Domain examples are illustrative; the strategies are domain-independent — and supported by the engine across multiple domains in one vault and a link-preserving hierarchical migration.*
