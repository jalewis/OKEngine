# Engine ↔ Domain-Pack Boundary Spec

This is the architecture spec for the engine/pack boundary: which code is the
domain-agnostic engine, which is the per-deployment pack, and how the two combine
at deploy time. It is the file-layer companion to the runtime view in
[`okf/deployment-topology.md`](okf/deployment-topology.md).

## 1. Three layers, not two

The codebase is a **pinned Hermes-Agent** (consumed as a dependency, not forked)
with OKEngine's KB machinery layered on top. The engine ships no domain content of
its own (the reference pack, **okpack-cti**, is a separate repo). The honest
decomposition is three layers:

| Layer | What it is | Lifecycle |
|---|---|---|
| **Runtime** | The pinned Hermes-Agent (consumed as a dependency, not forked): `gateway/`, `agent/`, `hermes_cli/`, `cron/`, `plugins/`, `providers/`, `acp_*`, `tui_gateway/`, `ui-tui/`, `locales/`, `skills/`, `tools/` (mostly), `Dockerfile`, `docker-compose.yml` | Pinned at a fixed tag; `git fetch upstream` to bump the pin; we patch sparingly. |
| **Engine** | OKEngine's domain-agnostic value-add: LLM-wiki governance/maintenance, OKF-compatible validation, KB-maintenance machinery (`engine` + `engine-template` crons), retrieval/graph integration, deploy tooling | Ships with the framework; same for every deployment. |
| **Domain pack** | Everything domain-specific: the vault (schema + content + persona), feeds, domain data files, `domain` crons + `engine-template` prompts, secrets | One per deployment; the reference pack is **okpack-cti** (separate repo). |

The framework = **Runtime + Engine**. A deployment = framework + **one pack definition OR bundle** (a bundle composes several ownership-disjoint packs into one vault; `install-domain` can co-install compatible domains).

## 2. File/dir → layer mapping

