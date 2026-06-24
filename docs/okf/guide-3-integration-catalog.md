# Integration Catalog: Connecting Data Sources to an Agent-Maintained Vault

Domain-agnostic; generalized from the security integration catalog; reflects the engine as of 2026-06-15. Security sources are the first worked example; the reference pack is **okpack-sec** (a security-focused LLM-wiki pack, maintained in its own repo).

The Open Knowledge Format (OKF) gives a universal syntax for representing knowledge but deliberately omits guidance on *how* external data gets in. This catalog fills that gap **generically**: how any **domain pack** wires its sources into an agent-maintained vault without creating duplicate entities, losing provenance, or bloating the store with ephemera. The patterns here are the deliverable. The security source list (MITRE ATT&CK, CISA KEV, NVD, EPSS, MISP, Abuse.ch, STIX/TAXII, OCSF) is one worked example, summarized at the end.

A **pack** is the domain-specific layer the engine consumes: its feed lists (`pack/feeds/*.opml`), data tables (`pack/data/*`), entity-type contract (`schema.yaml` `types`), and persona/curation rules. The engine is domain-clean; everything below is configured by the pack, not coded per-domain.

---

## 1. The Source Taxonomy (the core)

A foundational principle: **not all sources are ingested the same way.** Treating a high-velocity, ephemeral feed like a stable, authoritative reference set yields either a bloated vault or missed signal. Every source a pack catalogs is classified into exactly one of three classes, each with a distinct ingestion pattern.

| Class | What it is | Ingestion pattern | Writes to vault? |
|-------|-----------|-------------------|------------------|
| **Bundle** | Static or semi-static, authoritative, **redistributable**. | Pre-computed once per release cycle into a read-only `reference/` namespace. The agent reads, never writes. | Yes — bulk, scheduled regeneration. |
| **Query** | High-velocity, ephemeral, or **non-redistributable**. | Reached at **runtime via an MCP tool call**. Results are *not* auto-written. | Only if **locally significant** — the agent calls a create tool and cites the source. |
| **Enrichment** | Authoritative metadata that **annotates an existing entity** rather than creating a new one. | An MCP hook fired on entity create/update; the returned fields are injected into frontmatter before write. | Yes — but as fields on an existing page, never a new page. |

**How to classify a candidate source — decision rules:**

1. Is it redistributable and stable enough to materialize as files? → **Bundle**. (Else not Bundle.)
2. Does it produce *new* entities, or only *attributes of* entities you already have? → new entities = Bundle/Query; attributes-only = **Enrichment**.
3. Is it high-volume, fast-aging, or licensed against bulk storage? → **Query** (materialize nothing by default; pull on demand).

A single source can shift class by use: the same vulnerability feed is a Bundle if you mirror the catalog daily, or a Query if you only look up a CVE when one surfaces. The pack records the chosen class per source.

### 1.1 Namespace structure

The taxonomy maps directly onto where (and whether) a source gets a namespace.

```text
vault/
├── reference/              ← READ-ONLY: Bundle sources, pre-computed
│   ├── <bundle-a>/         ← one subdir per Bundle source
│   └── <bundle-b>/
├── wiki/                   ← WRITABLE: agent-maintained knowledge
│   ├── sources/            ← ingested source records (feeds → raw → here)
│   ├── entities/           ← typed entities (per schema.yaml `types`)
│   ├── concepts/
│   └── predictions/
└── raw/                    ← landing zone (immutable inputs, pre-ingest)
```

- **Bundle sources → `reference/`, read-only.** The agent cross-links *to* them but cannot modify them. They are regenerated out-of-band on the source's cadence.
- **Writable agent namespaces** (`wiki/sources`, `entities`, `concepts`, `predictions`) hold what the agent compiles and maintains.
- **Query and Enrichment sources get NO namespace.** They live at the MCP surface at runtime. Query results materialize into `wiki/` only when the agent judges them locally significant; Enrichment data only ever lands as fields on a page that already exists.

The writable layout above is the pack's structural contract, declared in `schema.yaml` `partitioning` (by-type for entities, by-date for sources, by-letter for concepts), so the engine reads layout instead of hardcoding it. A different pack declares a different scheme.

---

## 2. Integration mechanics

Four mechanisms cover every source class.

### 2.1 Bundle ingestion (Bundle → `reference/`)

