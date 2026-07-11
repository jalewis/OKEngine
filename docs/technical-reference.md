# OKEngine / OKPacks — Technical Reference

*The single heavy document: what OKEngine is, every major subsystem, how the parts
compose, and the operational lessons that shaped the design. Written to be read
end-to-end by someone technical who has never seen the codebase. Everything here
is domain-agnostic and shareable; deployment-specific details (hostnames, brands,
private packs) are deliberately absent, per the engine's own scrub rule.*

*Current as of engine **v0.9.1** on Hermes-Agent **v0.18.0** (upstream tag
`v2026.7.7.2`), July 2026. Pointers into the repo are given throughout; where this
document and the code disagree, the code wins.*

---

## Table of contents

1. [The concept](#1-the-concept)
2. [OKF — the Open Knowledge Format](#2-okf--the-open-knowledge-format)
3. [System decomposition: Runtime / Engine / Pack](#3-system-decomposition-runtime--engine--pack)
4. [The runtime layer: a pinned Hermes-Agent](#4-the-runtime-layer-a-pinned-hermes-agent)
5. [The enforced write path](#5-the-enforced-write-path)
6. [Schema composition](#6-schema-composition)
7. [The cron machinery](#7-the-cron-machinery)
8. [The maintenance fleet](#8-the-maintenance-fleet)
9. [Retrieval, graph, and read surfaces](#9-retrieval-graph-and-read-surfaces)
10. [The extension system](#10-the-extension-system)
11. [Model management and cost discipline](#11-model-management-and-cost-discipline)
12. [Packs](#12-packs)
13. [Deployment topology and trust](#13-deployment-topology-and-trust)
14. [Operational lessons (field notes)](#14-operational-lessons-field-notes)
15. [Known limits and open directions](#15-known-limits-and-open-directions)

---

## 1. The concept

Most LLM knowledge systems are **query-time** systems: retrieval-augmented
generation embeds a corpus, retrieves chunks per question, and synthesizes an
answer that is then discarded. The synthesis work — entity resolution, claim
grading, cross-linking, trend detection — is repeated on every query and never
accumulates.

OKEngine inverts this. It is a **compile-time** knowledge system: an agent
ingests raw material once, compiles it into typed, cross-linked markdown pages,
and a machine fleet keeps that corpus healthy indefinitely. Queries then read
*already-synthesized* knowledge. The wiki is the memory; retrieval is merely an
access path into it. (The catalyst was Karpathy's LLM-wiki idea: an agent should
accumulate knowledge in a durable wiki instead of rediscovering it from scratch
per query.)

The one-line factoring of a live system:

```
a live deployment  =  OKEngine @ a pinned Hermes-Agent  +  ONE domain pack
```

- **OKEngine** (this repo) is the reusable, domain-agnostic layer: write
  governance, validation, indexing/health/tiering, repair drains, cron
  machinery, retrieval/graph integration, read surfaces, extension system,
  deploy tooling. It ships **no domain knowledge**.
- **Hermes-Agent** is the agent runtime (gateway, agent loop, model providers,
  cron scheduler, MCP plumbing). It is consumed as a **pinned dependency**, not
  a fork (§4).
- **A pack** ("okpack") is the domain layer: schema, persona, feeds, prompts,
  domain crons, seed content. Swap the pack, and the same engine maintains a
  security wiki, a competitive-intelligence wiki, an AI-research wiki, a market
  wiki.

Why the pattern beats RAG-as-memory, in operational terms:

| Property | RAG-as-memory | Agent + compiled wiki |
|---|---|---|
| Synthesis cost | paid per query, forever | paid once at ingest |
| Cross-entity structure | none (chunks) | typed pages + wikilink graph |
| Claim accountability | none | `sources:` provenance, grading lanes, tombstones |
| Drift handling | re-embed and hope | repair drains, conformance audits, health dashboards |
| Human legibility | opaque vectors | a browsable wiki (reader/cockpit UIs) |
| Falsifiability | n/a | a `prediction` type with graded outcomes |

The full argument, including where RAG still fits (it remains a retrieval
technique layered *on top* of the wiki), is in
`docs/okf/guide-1-agent-wiki-pattern.md`.

---

## 2. OKF — the Open Knowledge Format

OKF (Open Knowledge Format) is the small contract that makes the corpus
machine-maintainable. Its floor is deliberately tiny — **a page is a markdown
file with YAML frontmatter carrying at least `type:`** — and everything else is
layered contract.

### 2.1 The three layers of a vault

```
vault/
├── raw/          # immutable ground truth: fetched articles, feed items, filings.
│                 # Never edited, never deleted; processed items are renamed
│                 # *.processed.md so ingest lanes can find the unprocessed tail.
├── wiki/         # the agent-maintained knowledge layer (typed pages, see below)
├── schema.yaml   # the structural contract for wiki/ (types, fields, partitioning)
└── CLAUDE.md     # the persona: domain judgment, curation rules, voice
```

This maps to a clean separation of concerns: `raw/` is what happened, `wiki/` is
what it means, `schema.yaml` + `CLAUDE.md` are the contract and the judgment.

### 2.2 Page anatomy

```markdown
---
type: source                      # REQUIRED — the only universal floor
title: "Vendor X acquires Y"
published: 2026-06-21
raw: raw/market/2026-06-21-item.processed.md   # provenance back to ground truth
sources: ["[[sources/2026/06/other-page]]"]     # provenance across the wiki
reliability: B                    # graded, not asserted
needs_review: false               # universal review scaffolding
version: 3                        # write-path maintained
created: '2026-06-21T09:00:00Z'
last_updated: '2026-07-01T05:12:44Z'
---
# Vendor X acquires Y
Prose with [[wikilinks]] into the graph...
```

Key conventions:

- **Types** define required + optional fields (§6). Seven core types ship with
  the engine: `source`, `concept`, `prediction`, `finding`, `dashboard`,
  `briefing`, `trend`. Packs and extensions add domain types (`vendor`,
  `threat-actor`, `term`, `lacuna`, …).
- **Namespaces** are directories with meaning: `sources/YYYY/MM/` (time-sharded),
  `entities/<letter>/` (alphabet-partitioned — §14.1), `concepts/`,
  `predictions/`, `dashboards/`, `briefings/`, `operational/`.
- **Wikilinks** (`[[target]]` / `[[path/target|Label]]`) are the graph edges.
  Backlink resolution is computed (§9), not stored.
- **Tombstones, not deletions.** The write path never hard-deletes an
  agent-managed page; it tombstones, so history is auditable.
- **Generated pages self-describe.** A generated dashboard can carry a
  `panel:` block in frontmatter (e.g. `{kind: two-axis, nodes: [...]}`) that the
  read surfaces render as a chart — data travels with the page, renderers stay
  generic (§9.3).

### 2.3 Conformance

`tools/schema_validator.py` is the conformance contract: given a page and the
governing schema, it produces a reject reason or passes. It runs in two places —
inline in the enforced write path (§5), and corpus-wide in the conformance-audit
lane (§8). The conformance profile (G1 enforced write path, G2 namespace
permissions, G3 review flags + tombstones, G4 hot/warm/cold tiers) is specified
in `docs/okf/okengine-conformance-spec.md`.

Schema resolution is **walk-up**: the nearest `schema.yaml` above a page governs
it. One vault can therefore host a root domain plus sub-domains
(`wiki/<subdomain>/schema.yaml`), each with its own types and rules, sharing one
instance (§13).

---

## 3. System decomposition: Runtime / Engine / Pack

The honest decomposition is three layers, declared file-by-file in
`engine-manifest.yaml` and specified in `docs/engine-domain-boundary.md`:

| Layer | Owns | Changes per domain? |
|---|---|---|
| **Runtime** (pinned Hermes) | gateway, agent loop, model providers, cron scheduler, transports, MCP plumbing | No |
| **Engine** (this repo) | write governance, validator, cron fleet + composition, indexing/tiering/health, repair drains, retrieval/graph glue, reader + cockpit UIs, MCP servers, extension system, deploy tooling, base schema | No |
| **Pack** | domain schema, persona, feeds, prompts, domain crons, data tables, content, secrets | **Yes** |

Two hard rules keep the decomposition real:

1. **No domain knowledge in the engine.** No vendor names, no private
   hostnames, no deployment paths. Anything domain-specific enters as a pack
   input (schema field, env var, config file). A grep-based scrub check runs
   before every engine commit.
2. **Generated artifacts are never hand-edited.** The deployed cron file
   (`config/cron-plus-jobs.json`) is produced by the composition pipeline (§7.4);
   deployed runtime copies are produced by the `deploy-*.sh` scripts. Edit the
   source, regenerate.

Version coupling is explicit: the engine pins Hermes
(`engine-manifest.yaml: pinned_version / pinned_tag / pinned_sha`), and each pack
pins the engine + Hermes (`<pack>/engine.version: version + hermes_pin`). An
upgrade is therefore a deliberate, testable event at every layer (§4.2, §14.6).

---

## 4. The runtime layer: a pinned Hermes-Agent

### 4.1 Consume, don't fork

Hermes-Agent provides the agent substrate: a **gateway** process hosting
messaging platforms, an API server, the cron scheduler, the agent loop with tool
execution, model-provider plugins (OpenAI-compatible, Ollama-style `custom`
endpoints, DeepSeek, …), sandboxed terminals, and MCP client/server plumbing.

OKEngine **does not fork it**. The engine clones Hermes at a fixed tag, applies a
small set of carried patches, and overlays its own code. The discipline:

- Engine changes are **additions** (overlay files) or **carried patches**
  (`patches/*.patch` + `patches/apply.sh`) — never in-place edits of upstream
  files outside the patch set.
- The patch set is kept minimal and each patch has one job. Currently 7:

| Patch | What it guards |
|---|---|
| `01-file-operations-write-guard` | blocks direct file-tool writes into the governed vault (forces the MCP write path) |
| `02-file-tools-doubled-path-guard` | rejects doubled-path artifacts (`wiki/wiki/...`) from confused tool calls |
| `03-cron-scheduler-failure-path-guard` | strips dangerous toolsets (terminal/file/code-execution) from cron failure-path runs |
| `04-usage-pricing-models` | adds pricing entries for models upstream doesn't know |
| `05-delegate-tool-session-end` | fixes delegate-tool session teardown |
| `06-cron-per-job-ollama-num-ctx` | per-cron-job context-length override for Ollama-style endpoints |
| `07-api-server-inference-model` | pins the interactive API-server agent's model independently of the bulk default |

- `scripts/build-engine-image.sh` verifies the pinned SHA, applies patches, and
  builds the deployable gateway image (`hermes-agent:okengine-vX.Y.Z` + `:latest`).

### 4.2 Upgrading the pin

Moving to a new Hermes version is a documented procedure (kept in the
maintainer-internal `docs/hermes-upgrades/`, excluded from the public snapshot):
bump the pin → re-apply patches (3-way rebase of any
that fail; drop patches upstream absorbed) → acid-test all patches against a
stock tree → build the image → roll a **guinea-pig deployment** first and run a
gauntlet (config migration, cron ticker liveness, a `no_agent` lane, an agent
lane performing a **real MCP vault write**, env-knob survival, zero failing
lanes) → soak → roll remaining deployments → bump pack `hermes_pin`s only when
everything is green. Each upgrade file records the incidents it hit — those
records are load-bearing (§14.6).

---

## 5. The enforced write path

**The single most important architectural decision in the system**: agent writes
to the vault go through one enforced chokepoint, `okengine-mcp/write_server.py`
(the `okengine-write` MCP server, stdio transport). Not "agents are asked
nicely to use it" — the runtime patch set (§4.1, patch 01) blocks the generic
file tools from writing governed paths, so the MCP path is the *only* way an
agent can mutate `wiki/`.

What the write server enforces per call:

1. **Schema validation** — the page (post-write) must pass
   `schema_validator` against the governing (walk-up) schema: type exists,
   required fields present, enums valid, namespace accepts the type.
2. **Namespace permissions** — a pack's schema can mark namespaces
   agent-writable, human-only (e.g. `findings/`), or reserved. Permission
   errors are hard rejections.
3. **Field-loss guard** — an update that would silently *drop* existing
   frontmatter fields is rejected; the agent must carry fields forward. This
   kills the classic LLM failure of rewriting a page from partial memory.
4. **Reserved-file guard** — `INDEX.md`, dashboards owned by generators, and
   other machine-maintained files reject agent writes.
5. **Tombstoning** — deletes become tombstones; nothing vanishes.
6. **Version/audit stamping** — `version`, `created`, `last_updated` are
   maintained by the path, and every successful write appends a line to
   `wiki/log.md` (a flat, greppable audit log).

The read side is a separate server (`okengine-mcp/server.py`): `search`,
`get_page`, `find_references` and friends, exposed over HTTP with bearer auth so
*other* agents and tools can consume the compiled graph without write access.

Why a chokepoint matters more than a convention: every quality property in §2
(provenance, field integrity, taxonomy conformance, auditability) is enforced at
the one boundary every writer crosses. Policies that live only in prompts decay;
policies at the boundary do not. This principle repeats across the system
(thinking-off at the serving layer §11.3; CI gates on raw LLM calls §11.2).

---

## 6. Schema composition

A deployment's effective schema is **composed**, not monolithic:

```
config/base-schema.yaml      (engine-owned: 7 core types, core namespaces,
                              cross-cutting optional fields, base enums)
        ⊕  extension schema fragments   (each extension may OWN new types/namespaces, §10)
        ⊕  the pack's schema.yaml       (domain types; `extends:` core types with new fields)
        ─────────────────────────────────────────────
        =  the governing schema the validator + write path see
```

- The merge is performed by `scripts/cron/schema_lib.py`; the pack inherits the
  core and declares only its domain on top.
- `extends:` lets a pack add optional fields to a core type without redefining
  it (see `docs/core-types-and-extensions.md`).
- Extension fragments carry an **owner** marker (`ext:<id>`), and composition
  fails loudly on ownership collisions (two parties claiming one type or
  namespace).
- Universal scaffolding fields (`needs_review`, `version`, timestamps, …) are
  engine-side (`_OKF_ALWAYS` in the write server) — no pack redeclares them.

The composed contract is what makes the rest of the machinery generic: every
lane, drain, index, and UI reads types/fields through the same resolved schema.

---

## 7. The cron machinery

The wiki stays healthy because a **fleet of scheduled jobs** (dozens per
deployment; an 88-job fleet is typical for a mature pack) runs under `cron-plus`,
a Hermes plugin the engine pins and auto-installs (`scripts/ensure-runtime.sh`).

### 7.1 Job anatomy and the cheap-by-construction rules

Every job runs as a **separate subprocess** of the gateway with three cost tiers:

- **`no_agent` script lanes** — pure Python, zero LLM cost. Index builds,
  health checks, deterministic generators (e.g. the Wardley map builder).
- **Wake-gated agent lanes** — a cheap selector script runs first, prints a
  digest, and emits `{"wakeAgent": bool}`. The (expensive) agent run happens
  **only** when the gate says there is real work. This contract is upstream-native
  as of Hermes v0.18.0 (the engine used a compatible convention before that).
- **Full agent lanes** — synthesis jobs (briefs, quadrants, grading) that
  always involve a model, with per-lane model routing (§11.1).

Operational contracts learned the hard way (§14):

- **A host timeout is a kill, not a budget.** `HERMES_CRON_SCRIPT_TIMEOUT`
  terminates the subprocess. Any model-calling lane must therefore manage its
  *own* clock (a time budget checked between items), checkpoint partial
  progress, and requeue the tail — never assume it will be allowed to finish.
- **Idempotency via pidfiles.** cron-plus writes
  `/opt/data/cron-plus/pids/<job-id>.pid` (JSON: pid + started_at); a scheduled
  fire while the previous run is alive is skipped. Corollary: recreating the
  gateway container orphans in-flight pidfiles, which then silently block those
  lanes forever — post-recreate pid sweeps are mandatory (§14.5).
- **Queue handoffs between lanes.** When a discovery pass is cheap but the
  per-item work is expensive, the discovery lane writes an explicit queue file
  (e.g. `wiki/.scope-queue.json`) and the worker lane drains it N-per-run. This
  bounds each run *and* avoids re-scanning a 30k-page corpus per tick.

### 7.2 The three cron tiers

Jobs are classified in `config/cron-tiers.yaml`:

| Tier | Schedule + script | Prompt | Ships with |
|---|---|---|---|
| `engine` | engine | none (script-only or generic) | engine, runs on any OKF vault |
| `engine-template` | engine (the wake-gate/selector) | **pack** | engine + pack jointly |
| `domain` | pack | pack | pack |

The `engine-template` tier is the interesting one: the engine knows *when* and
*how* to select work (e.g. "find unprocessed raw items"), but only the pack
knows what judgment to apply — so the script ships engine-side and the prompt is
merged in from the pack at compose time.

### 7.3 Extension-tier jobs

Extensions (§10) contribute complete jobs (schedule + script + prompt) marked
with an `extension:` provenance field. Their prompts are **inlined at compose
time** from the extension's `prompts/*.md` files — meaning a prompt change
deploys via job-recomposition, not script staging (a distinction that matters
operationally; see deploy surfaces, §13.3).

### 7.4 The composition pipeline

```
config/engine-crons.json         (engine half — source of truth)
  +  <pack>/crons/domain-crons.json           (domain jobs)
  +  <pack>/crons/engine-template-prompts.json (prompts keyed by job name)
  +  extension pass                (extension_compose.py over enabled extensions)
  ── scripts/cron_pack_split.py `regen` ──────────────────────────
  =  config/cron-plus-jobs.json    (GENERATED, fail-loud on collisions/cycles)
```

Deploy-time transforms (applied to a temp copy by `deploy-cron-plus-jobs.sh`,
never to the generated source): `@jitter:*` sentinels expand to concrete
schedules for per-install jitter; `@<profile>` model references resolve against
the pack's `model-profiles.yaml` (§11.1); per-lane model overrides from
`.okengine/cron-models.json` apply; `after:` ordering is validated (fail-loud on
cycles). The result lands in the container at `/opt/data/cron-plus/jobs.json`.

Scheduling is timezone-aware (`CRON_TZ`/`TZ`, DST-safe; engine default UTC), and
lanes can defer into cheap model hours (`CRON_DEFER_UTC_HOURS`) for providers
with peak pricing.

---

## 8. The maintenance fleet

What actually runs against a vault, grouped by function (all engine-tier unless
noted; scripts in `scripts/cron/`):

- **Structure**: `build_index_tree.py` (per-directory INDEX pages with paging),
  `reshelve.py` (move misfiled pages to their canonical namespace),
  `reshard_oversized.py` (split buckets that outgrew partition limits),
  `okf_migrate.py` (layout migrations).
- **Health & conformance**: schema-drift checks, the conformance audit
  (validator over the whole corpus → dashboard), `kb_health.py`, source
  freshness, broken-wikilink drains, YAML repair.
- **Tiering**: `build_hot_set.py` + tier refresh maintain hot/warm/cold sets
  from the root schema's `tier` block, so readers and agents can prioritize.
- **Ingest support** (engine-template): raw-batch selectors that wake the agent
  only when unprocessed `raw/` items exist; backfill drains that walk historic
  gaps in bounded batches.
- **Quality lanes** (extension/pack tier): source-quality grading,
  entity/concept backfill, dedupe candidates, prediction grading + audit +
  structural backfill, relevance gating (§10.2), contradiction surfacing,
  timelines, glossary, briefs.

Design invariants: every drain is **bounded per run** and **self-draining**
(processes a batch, stops waking the agent at zero remaining); every generator
is **deterministic and idempotent**; anything that caps output **logs what it
dropped** (silent truncation reads as "done" when it isn't).

---

## 9. Retrieval, graph, and read surfaces

### 9.1 Search — layered, local-first

The wiki is the memory; search is an access path (`docs/kb-tooling.md`):

- **ripgrep** for exact/structural matches (fast, zero infra);
- **qmd** for lexical + hybrid local search over the vault (one index per
  instance);
- **IWE** for the markdown knowledge graph — backlinks ("what links here"),
  reference resolution. Backlink queries warm a server-side graph on first use
  and are cached with a TTL; the graph build is **never** allowed to block a UI
  request (it runs async; the UI renders and fills in). A daily engine cron
  (`backlinks-refresh`) precomputes the inverted graph into a static
  `wiki/.backlinks.json` artifact so the reader/cockpit serve it directly and the
  heavy build runs once per deployment, off the UI containers.

### 9.2 The reader and the cockpit

Two web UIs, both engine-generic, both read-only over the vault mount:

- **`okengine-reader/`** — public-grade reading surface: rendered pages,
  backlinks, provenance/trust strip (sources cited, grounding results, review
  state), metadata panels, exports. Namespaces and types are discovered from the
  vault, never hardcoded.
- **`okengine-cockpit/`** — the operator console: fleet/health dashboards,
  watchlist views (pack-configured via an optional `cockpit:` block in
  `schema.yaml` — the only place domain knowledge enters, as config), page
  click-through, and an **agent Chat tab** that relays to the gateway's API
  server over the private per-pack bridge (wiki-first + write-back through the
  enforced path; the API-server agent's toolset is locked to web/read/write —
  no shell, no code execution).

Both share one auth model: a single `OKENGINE_READER_PASSWORD` (HTTP Basic via
ASGI middleware, constant-time compare, `/healthz` exempt) protects both UIs;
the cockpit must never be laxer than the reader since it is a superset.

### 9.3 Declarative UI panels

Extensions never ship UI code. A generated page **self-declares** its
visualization as data — `panel: {kind: two-axis, x_label, y_label, nodes:
[{label, slug, x, y}]}` — and the UIs own one generic renderer per kind
(`fields`, `two-axis`). Type-bound panels (render a `fields` chip-strip for
every page of type T) are staged as a small JSON binding file at deploy time.
This keeps the extension/UI boundary data-only: a new map, quadrant, or
chip-strip needs zero frontend changes.

### 9.4 Index/graph artifacts

`corpus_indexer.py` builds JSONL indexes over any OKF vault (page → type,
fields, links) consumed by lanes and dashboards, complementing the same-purpose
runtime queries.

---

## 10. The extension system

The engine's answer to "where does optional, reusable capability live?" Neither
engine-core (it would bloat every deployment) nor pack (it would be copy-pasted
across packs). An **extension** is a self-contained directory
(`extensions/okengine.<name>/`) with a manifest, and 17 currently ship with the
engine:

`competitive-analytics, contradictions, critic, dedupe, embeddings, events,
frontier-watch, glossary, grounding, lacuna, messaging-synthesis, predictions,
relevance-gate, timeline, viz`

### 10.1 Anatomy and lifecycle

```
extensions/okengine.<id>/
├── extension.yaml        # manifest: kind, trust, requires.engine, capabilities
│                         #   (read/write globs, network), config knobs, operations
│                         #   (schedule + entrypoint [+ prompt_file] [+ tier/model])
├── schema/<id>.schema.yaml   # OPTIONAL: owned types + namespaces (fragment)
├── <selector>.py         # wake-gates / generators — self-contained (stdlib+yaml only)
├── prompts/*.md          # agent prompts (inlined into job defs at compose time)
└── README.md
```

- **Discovery** scans three roots — engine `extensions/`, pack-bundled,
  operator-local — with collision rules (`docs/design/discovery-spec.md`).
- **Enable-state is vault-level** (`<vault>/.okengine/extensions.yaml`):
  present-on-disk ≠ enabled; one package serves many deployments. `core: false`
  extensions (anything that spends model budget) are opt-in.
- **Composition** (§7.4) folds an enabled extension's operations into the cron
  fleet and its schema fragment under the pack schema, with fail-loud ownership
  checks. `capabilities.write` globs bound what its lanes may touch.
- Extension scripts must be **self-contained** (a CI test enforces
  stdlib+yaml-only imports), so staging a script is copying a file.

### 10.2 Worked examples (what extensions actually do)

- **predictions** — a falsifiable-claim pipeline: candidate detection from
  sources, a `prediction` page per dated claim, grading lanes that revisit
  matured predictions, an accuracy track record, plus audit/remediation drains
  (structural backfill, schema drain) for corpus-wide integrity.
- **relevance-gate** — scope governance. A deterministic pre-scorer
  (term-matching against the pack's `scope` config) flags obviously off-scope
  pages and queues the ambiguous tail; an LLM classify lane drains the queue in
  bounded batches under its own clock. Verdicts are **flag-not-delete**
  (`off_scope: true` + `scope_reason`) — the boundary is data, downstream
  consumers decide filtering. "Uncertain" keeps the page (fail-open on
  retention).
- **viz** — deterministic strategic maps: a Wardley (evolution × value-chain)
  map over the concept graph, positions from concept fields when enriched, else
  graph heuristics (ubiquity percentile / entity coupling); anchor-scoped to an
  operator-chosen neighborhood (e.g. a watchlist page) so the map shows a
  field, not global hub noise. Emits a self-declared `two-axis` panel (§9.3).
- **competitive-analytics** — watchlist-driven market machinery (segments and
  axes are pack config): quadrant syntheses (prose *and* chart data from the
  same evidence), battle cards, acquirer-signal watch, competitor discovery.
- **lacuna** — structural-gap discovery: maps a dense region of the real
  concept/entity graph, finds the cell the geometry implies but nothing
  occupies, requires a *named force* keeping it empty, grades confidence by
  measured graph density around the gap, writes into its own low-trust
  namespace (`needs_review: true`), and optionally emits a testable fill as a
  prediction candidate. Explicitly containment-first: it never writes canonical
  entity/concept pages.
- **completeness** — declared-expectation auditing (the layer above schema
  conformance): the pack declares rules (`when: {type} → expect: field|link|
  companion|freshness`); a deterministic daily audit turns each unmet expectation
  into an explainable gap page (auto-resolving when satisfied, per-rule precision
  surfacing noisy rules), and a paired `gap-drain` resolves the rules the pack
  marks agent-fixable through the enforced write path.
- **actor-risk-ranking** — deterministic target-relative ranking over the
  precomputed backlink graph (§9): explainable edge-set drivers (direct /
  opportunity / capability / intent / recency), confidence counted in distinct
  origin domains (a syndicated report can never lift a band), unknowns that cap
  the band. Dashboard-only; ontology is config (the vendor-risk pack reuses it
  by pointing it at `vendor` pages).
- **glossary** — undefined-term detection over the link graph → `term` pages.
- **contradictions / timeline / frontier-watch / messaging-synthesis /
  critic / grounding / dedupe / events / embeddings** — same shape: a selector
  that finds work cheaply, an agent op with a bounded prompt, pages written
  through the enforced path into owned or core namespaces.

The pattern to notice: **every extension is (cheap deterministic selection) +
(bounded agent judgment) + (typed, provenance-carrying output)**. Nothing free-runs.

---

## 11. Model management and cost discipline

### 11.1 Routing

A deployment declares a **default chain** and per-lane overrides:

- The chain escalates on failure and **never de-escalates**: local mid-size
  model → cloud flash tier → cloud pro tier. (Design rule: *fallbacks escalate
  capability, never weaken it* — a weaker fallback converts transient failures
  into silent quality loss.)
- **Per-lane pins** (`.okengine/cron-models.json`, plus `model:` on extension
  ops): bulk/mechanical lanes run local models; synthesis lanes (briefs,
  quadrants, grading, lacuna) pin to a capable cloud model.
- **Model profiles** (`model-profiles.yaml`, referenced as `@<profile>` in job
  defs) bundle endpoint + model + context-length so a lane can switch serving
  hosts atomically; undefined profiles fail the deploy.
- The interactive chat agent's model is pinned independently of the bulk
  default (runtime patch 07).

### 11.2 Call discipline

All direct model calls from engine/pack scripts go through one client library
(`scripts/cron/llm_lib.py`): explicit timeouts, retry policy, truncation
detection that **raises** (a truncated classification is an error, not a
value), a `classify()` helper that returns `label | uncertain`, and
reasoning-effort defaults (§11.3). A CI gate (`tests/test_llm_call_discipline.py`)
**forbids raw chat/completions calls** outside the library — the "global
decision" is enforced, not requested (same principle as §5).

### 11.3 The thinking-off stack (a case study in boundary enforcement)

Local reasoning models default to thinking ON at the endpoint, and thinking
silently consumes the output budget of short structured calls (a 90-item
classification once returned 88 false-"uncertain" verdicts because reasoning ate
`max_tokens` before the answer). The decision "thinking off unless explicitly
wanted" is enforced at **three layers**, because any single layer leaves a
client class uncovered:

1. **Serving layer (shim)** — a proxy in front of the model host injects the
   off-switch into *bare* requests on both API styles (`think:false` on the
   native path, `reasoning_effort:"none"` on the OpenAI-compatible path);
   explicit values pass through untouched. Catches every LAN client.
2. **Gateway provider profile** — Hermes's `custom` provider sends the
   off-switch by default; per-lane config can re-enable.
3. **Script layer** — `llm_lib` sends it per call; the CI gate keeps scripts
   inside `llm_lib`.

### 11.4 Cost levers

`no_agent` lanes for everything deterministic; wake-gates so agent lanes run
only on real work; bounded batches everywhere; queue handoffs to avoid re-scans;
off-peak deferral for peak-priced providers; a budget-guard lane watching spend;
local-first search (§9.1) so retrieval costs nothing.

---

## 12. Packs

### 12.1 Anatomy

```
okpack-<domain>/
├── schema.yaml            # domain types (+ extends of core), partitioning, hot_set,
│                          #   optional cockpit: block, optional scope: (relevance-gate)
├── CLAUDE.md              # persona: ingest workflow, curation judgment, voice
├── engine.version         # pins: engine version + hermes_pin
├── feeds/*.opml           # source feeds per lane
├── crons/domain-crons.json            # domain jobs
├── crons/engine-template-prompts.json # prompts for engine-template lanes
├── data/*.yaml|json       # rosters, curated fields, watchlists
├── wiki/ , raw/           # (live deployments; a library pack ships seeds/examples)
├── docker-compose.yml     # the instance stack (generated by `framework init`)
└── .env                   # secrets + knobs (never committed)
```

### 12.2 The framework CLI

`scripts/framework.py init` scaffolds a new pack (compose stack with
`--port-offset` for multi-instance hosts, runtime dir, config template);
`framework.py validate` is the pre-deploy gate: schema composes, crons merge,
extension state resolves, bind/auth sanity (§13.2). `framework extensions
enable|disable|list|validate` manages vault-level extension state.

### 12.3 Pack repos and reuse

Packs live in their own repos/libraries, versioned independently of the engine,
pinning the engine version they were validated against. A generic pack (e.g. a
competitive-intelligence pack) ships **sample market kits** — curated feed sets
+ watchlist skeletons per example market — so standing up a new deployment is
configuration, not authoring. Authoring guidance and the accumulated failure
modes are documented in `docs/authoring-a-pack.md` and
`docs/pack-building-challenges.md` (§14.7).

---

## 13. Deployment topology and trust

### 13.1 The instance model

**engine (one codebase) → instance (one vault + one stack + one cron fleet) →
pack/domain (the content layer).**

An instance is a compose stack of four containers on a private per-pack bridge:

| Service | Role | Notes |
|---|---|---|
| `gateway` | Hermes runtime: agent, cron ticker, API server | vault mounted RW; env from `.env` |
| `okengine-mcp` | read MCP (HTTP, bearer token) | vault RO |
| `okengine-reader` | reading UI | vault RO |
| `okengine-cockpit` | operator UI + agent chat relay | vault RO; reaches gateway by service name |

Multiple instances co-tenant one host via port offsets. One vault can host
multiple *related* domains via walk-up schemas (model A); a distinct task,
audience, or trust boundary gets its own instance (model B). Co-installing a pack
alongside others is automated by `framework install-domain` (a collision
preflight over what actually lands, then key-based merges of the pack's types,
namespaces, rules, lanes, and persona — idempotent, dry-run by default). **Public vs
private content never share an instance** — one search index, one reader, one
cron fleet, one `.env` per instance makes co-mingling a leak by construction.

### 13.2 Trust defaults

Local-first: host ports bind `127.0.0.1` until `OKENGINE_BIND` is widened, and
widening **fails validation** unless real secrets are set
(`OKENGINE_READER_PASSWORD`, non-default `OKENGINE_MCP_TOKEN`). A pack declares
its trust level; `trust: private` fail-safes to mandatory auth. Container
hygiene rules (engine-wide): `restart: unless-stopped` + bounded
`restart_policy` (never `restart: always`), explicit CPU/memory limits on every
service.

### 13.3 Deploy surfaces (what kind of change needs what)

| Change | Deploy action |
|---|---|
| engine cron script / extension script | `deploy-cron-scripts.sh` (file staging, no restart) |
| job defs / extension **prompts** / schedules / models | `cron_pack_split regen` via `deploy-cron-plus-jobs.sh` |
| gateway env (`.env` knobs) | container **recreate** (env is start-time) |
| baked code (write server, reader, cockpit images) | image rebuild + recreate |
| Hermes pin | full upgrade procedure (§4.2) |

`ensure-runtime.sh` runs before any compose-up: seeds the runtime dir, verifies
uid-writability, installs the pinned cron-plus plugin, and unlocks known
migration hazards (§14.5).

---

## 14. Operational lessons (field notes)

These are the empirical results — the part a research reader can't get from the
design docs. Each was paid for in production.

### 14.1 Partitioning is a contract, not a convention

Alphabet-partitioned entities (`entities/<letter>/<slug>.md`) with **bare-slug
wikilinks** is the canonical layout; per-type directories re-shard badly and
split link forms. The deeper lesson: **any tool that resolves or counts links
must handle every historical layout present in the corpus** — a link-counting
regex blind to one sharding form silently mis-weighted 94% of a live vault's
links, which then silently degraded every downstream consumer (selection caps,
maps, in-degree heuristics) while looking plausible. Migrations compound daily:
every day a legacy layout persists, more pages are written into it.
(`docs/entity-partitioning.md`, `docs/sharded-scan-discipline.md`)

### 14.2 A host timeout is a kill, not a budget

Model-calling lanes that relied on the subprocess timeout as an implicit budget
lost all partial work on every overrun. The fix pattern (now standard): the lane
owns its clock — check a time budget between items, checkpoint each completed
item immediately, requeue the remainder. Related: batch sizes are chosen to fit
inside the timeout at observed per-call latency, and queue handoffs (§7.1) keep
discovery cost out of the worker lane.

### 14.3 LLM verdicts need explicit failure semantics

Truncation must raise (a cut-off answer is not an answer); classifications
return `label | uncertain`, and the consumer decides what `uncertain` means
(retention gating fails open; destructive actions fail closed). Reasoning-model
thinking is a silent output-budget thief on short structured calls (§11.3).

### 14.4 Filtering upstream destroys information downstream

Early scope/relevance designs deleted or blocked off-scope content. The settled
principle: **light filtering at the source; judgment at the consumer** — flag
with a reason (`off_scope`, `needs_review`, reliability grades) and let each
consumer decide its threshold. Same shape as the "global decisions must be
enforced globally" principle (§5, §11) — both are about putting policy at the
right boundary.

### 14.5 Silent-death modes are the expensive ones

The failures that cost real time were the quiet ones:

- A **read-only config file** (444 by design) made a runtime config migration
  die *non-fatally* — the gateway "restored and continued" on the old config
  version and the **cron ticker silently stopped**; 31 jobs went overdue for
  six hours with zero errors surfaced. Fix: the deploy path pre-unlocks the
  file; the upgrade gauntlet checks ticker liveness explicitly.
- **Container recreation orphans in-flight pidfiles**, after which those lanes
  skip every future fire as "previous run still active" — forever, silently.
  Fix: post-recreate pid sweeps (liveness-checked, never age-based).
- A `:latest` image tag during a staged rollout means a routine `up -d` on a
  not-yet-rolled deployment silently upgrades it mid-soak. Fix: restart-only
  discipline on pending deployments, deliberate recreates.

The meta-lesson: **verify observed-working in the real deployment** — tests
green + deploy script exited 0 is not "live" (§4.2's gauntlet exists because of
this).

### 14.6 Upgrades want a guinea pig and a gauntlet

Pin bumps roll one low-stakes deployment first, run the full gauntlet (§4.2)
including a *real* write through the enforced path, soak, then roll the rest.
Patch rebases are tested in isolation (3-way onto a stock tree) before the
image build. Every incident goes into the upgrade record — the next upgrade's
pre-flight checklist is the last upgrade's incident list.

### 14.7 Pack-building has recurring structural failure classes

Documented in `docs/pack-building-challenges.md`; the recurring five: schema
permissiveness that lets junk conform; stale imported content masquerading as
current signal; empty/dead feeds that make lanes look healthy while ingesting
nothing; tool/name drift between pack docs and engine reality; and version-pin
drift (packs asserting engine/runtime versions they were never validated
against). Each now has either a validator check, an audit lane, or a checklist
entry.

### 14.8 Time-of-day and locality matter operationally

Deployments run in a local timezone (cron semantics, DST); model serving is
LAN-local for bulk work (cost, latency) with cloud escalation for synthesis;
peak-priced cloud hours are avoided by deferral windows. None of this is
architecturally deep, but all of it shows up in the bill and the freshness
dashboards.

---

## 15. Known limits and open directions

Stated plainly, because a research paper should know where the edges are:

- **Composition needs disjoint ownership.** Multiple packs share one vault via
  `kind: bundle` / `framework install-domain` behind a coinstall preflight (types,
  namespaces, crons), but two packs that claim the same type or namespace is a
  blocking conflict — arbitrary mutually-untrusted packs aren't sandboxed to share a
  vault automatically (`docs/design/composable-okpacks.md` tracks the harder cases).
- **Federation.** A two-instance pattern — a protected vault consulting a
  curated public vault as a lookup peer via the read-MCP — is designed
  (trust-asymmetric, read-only edges) but not yet built.
- **Graph density is the ceiling for structural features.** Lacuna's gap
  confidence and viz's maps are only as good as the concept/entity link fabric;
  young vaults produce sparse (though honest) structure. Enrichment lanes
  (concept fields, entity cross-links) are the lever.
- **Layout migrations at scale.** Corpus-wide re-partitioning (tens of
  thousands of pages, link rewrites, dedup during moves) is scoped as a batched,
  LLM-assisted migration with checkpoints — the standing example that the wiki
  is a *database with prose semantics* and needs database-grade migration
  discipline.
- **Evaluation.** The system's honest metrics today are operational
  (conformance rate, freshness, prediction track record, lane health). Corpus
  *quality* evaluation — is the compiled knowledge good? — currently rests on
  the prediction-grading loop, provenance discipline, and human review flags;
  a stronger intrinsic evaluation story is open work.

---

## Appendix A — repo map

```
engine-manifest.yaml           the layer boundary, versions, pins (start here)
config/base-schema.yaml        engine-owned OKF core (7 types)
config/cron-tiers.yaml         engine/engine-template/domain classification
config/engine-crons.json       engine half of the fleet (source)
config/cron-plus-jobs.json     GENERATED fleet artifact — never hand-edit
tools/schema_validator.py      the conformance contract
okengine-mcp/write_server.py   the enforced write path (G1)
okengine-mcp/server.py         read MCP (search/get_page/find_references)
okengine-reader/               reading UI          okengine-cockpit/  operator UI
scripts/cron/                  engine + engine-template lanes, schema_lib, llm_lib
scripts/cron_pack_split.py     fleet composition   scripts/extension_compose.py
scripts/framework.py           pack CLI (init/validate/extensions)
scripts/build-engine-image.sh  pin-verified image build
scripts/ensure-runtime.sh      pre-compose runtime seeding + hazard unlocks
scripts/deploy-cron-*.sh       deploy surfaces
extensions/okengine.*/         the 15 first-party extensions
patches/                       the 7 carried Hermes patches + apply.sh
docs/                          per-topic depth (overview, guides, design specs,
                               upgrade records, operational lessons)
tests/                         regressions — every fix ships one
```

## Appendix B — reading order for a deep dive

1. This document.
2. `docs/okf/guide-1-agent-wiki-pattern.md` — the concept defense in full.
3. `docs/engine-domain-boundary.md` + `engine-manifest.yaml` — the boundary, precisely.
4. `okengine-mcp/write_server.py` — read the enforcement code itself; it is short.
5. `docs/design/extension-system.md` + one extension end-to-end
   (`okengine.relevance-gate` is the best single exhibit: config → prescore →
   queue → budgeted classify → flags → audit dashboard).
6. `scripts/cron_pack_split.py` — the composition pipeline.
7. `docs/okf/guide-4-scaling-to-100k.md` + `docs/entity-partitioning.md` +
   `docs/common-issues.md` — the scale and failure-mode record.
8. A pack repo (`okpack-cti` or the competitive pack) — see the domain layer
   with the engine abstractions fresh in mind.
