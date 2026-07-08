# Building an OKEngine domain pack

A **domain pack** is a self-contained directory the OKEngine framework treats as a
*vault*: it ingests open feeds, compiles them into a cross-linked knowledge graph, and
maintains that graph over time. The pack dir **is** the vault — the engine reads the
pack's `schema.yaml` for layout/rules and the pack's `CLAUDE.md` for domain judgment,
and never hardcodes either.

This guide is the **conceptual companion** — the *decisions* you make when authoring
a pack. For the step-by-step walkthrough (scaffold → fill → validate → deploy) and
the full v0.2.0 contract (`pack.yaml`, ids, composition, the conformance profile),
follow **[`docs/authoring-a-pack.md`](../../docs/authoring-a-pack.md)** in the engine
repo. The reference implementation is **okpack-cti** (security threat-intel);
examples below point at it.

---

## 1. The contracts

A pack is governed by a small set of declarative files, deliberately split:

- **`schema.yaml` — the machine-readable contract.** Page *types* + required fields
  (incl. `id_authority`/`owner` for identity), how each namespace *partitions* on
  disk, the load-first *hot_set*, the *permissions* matrix (what the agent may
  write), the *review* trust model, and the hot/warm/cold *tier* rules. The engine's
  write path enforces this; a non-conformant write is rejected.