### Engine (domain-agnostic — ships with the framework)
| Path | Role |
|---|---|
| `tools/schema_validator.py` | OKF conformance contract (validator + write-guard). Lives inside the upstream `tools/` dir today. |
| `scripts/cron/` — the `engine` + `engine-template` tier scripts (`config/cron-tiers.yaml`) | wake-gates / selectors / drains: `reshelve.py`, `reshard_oversized.py`, `build_index_tree.py`, `build_hot_set.py`, `okf_migrate.py`, `schema_*_drain.py`, `repair_*`, `normalize_entity_schema.py`, source-hygiene + index/health refreshers, etc. (forecasting/event/classification jobs are pack-supplied) |
| `scripts/cron/kb_search.py`, `kb_graph.py`, `kb_health.py` | qmd + IWE integration (retrieval + graph) |
| `scripts/cron/corpus_indexer.py` | JSONL index builder over any OKF vault |
| `scripts/backfill_source_fields.py`, `backfill_typeless_type.py`, `normalize_publishers.py`, `restore_clobbered_tags.py`, `render-dataview-blocks.py` | generic corpus-maintenance helpers |
| `scripts/cron-plus*.sh`, `deploy-cron-*.sh`, `dump-cron-plus-jobs.sh` | deploy/ops tooling |
| `okengine-reader/` | generic vault reader UI (markdown + backlinks + PDF) — no domain knowledge |
| `config/cron-tiers.yaml` | the engine/domain cron manifest (this layer's own contract) |
| `config/base-schema.yaml` | the **engine-owned** universal OKF core (okengine#90 P2): core types (`source`/`concept`/`prediction`/`finding`/`dashboard`/`briefing`/`trend`), core namespaces, the cross-cutting optional fields + base enums, and the global toggles. Merged *under* every pack by `scripts/cron/schema_lib.py`; a pack inherits it and declares only its domain on top |
| `config/config.yaml.template` | deployment-critical Hermes keys (reference) |
| `docs/okf/`, `docs/kb-tooling.md`, `docs/okf-alignment.md` | engine docs |

### Domain pack (per-deployment — illustrative; the reference pack is okpack-cti, a separate repo)
| Path | Role |
|---|---|
| **Vault repo** — `schema.yaml` | the structural contract: `partitioning` + `hot_set` + `types` — but **domain types only**. The universal OKF core now lives engine-side in `config/base-schema.yaml` (okengine#90 P2) and is merged under the pack; a pack inherits it and uses `extends:` to add optional fields to a core type (see [`core-types-and-extensions.md`](core-types-and-extensions.md)) |
| Vault `wiki/`, vault `CLAUDE.md` | content + persona/curation rules (the domain's knowledge + voice) |
| Vault `wiki/<subdomain>/schema.yaml` | a *second* domain in the same vault (the walk-up pattern) |
| `pack/feeds/<lane>.opml` | feeds (domain sources) |
| `pack/data/*.yaml`, `pack/data/*.json` | domain data tables (rosters, curated entity fields, etc.) |
| `scripts/cron/` — the `domain` tier scripts | domain ingest / digest / analysis |
| `domain` cron *definitions* + the **prompts** for the `engine-template` jobs | the domain half of `cron-plus-jobs.json` |
| the pack's `.env` (runtime data dir) | secrets + delivery targets |

### Runtime (the pinned dependency — out of scope for the boundary)
`gateway/`, `agent/`, `hermes_cli/`, `cron/`, `plugins/`, `providers/`, the
upstream parts of `tools/` and `scripts/`, `Dockerfile`, `docker-compose.yml`,
all `RELEASE_*.md`. (`agent/usage_pricing.py` is a runtime patch, not engine.)

## 3. Cron tiers and the generated `cron-plus-jobs.json`

Cron jobs are classified into three tiers in `config/cron-tiers.yaml`:

- **`engine`** — domain-agnostic machinery (schedule + script, no domain prompt);
  ships unchanged and runs on any OKF vault.
- **`engine-template`** — the engine ships the selector/wake-gate **script**; the
  pack supplies the agent **prompt**.
- **`domain`** — fully pack-supplied (full defs).

The deployed runtime cron file is **generated**, not hand-bundled. One file
(`config/cron-plus-jobs.json`) ultimately carries **schedule + script ref +
prompt + model** for every job, but it is produced by merging the two sources:

```
engine cron defs        (tiers: engine — schedule+script, no domain prompt)
  +  domain-pack cron defs (tiers: domain — full defs; engine-template — prompts only)
  ─────────────────────────────────────────────────────────────────────
  =  cron-plus-jobs.json   (the deployed, merged runtime file)
```

So an `engine-template` job's schedule + script come from the engine; its prompt
comes from the domain pack and is merged at deploy time. `config/cron-tiers.yaml`
is the key that drives the merge; `scripts/cron_pack_split.py` performs it. The
prompts themselves live in the pack as an `engine-template-prompts.json` keyed by
job name, and `merge` re-attaches each prompt onto the engine's script def by name.

`cron-plus-jobs.json` is a **generated artifact** — never hand-edited. Edit the
source (`config/engine-crons.json` for engine crons; the pack for domain crons),
then regenerate.

## 4. Known intermingling

A few engine concerns currently live inside upstream-owned directories. They work
as-is; isolating them further is optional cleanup, not a correctness requirement:

- **`tools/schema_validator.py` sits in the upstream `tools/` dir** — could move to
  an engine namespace (e.g. `engine/okf/`) so an upstream `tools/` change can't
  collide and the engine is fully self-contained.
- **`scripts/` mixes engine + upstream** (`install.sh`, `release.py`, `run_tests.sh`
  are upstream; `deploy-cron-*.sh` are engine).
- **`config/` mixes engine + domain** (`cron-tiers.yaml` is engine; `*.opml` and
  the domain data tables are domain — domain data belongs in the domain pack).
- **`scripts/cron/` holds all three tiers in one dir** — the tier manifest already
  classifies them; physical separation can follow or stay flat + manifest-driven.

## 5. Repo model

Two repos: the **engine** is its own versioned repo; the **vault** (extended) is
the domain pack and pins an `engine.version`. This is a standard image-vs-config
split, so an engine upgrade is not a pack rewrite.

The engine/pack boundary is declared in `engine-manifest.yaml`. The pinned
Hermes-Agent stays at the repo root for clean upstream tracking, so the manifest
*is* the logical boundary (rather than a physical `runtime/` move that would
complicate upstream merges). An instance can additionally host multiple domains in
one vault via a walk-up `schema.yaml` per subtree.

Suggested physical layout for a pack (illustrative):

```
domain pack (its own repo)
├── schema.yaml         # partitioning + hot_set + types  (the contract)
├── wiki/               # content
├── CLAUDE.md           # persona / curation rules
├── feeds/              # *.opml
├── data/               # rosters, curated entity fields, ...
├── crons/
│   ├── domain/         # the domain scripts + defs
│   └── prompts/        # the engine-template prompts (merged onto engine scripts)
└── .env.example
```

The reference pack is **okpack-cti** (a security-focused LLM-wiki pack,
maintained in its own repo).