Generic bundle ingestion is a parse-map-emit-link loop:

1. **Parse** the source artifact (JSON/CSV/graph/whatever the source ships).
2. **Map fields** from the source's native object schema to the pack's `schema.yaml` `types` (see §5).
3. **Emit one file per object** into the source's `reference/<source>/` subdir, with provenance frontmatter (§4).
4. **Translate source relationships into wiki links** — every native edge (a reference, a "relationship" object, an ontology predicate) becomes a `[[wikilink]]` so the bundle is navigable as a graph, not a flat dump.

Bundle ingestion is a batch job on the source's release cadence (§6); it overwrites the read-only namespace wholesale, so it must be deterministic.

### 2.2 The feeds → raw → ingest pipeline (the engine's live-source path)

This is how a pack ingests **streaming/published** sources (news, blogs, filings, paper feeds). It is **pure config plus one generic engine script** — a new domain supplies an OPML file and changes no code.

```
pack/feeds/*.opml   →  feed_fetch.py  →  raw/<lane>/  →  wake-gated ingest agent  →  wiki/sources + entities/concepts/predictions
(feed list, config)    (engine, generic)  (raw landing)   (LLM, relevance triage)      (compiled knowledge)
```

1. **OPML feed list** (`pack/feeds/<lane>.opml`) — feeds are pure config. Each `<outline>` carries an `xmlUrl`; `feed_fetch.py` reads every outline with a feed URL. Outlines are grouped by source *type* (lab blogs, VCs, practitioner, etc.), not by the final classification — the classification is assigned at ingest time, per item.
2. **Generic fetcher** — `scripts/cron/feed_fetch.py` (engine) fetches and parses RSS + Atom, dedupes each item against a per-lane state file (`--state`), and lands each *new* item as a raw queue file (`type: source`, `source_channel: feed`, with `title`/`url`/`published`/`fetched`/`watch_lane`) into `--out-dir`. The domain ingest step assigns the final `source_kind` from the pack schema. The fetcher is `no_agent` (a pure-script pull that ends `{"wakeAgent": false}`); the optional `--digest` mode instead emits markdown to stdout for an agent-consumption cron.
3. **Raw landing** — new items accumulate in `raw/<lane>/` as immutable inputs. No knowledge is compiled yet; this is the queue.
4. **Wake-gated ingest agent** — a separate cron compiles `raw/<lane>/` → `wiki/sources/` (plus `entities`/`concepts`/`predictions`) with **relevance triage**: the agent decides what is worth a permanent page, drops noise, resolves entities to canonical pages (§5), and writes provenance (§4). The "wake gate" means the agent only runs when there is new raw material, not on a blind schedule.

**Worked reference — the okpack-sec pack** runs exactly this: a `<lane>.opml` feed list → `feed_fetch.py` → `raw/<lane>/` → a wake-gated `*-ingest` cron compiles into `wiki/`. Every pack that ingests feeds reuses this same engine path.

### 2.3 Enrichment hooks (Enrichment → fields on existing entities)

1. On the MCP server's create/update of an entity, the write is intercepted.
2. The server queries the enrichment source keyed on the entity's identifier.
3. Returned metadata is injected into the entity's frontmatter **before** the file is written — additively. Enrichment fields never overwrite the entity's own fields; they sit alongside them.

Enrichment never creates a page. If the keyed entity does not exist, the hook is a no-op.

### 2.4 Query tools (Query → runtime MCP, conditional write)

1. The MCP server exposes a per-source query tool.
2. During its work the agent calls the tool with a lookup key and gets the result **in context**.
3. Nothing is written by default.
4. If — and only if — the agent judges the result locally significant, it calls a create tool to write a page, **citing the originating source**. Ephemeral results should carry a TTL so they age out.

This project's read-only MCP surface is **`okengine-mcp`** (`search` / `get_page` / `find_references` / `list_*`); it is how other agents and consumers query the vault. The MCP-enforced write path is **DONE** (G1): a separate local stdio server **`okengine-write`** (wired as `mcp_servers.okengine-write`) exposes 6 write tools — `create_entity`, `update_entity`, `tombstone_entity`, `flag_for_review`, `patch_entity`, `append_to_section` — each schema-validated against the governing `schema.yaml` before writing and log-appending. The `file`-tool write-guard (`schema_validator`) remains as the schema/shape backstop. Query-source runtime tools attach to this surface.