- **`CLAUDE.md` — the human-judgment contract.** The domain *voice*, the *ingest /
  curation workflow* the cron agents follow at runtime, and the editorial rules
  (what's worth a page, how to score sources, what never to write). `schema.yaml` says
  *what shape is legal*; `CLAUDE.md` says *what's worth writing and how to think*.
- **`pack.yaml` — the identity contract.** `name`/`version`/`trust` and the `owns`
  block (the types + namespaces this pack defines) — required for composition and
  `framework validate`. Two packs composed into one vault must own disjoint
  types/namespaces; see `docs/authoring-a-pack.md` §2a.

Rule of thumb: anything a machine must enforce → `schema.yaml`. Anything that needs
judgment → `CLAUDE.md`.

## 2. Layout

```
schema.yaml            the machine contract (below)
pack.yaml              identity + ownership (name/version/trust/owns) — for composition
CLAUDE.md              persona + ingest workflow the cron agents read at runtime
engine.version         the engine tag this pack is pinned to
feeds/feeds.opml       ACTIVE sources — EMPTY by default (opt in)
feeds/feeds.opml.example  the curated suggestion list operators copy from
crons/
  domain-crons.json    pack-specific crons (feed-fetch + a daily brief)
  engine-template-prompts.json   ingest-lane prompts the engine drives
  scripts/             pack scripts co-deployed to the engine cron host
docker-compose.yml     gateway + okengine-reader + okengine-mcp
.env.example           secrets + delivery (copy to .env; never commit)
validate.py            offline parse + cross-consistency checks (+ CI)
wiki/                  THE vault — populated by ingest, bucketed by namespace
raw/                   runtime: feed_fetch output (gitignored)
```

## 3. Designing the taxonomy (`types`)

This is the one genuinely domain-specific decision. A *type* is a kind of page; each
declares the **identity / load-bearing fields** that must be present (keep the
`required` list minimal so the write-gate never rejects a legitimately-shaped page —
everything else is optional via `common_optional`).

**The OKF core is engine-owned — you inherit it, you do not declare it** (okengine#90
P2). The universal spine lives in the engine's `config/base-schema.yaml` and is merged
*under* your `schema.yaml`, so every pack shares the same `source` / `concept` /
`prediction` / `finding` / `dashboard` / `briefing` / `trend` types and the core
namespaces (`entities`, `sources`, …). That shared core is what lets two packs compose
into one graph; **re-declaring a core type breaks composition** (and `framework
compose-preview` flags it). So your `types:` block declares **only your domain types** —
do *not* copy the spine in.

You inherit (from the core — for reference, don't re-declare):

| Type | Role | Core required |
|------|------|------------------|
| `source` | a scored ingest item (provenance + dedupe) | `type, published` |
| `concept` | a cross-cutting pattern / segment / cluster | `type` |
| `prediction` | a falsifiable, dated forward claim | `type, status, confidence, subject, resolves_by` |
| `finding` | analyst synthesis (often human-only — see §5) | `type, status` |
| `dashboard` | a generated view / index | `type, title` |
| `briefing` | a daily/weekly synthesized brief | `type, title, published` |
| `trend` | a dated, directional shift | `type, title, period, direction` |

Need an extra field on a core type (e.g. a `source_kind` on `source`)? Use `extends:`
— **additive and OPTIONAL only**; a pack may never add a *required* field to a core type
or otherwise tighten it (a stricter shared `source`/`finding` would reject another pack's
pages). Validate the value with a `field_enums` entry, and enforce presence in your
ingest workflow, not the gate. The cross-cutting optional fields the core already ships
(`tlp`, `source_kind`, `severity`, `publisher`, `reliability`, `credibility`,
`sensitivity`) are yours for free, and their base enums are extensible. Full model,
rules, and examples: **[`docs/core-types-and-extensions.md`](../../docs/core-types-and-extensions.md)**.

Then add your **domain entity types**. okpack-cti uses `host, ioc, threat-actor,
malware, campaign, technique, vulnerability, detection, tool, software`. A finance pack
might use `institution, instrument, fraud-scheme, regulation, actor`. For each, pick
the **identity field** that makes the page addressable (`ioc.value`, `technique.mitre_id`,
`vulnerability.cve_id`). Prefer canonical external IDs where a standard exists.

**Create a page only when the subject is worth tracking over time.** One-off mentions
stay listed on the source page; the value is the *compounding* graph, not coverage.

## 4. Partitioning, hot_set, tier

- **`partitioning`** — how each namespace buckets on disk so a directory never grows
  unbounded. `by-letter` (entities/concepts), `by-date` (sources, briefings),
  `flat` (predictions/findings). Set `reshard_over` so the engine reshelves when a
  bucket gets large.
- **`hot_set`** — the agent's load-first working set (compiled to `wiki/HOT.md`):
  recent sources, recently-updated entities, open predictions. Tune `days`/`cap`.
- **`tier`** — hot/warm/cold, *derived at query time* from a date field (never stored).
  Each namespace names the date field it ages on (`sources.published`,
  `entities.updated`, `predictions.resolves_by`).

Keep the namespace set in `partitioning`, `tier`, and the on-disk `wiki/<ns>/` dirs in
sync — `validate.py` checks that crons only write to declared namespaces.

## 5. Permissions — human-only gates (`permissions`)

The default is `{create: true, update: true, delete: false}` (the agent writes; never
hard-deletes — it tombstones). A namespace can be marked **human-authored** by setting
`{create: false, update: false}`. okpack-cti gates `findings/` this way: analysts
author findings via git; the MCP write path *refuses* agent writes there. Use this for
any namespace where a machine assertion shouldn't stand without a human.

## 6. Review trust model — flag, not gate (`review`)

Let the agent assert *numeric* or `low`/`med`/`high` confidence freely. Reserve a few
**categorical verdicts** (e.g. `confirmed`, `refuted`, `false-positive`) as
*review-flagged*: asserting one lands the write but stamps `needs_review: true` and
queues it for a human. This mirrors the discipline that a hard verdict is a human call —
without blocking the agent's normal work.

## 7. Feeds — ship inert (safe by default)

**A pack must ship with no active sources and no running crons.** A public pack may be
cloned by many operators; if it shipped feeds enabled on a fixed schedule, every
deployment would hit the same origins at the same instant — a thundering herd against
the upstream publishers. So:

- `feeds/feeds.opml` ships **empty**. The curated suggestion list lives in
  `feeds/feeds.opml.example`; the operator reviews it and copies entries to opt in.
- Do **not** encode per-source quality in the OPML — reliability/credibility/TLP are
  assigned **at ingest time** by judgment (see your `CLAUDE.md` rubric).
- Keep the `.example` header's feed count honest (`validate.py` blocks on drift;
  `--fix` repairs it). Re-probe before enabling.

## 8. The cron fleet — disabled by default

Two pack-defined crons (`crons/domain-crons.json`), both shipping `enabled: false` with
a deliberate **never-fires** placeholder schedule (`0 0 30 2 *` — Feb 30, valid cron
that can never run):

- **feed-fetch** — a *pure script* (`no_agent: true`): pulls the OPML into `raw/<domain>/`.
  No model cost.
- **daily brief** — an agent cron: reads `wiki/HOT.md`, writes a terse dated brief to
  `wiki/briefings/<date>.md`.

The operator enables a cron by setting `enabled: true` **and choosing their own `expr`**
— a **random minute**, never `:00`, interval ≥ ~2h — so deployments desynchronize. Ship
the suggested cadence in the README, not as a working schedule. `validate.py` fails the
build if a committed cron is `enabled: true`.

Plus the **engine-driven ingest lane** (`crons/engine-template-prompts.json`): prompts
the engine schedules to compile raw→entities, score sources, classify types, enrich
thin pages, and grade predictions. All ingest crons are **local-only** (no web tools —
shared paid budget). Start from okpack-cti's prompts and swap the domain nouns.

> **Engine-side, tracked:** two deeper thundering-herd fixes live in the engine, not the
> pack — (1) `feed_fetch.py` conditional GET (`ETag`/`If-Modified-Since` → cheap `304`s)
> + `429`/`503` backoff (okengine#2), and (2) a native `jitter_s` schedule field so
> desync isn't a per-pack convention (hermes-cron-plus#1). Until `jitter_s` lands, packs
> jitter by hand (random cron minute, never `:00`).

## 9. Deploy & ports

The pack dir is the vault, mounted into three services (gateway, reader, mcp). Host
ports are offset (`+{{PORT_OFFSET}}`) so multiple packs coexist on one host. Each
service has explicit resource limits, `restart_policy` backoff, and (reader/mcp) a
healthcheck. Full deploy steps live in the rendered pack's `README.md`.

**Trust enforcement (okengine#90 P4a).** A `trust: private` pack's reader **fail-closes**:
if it's exposed beyond loopback (`OKENGINE_BIND` != a loopback address) without an
`OKENGINE_READER_PASSWORD` set, it refuses to serve rather than leak private content.
`framework validate` is trust-aware to match — a private pack exposed without a password
is a **FAIL**; a public one is a **WARN**. So set `trust:` honestly in `pack.yaml`, and if
you expose a private vault on the LAN, set a real `OKENGINE_READER_PASSWORD`.

## 10. Validate

`validate.py` runs offline (no engine, no Docker) and in CI:

- parses `schema.yaml`, both cron JSONs, and `feeds.opml`;
- checks every `wiki/<ns>/` a cron writes to is a declared namespace;
- checks `type:` frontmatter the crons write maps to a real schema type;
- validates human-only gates *generically* — any namespace declared
  `create:false, update:false` must keep that exact shape; a pack with no gate is
  legal (set `REQUIRED_HUMAN_ONLY` to assert a specific one, as okpack-cti does for
  `findings`);
- checks schema cross-section drift — every `tier`/`hot_set` namespace must exist in
  `partitioning`;
- **enforces the safe default** — fails if a committed cron is `enabled: true`;
- blocks on a stale feed count in `feeds.opml.example` (`--fix` repairs it).

The engine's own `framework validate <pack>` (in okengine) is a deeper, separate
check — strict about real requirements: schema/CLAUDE/runtime-config, a pinned
`engine.version`, a substantive `README.md` **with a Deploy section**, a `LICENSE`,
no unrendered `{{tokens}}`, well-shaped crons (real schedule + action, non-empty
engine-template prompts), valid `pack.yaml` enums, feeds (`--probe-feeds`). See
okengine `docs/authoring-a-pack.md` §6 for the full FAIL/WARN list. `validate.py`
is the repo-local, zero-dependency gate that runs in the pack's CI.

Wire it into CI (`.github/workflows/validate.yml`) so drift can't merge.

---

### Authoring principles (carry these into `CLAUDE.md`)

- **Filter, not feed.** Compress signal into structured pages; don't mirror the feed.
- **Compounding KB, not RAG.** Compile once, then *maintain* — new sightings append to
  an entity, never spawn a duplicate page.
- **Assume expertise.** Skip 101 explanations; capture the specific, actionable detail.
- **Link generously, but only to pages that exist (or you create in the same batch).**
  The graph is the value.
- **Ship inert.** No active sources, no running crons. A pack a stranger clones must do
  nothing until they opt in — both to respect upstreams and to avoid surprise cost.