---

## 3. Provenance & metadata

Every ingested entity carries provenance frontmatter so a downstream consumer can judge and trace it. Required fields:

| Field | Purpose |
|-------|---------|
| `type` | The pack `schema.yaml` type (OKF's one mandatory field). |
| `title` | Human-readable name; also the dedup/aliasing anchor. |
| `reliability` + `credibility` | **Source-trust rating, Admiralty-style** — reliability of the *source* (A–F) and credibility of the *information* (1–6). Set at ingest. |
| `confidence` | The agent's/human's confidence in the claim (distinct from source trust). |
| source citation (`url` / `source` / `raw`) | The originating source — always cite where it came from. |
| sensitivity marking (`tlp`-analogue) | A sharing/sensitivity marking analogous to TLP (e.g. `clear` / `internal` / `restricted`). |

**Trust scoring** is the pack's call: the okpack-sec pack scores reliability + credibility per a rubric in its persona `CLAUDE.md` (a curated source roster maps known publishers to ratings). A different pack supplies its own rubric. The engine carries the *fields*; the pack carries the *scoring*.

**Inherit-most-restrictive.** A synthesized page (one compiled from several sources) inherits the **most restrictive** sensitivity marking among its inputs, and the **lowest** trust among load-bearing inputs. If a synthesis draws on a `restricted` source, the synthesis is `restricted`. When inputs carry mixed markings and you cannot honor the most-restrictive in one namespace, split the ingest by marking. Always cite each contributing source.

---

## 4. Mapping external objects → vault types (the generic pattern)

A source ships its own object model; the pack ships `schema.yaml` `types`. Mapping is a three-rule discipline:

1. **Map native types → pack types.** Each source object type maps to a pack `type`. The mapping is explicit and recorded per source (in the pack data table or the ingest prompt), so it is auditable and re-runnable.
2. **Consolidate near-duplicates with tags.** When a source distinguishes object kinds the pack treats as one type, map all to the single pack type and preserve the distinction with a `tag`. (E.g. a source separating "tool" and "library"; the pack has one `software` type → both map to `software`, tagged `[tool]` / `[library]`.) This keeps the type set small without losing information.
3. **Resolve aliases to a canonical entity.** Sources name the same real-world entity differently. Pick one canonical `title`/id (prefer the most authoritative source's name), record the rest in an `aliases:` list, and resolve incoming aliases to the canonical page at ingest — so a second source mentioning the entity under another name updates the existing page instead of creating a duplicate.

The validator walks **up to the nearest `schema.yaml`** for the page being written, so a sub-domain (e.g. a `wiki/<subdomain>/` tree with its own `schema.yaml`) maps to its own `types` while the root pack maps to the root types. Mapping is per-namespace, not global.

---

## 5. Update cadence

Each source declares a regeneration cadence matched to how fast its data moves and its class.

| Class | Cadence pattern | Rule |
|-------|----------------|------|
| **Bundle** (stable) | On the source's release cycle (quarterly, monthly). | Regenerate the `reference/<source>/` namespace wholesale on schedule. |
| **Bundle** (fast-moving but redistributable) | Daily automated regeneration. | A daily cron rebuilds the bundle; do not hand-edit. |
| **Feed** (published streams) | Continuous pull, wake-gated compile. | `feed_fetch.py` on a short cron (e.g. every few minutes) lands raw; the ingest agent wakes only on new raw. |
| **Enrichment** | Per entity write, plus periodic refresh. | Hook fires on create/update; a periodic job re-pulls stale enrichment fields. |
| **Query** | **Never materialized on a schedule.** | Pull on demand only. High-velocity sources stay Query — do **not** create a permanent page per item; that is the bloat the taxonomy exists to prevent. |

The cardinal cadence rule: **a high-velocity source stays Query.** Materializing every item from a fast, ephemeral feed is the failure mode the taxonomy is designed to avoid. Bundle is for sources stable enough to mirror; everything fast is queried.

---

## 6. Example from the reference pack

These are illustrations of the patterns above, not the subject.

**okpack-sec pack** (the security-focused LLM-wiki pack, maintained in its own repo) catalogs the security sources by class with no engine change: MITRE ATT&CK / D3FEND / ATLAS, CISA KEV, NVD as **Bundle** sources in `reference/`; EPSS as an **Enrichment** source (annotates `vulnerability` entities); MISP and Abuse.ch as **Query** sources at the MCP surface; STIX/TAXII as the transport for whichever class a given feed is; OCSF as a schema reference for event-shaped entities. It wires its feeds through the standard `<lane>.opml` → `feed_fetch.py` → `raw/<lane>/` → wake-gated ingest pipeline (§2.2), and scores reliability + credibility per a rubric in its persona `CLAUDE.md` (a curated source roster maps known publishers to Admiralty-style ratings). Same taxonomy, same mechanics — any pack supplies its own sources and rubric.

---

## Worked example: the security source set

The original security integration catalog classified the major security sources as below. Reproduced here as the canonical worked example of §1–§5 applied end-to-end in one domain.

| Source | Class | Native → vault type mapping | Default trust | Notes |
|--------|-------|------------------------------|---------------|-------|
| **MITRE ATT&CK** | Bundle (STIX 2.1, quarterly) | `attack-pattern`→TTP, `intrusion-set`→Actor, `malware`/`tool`→Software, `campaign`→Campaign, `course-of-action`→Mitigation | A1, `TLP:CLEAR` | Canonical actor names; aliases (e.g. "Cozy Bear") resolve to the ATT&CK id. |
| **MITRE D3FEND** | Bundle (OWL/JSON-LD, quarterly) | defensive technique→Mitigation, artifact→Asset | A1, `TLP:CLEAR` | Flatten ontology to dirs; RDF predicates → cross-links to ATT&CK. |
| **CISA KEV** | Bundle (JSON/CSV, **daily regen**) | CVE entry→Vulnerability | A1, `TLP:CLEAR` | KEV's `exploited_in_wild` overrides NVD status; `dueDate`→`action_required_by`. |
| **NVD / CVSS** | Bundle (JSON 2.0 API, daily regen) | CVE item→Vulnerability | A1, `TLP:CLEAR` | Highest CVSS version → `cvss_score`; severity ≠ exploitation status. |
| **EPSS** | **Enrichment** (daily) | enriches existing Vulnerability | A1, `TLP:CLEAR` | Hook injects `epss_score`/`epss_percentile`; never overwrites CVSS. |
| **MITRE ATLAS** | Bundle (YAML/STIX, monthly) | technique→TTP `[atlas]`, mitigation→Mitigation, case-study→Finding | A1, `TLP:CLEAR` | Own namespace; cross-link to Enterprise ATT&CK. |
| **STIX / TAXII** | Transport (Bundle or Query) | `indicator`→IOC, `malware`/`tool`→Software, `threat-actor`→Actor, `vulnerability`→Vulnerability, `campaign`→Campaign | per feed | Map STIX TLP → sensitivity; heuristic for STIX confidence → Admiralty. |
| **MISP** | **Query** (typically) | Event→Investigation/Campaign, Attribute→IOC, Galaxy→Actor/TTP | variable; default C3, `TLP:AMBER` | Variable quality — not authoritative reference; query, don't bulk-ingest. |
| **Abuse.ch** (MalwareBazaar / URLhaus / ThreatFox) | **Query** (continuous) | feed item→IOC (tagged) | B2, `TLP:CLEAR` | High-volume, fast-aging → query + enrich; apply strict TTL on any written IOC. |
| **OCSF** | Schema reference (not a feed) | OCSF event→Event/Baseline | — | Structures event-shaped entities; core fields → frontmatter, payload → code block. |

---

## References

- Generalized from: *Security Integration Catalog: Reference Data Sources* (v0.2 draft).
- Engine feed mechanism: `scripts/cron/feed_fetch.py`.
- Reference pack feeds: the okpack-sec pack's `pack/feeds/<lane>.opml` lists (okpack-sec is a separate repo).
- Pack data tables: the pack's `pack/data/*.yaml` / `pack/data/*.json` (e.g. a public-entity roster + curated entity fields).
- Entity-type contract: `schema.yaml` `types` (root) and any `wiki/<subdomain>/schema.yaml` (sub-domain, resolved by the walk-up validator).
- LLM-wiki pattern (origin of OKEngine): Andrej Karpathy, [LLM wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
