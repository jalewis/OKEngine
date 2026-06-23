# Authoring a domain pack

This is the **end-to-end walkthrough** for building a pack from nothing to a
running deployment: scaffold → fill the schema → write the persona → wire feeds →
add crons → validate → deploy → iterate. It ties together the pieces documented
elsewhere:

- [`deploy-a-new-domain.md`](deploy-a-new-domain.md) — §1 is the pack **spec**
  (layout + the `schema.yaml` blocks); §2 is the **deploy** quickstart this guide
  hands off to.
- [`engine-domain-boundary.md`](engine-domain-boundary.md) — what the engine owns
  vs. what your pack owns, and the three cron tiers.
- [`okf/guide-2-building-an-agent-vault.md`](okf/guide-2-building-an-agent-vault.md)
  — the deeper *why* behind schema/frontmatter, the confidence model, and the
  agent behavioral contract.
- [`okf/deployment-topology.md`](okf/deployment-topology.md) — whether the pack
  runs as a new domain in an existing vault or its own instance (a **public** pack
  is always its own instance).
- [`okf/okengine-conformance-spec.md`](okf/okengine-conformance-spec.md) — the
  **normative** conformance profile (MUST/SHOULD/MAY) your pack is held to: page
  format, frontmatter, ids, tombstones, review flags, tiers, reserved files, and
  the write-governance contract. Read this if you want to know exactly what
  "conformant" means.

> **What a pack is.** `a live deployment = OKEngine @ a pinned Hermes + ONE pack`.
> The engine ships zero domain knowledge; everything domain-specific — types,
> persona, feeds, crons, content — lives in the pack. Swapping the pack changes
> the brain with no engine code change.

---

## 0. Prerequisites

- An OKEngine checkout (this repo) at a pinned release. `ENGINE_DIR` below is its path.
- Docker + a host user. The deploy uses a **fixed** uid (10000 recommended), never
  `$(id -u)` — a hardcoded build-host uid bakes that host into the image.
- At least one model-provider key, and (for delivery) a Telegram bot token — or
  `--delivery local` to skip delivery.

---

## 1. Scaffold

Two ways to start, both ending at a validated, inert pack dir:

- **`init`** (this section) — a blank pack from the skeleton.
- **`pull`** — an *existing* published pack from the catalog, ready to adapt:
  `framework list` to browse, then `framework pull <name> ../my-brain` (resolves a
  catalog name / `okpacks-library:<pack>` / `owner/repo[:subdir]` / git URL, strips
  any committed runtime, seeds `config.yaml`, validates, stays inert). The rest of
  this guide applies to either.

```bash
python $ENGINE_DIR/scripts/framework.py init ../my-brain --domain "My Brain"
#   --interactive     prompt for inputs (also the default with no dest + a TTY)
#   --feeds f.opml    seed feeds/ from an existing OPML
#   --delivery local  no Telegram (default: telegram)
#   --port-offset 100 if another pack already runs on this host (reader 9300, mcp 8830)
#   --no-compose      skip the generated docker-compose.yml
```

It refuses to overwrite a non-empty directory and writes a ready-to-fill skeleton:

```
my-brain/
├── schema.yaml                       # THE machine contract — edit first (step 2)
├── pack.yaml                         # identity + ownership for composition (step 2a)
├── CLAUDE.md                         # persona / curation rules (step 3)
├── engine.version                    # pins the engine release: engine/version/hermes_pin
├── README.md, LICENSE                # your pack's docs + license
├── validate.py                       # offline self-check (runs in the pack's CI)
├── .github/workflows/validate.yml    # CI: validate.py + `docker compose config`
├── .env.example                      # copy to .env and fill (step 0/7)
├── .gitignore                        # already excludes .env, .hermes-data/, config.yaml
├── feeds/
│   ├── feeds.opml                    # ACTIVE sources — EMPTY by default (opt in)
│   └── feeds.opml.example            # the curated suggestion list (step 4)
├── crons/
│   ├── domain-crons.json             # [] — your domain-tier crons (step 5)
│   ├── engine-template-prompts.json  # {} — prompts for engine selector scripts (step 5)
│   └── scripts/                      # your domain cron scripts
├── wiki/                             # the content tree the engine compiles + maintains
│   ├── index.md                      # a seed dashboard page
│   └── {sources,entities,concepts,predictions,findings,briefings,operational}/
├── .hermes-data/                     # runtime state (gitignored; `framework init` seeds config.yaml)
└── docker-compose.yml                # gateway + reader + mcp, ports offset as requested
```

> A pack may be **public**, so the scaffold writes `.gitignore` *first* and never
> commits `.env`/`.hermes-data/`. Keep it that way.

---

## 2. Fill in `schema.yaml` — the contract

`schema.yaml` is the heart: the engine **reads** it and never hardcodes layout.
The scaffold ships a sane generic contract; edit it to your domain. The blocks:

- **`okf.required: [type]`** — the OKF v0.1 base. Every page needs a `type`; this
  stays.
- **`types:`** — your domain's page types and their required fields. This is the
  main thing you change. Each type's `required` list should include `type`.
  ```yaml
  types:
    source:     {required: [type, source_kind, publisher, published]}
    entity:     {required: [type]}
    concept:    {required: [type]}
    prediction: {required: [type, status, confidence, subject, resolves_by]}
    dashboard:  {required: [type, title]}
  ```
  Rename/add freely — `release`, `paper`, `org`, whatever your domain needs.
- **Identity (`id`)** — every page carries a stable, immutable `id` of the form
  `<scope>:<key>` (conformance §5). By default the engine **mints a slug** from the
  page's title/name. For a type backed by an external authority, declare it on the
  type and the engine derives a deterministic id instead — the same entity gets the
  same id everywhere, which is what lets two packs converge on one canonical page:
  ```yaml
  types:
    attack-pattern: {required: [type, mitre_id], id_authority: mitre, id_field: mitre_id}
    #   -> id "mitre:t1059";  owner: <pack> marks who may mutate owned fields
    vulnerability:  {required: [type], id_authority: cve, id_field: cve_id, owner: my-pack}
  ```
  `owner` (and per-field `field_owners`) are how the converge-on-write path keeps
  one pack from clobbering another's fields. All four keys are optional — omit them
  and you get minted slugs, which is fine for a single pack.
- **`partitioning:`** — how each namespace buckets on disk (read by
  `okf_migrate`/`reshelve`/`reshard`): `by-letter`, `by-date`, or `flat`, with
  `reshard_over` (split a directory once it exceeds N entries).
- **`hot_set:`** — the agent's load-first working set (compiled to `wiki/HOT.md`):
  which namespaces/fields/recency feed the first thing the agent sees.
- **`permissions:`** (conformance G2) — the per-namespace create/update/delete
  matrix the **MCP write path enforces**. `delete:false` ⇒ tombstone, never
  hard-rm. Mark a namespace `create/update:false` to make it human-authored.
- **`review:`** (G3) — which confidence/review values *flag* a page for review
  (`needs_review` + `wiki/_review-queue.md`) rather than blocking the write.
- **`tier:`** (G4) — how hot/warm/cold are **derived at query time** (never
  stored); read by the tier-refresh cron and the `--tier` filter in
  `kb_search`/`okengine-mcp`.
- **Optional engine-cron inputs** — the maintenance drains/audits/classifiers
  ship no domain taxonomy; they read it from these (all default to empty, so a
  minimal pack just gets generic behaviour):
  `type_aliases` (legacy/alias type → a `types:` key, used by the
  schema-type/normalize drains), `operational_types` (types exempt from
  conformance drift), `depth_critical_types` (types page-quality holds to a
  deeper bar), `classify_hints` (`type → [tags]`) and `classify_catchall` (types
  the schema-classify drain disambiguates), and `protected_fields` (curated
  fields the write path must never silently drop). The scaffold seeds them empty
  with worked examples in comments.

A sub-tree can drop its **own** `schema.yaml`; the validator walks up to the
nearest one — that's how `wiki/<subdomain>/` becomes a second domain in one vault.

---

## 2a. `pack.yaml` — identity & composition

`pack.yaml` declares **who the pack is and what it owns**. It's small but strongly
recommended — `framework validate` WARNs when it's absent and (when present)
**FAILs if its `trust` isn't `public`/`private`**; it's required for composition:

```yaml
name: my-pack
version: 0.1.0
trust: public            # public | private — packs compose only within one trust level
owns:
  types: [source, concept, prediction, dashboard, <your domain types>]
  namespaces: [sources, concepts, predictions, entities]
requires: []             # other packs this one depends on, e.g. [okpack-base@>=0.1.0]
port_offset: 0           # default host-port offset: reader 9200+N, mcp 8730+N
```

`port_offset` makes a host-port offset a **durable property of the pack**: set it
(e.g. `100` → reader 9300, mcp 8830) when the pack is meant to run alongside
another stack, and `framework pull` applies it automatically — every fresh pull
and the deployed compose get the offset without anyone remembering `--port-offset`
(which still overrides it). `framework init --port-offset N` records N here for you.

**The composition model: one engine → one vault → one *or many* packs.** When you
drop several packs into a packs directory, the engine composes them into a single
vault — but composition is **additive and disjoint**: no two packs may `own` the
same **type** or **namespace**, and all must share one **trust** level. Declare
`owner: <pack>` on each type you own (above) so the converge-on-write path can keep
fields straight.

> **The catch most authors hit:** two *full* domain packs usually both define the
> spine (`source`/`concept`/`prediction` + `sources`/`concepts`/`predictions`),
> which is an ownership conflict — so they **can't** share a vault; they run as
> separate instances. To genuinely compose, factor the shared spine into a base
> pack that each domain pack `requires:`, and have each domain pack own *only* its
> own types/namespaces. If you just want a standalone pack, own the spine and ship
> it as its own instance — that's the common case.

Check a composition with `python $ENGINE_DIR/scripts/cron_pack_split.py compose
--packs <dir>` (it discovers every pack with a `pack.yaml` and fails loudly on an
ownership/trust conflict).

---

## 3. Write the persona — `CLAUDE.md`

This is what the engine's cron agents read at runtime as `$WIKI_PATH/CLAUDE.md`
(the *domain voice + workflow*, distinct from the engine repo's dev/ops
`CLAUDE.md`). Replace every scaffold placeholder — `framework validate` warns if
any remain. Sections:

- **Mission** — one line: what this second brain is for.
- **Positioning** — *filter, not feed*; *compounding KB, not RAG*; who reads the
  digests.
- **Ingest workflow** — how a source becomes entities/concepts/predictions: the
  dedupe key, any scoring rubric, when to create an entity vs a one-off mention,
  when to file a (falsifiable, dated) prediction.
- **Predictions** — the "what would refute this" discipline + the required fields
  from your `schema.yaml`.
- **Domain pointers** — the entity types/taxonomy you track.

This file is the single biggest lever on output quality. Be concrete.

---

## 4. Wire feeds

Add `<outline xmlUrl="..."/>` entries to `feeds/*.opml` (consumed by the engine's
`feed_fetch.py`). Probe them live — keep only sources that return HTTP 200 with
valid RSS/Atom. A query/enrichment-only pack can have no feeds (validate warns,
doesn't fail).

```xml
<opml version="2.0">
  <head><title>My Brain feeds</title></head>
  <body>
    <outline text="Example Source" type="rss" xmlUrl="https://example.com/feed.xml"/>
  </body>
</opml>
```

**Non-feed sources (PDF / HTML).** Feeds land as markdown stubs; for binary or noisy
sources you drop into `raw/`, the engine ships **Stage-1 extractors** that write a
clean `.txt` companion the ingest selector prefers over the raw file:
- `scripts/extract-pdfs.sh` — PDF → `foo.pdf.txt` (needs `poppler-utils`).
- `scripts/extract-html.py` — HTML → `foo.html.txt` article text (generic; uses
  trafilatura/readability if installed, else a stdlib heuristic; `--selector "<css>"`
  to override a site the generic pass gets wrong).
Both are domain-agnostic and run on the host over the `raw/` tree. **Per-publisher**
extraction (site-specific selectors, format adapters like SEC filings) is *pack*
work — supply it in `crons/scripts/`, not the engine.

---

## 5. Crons — domain jobs + engine-template prompts

Cron jobs fall into three **tiers** (classified in the engine's
`config/cron-tiers.yaml`; see `engine-domain-boundary.md` §3):

| Tier | Who ships it | Your pack supplies |
|---|---|---|
| `engine` | engine, full def | nothing — runs unchanged on any vault |
| `engine-template` | engine ships the **selector/wake-gate SCRIPT** | the **prompt**, keyed by job name |
| `domain` | your pack, full def | the whole job (schedule + script and/or prompt) |

At deploy, `scripts/cron_pack_split.py` **merges** the engine's
`config/engine-crons.json` with your pack's two cron files into the generated
`config/cron-plus-jobs.json` (never hand-edit that artifact):

- **`crons/engine-template-prompts.json`** — a JSON object mapping an
  engine-template job name → the domain prompt the engine grafts onto its script.
  The engine-template jobs you can supply prompts for include `raw-backfill`,
  `entity-backfill`, `concept-backfill`, `classify-new-sources`,
  `prediction-grade`, `prediction-candidate-watch`, `event-ledger`,
  `page-quality-enrich`, … (full list: `config/cron-tiers.yaml`).
  ```json
  {
    "entity-backfill": "You maintain entity pages for <domain>. For each entity missing required fields, research and fill …",
    "prediction-grade": "Grade open predictions against new evidence. A prediction resolves only when …"
  }
  ```
- **`crons/domain-crons.json`** — a JSON **array** of full domain-tier defs. Each
  needs a `name` and a `schedule`, plus a `script` and/or a `prompt`. A `script`
  is resolved from `crons/scripts/`.
  ```json
  [
    {"name": "my-weekly-digest", "schedule": "0 13 * * SUN",
     "prompt": "Compile the week into a digest grouped by …"}
  ]
  ```
- **`crons/scripts/*.py`** — any domain-specific scripts your domain crons call.

Leave both files at their scaffold defaults (`[]` and `{}`) for a minimal pack —
the engine-tier maintenance fleet still runs.

---

## 6. Validate (before every deploy)

```bash
python $ENGINE_DIR/scripts/framework.py validate ../my-brain
#   --probe-feeds   also HTTP-probe every feed URL (network)
#   --quiet         only WARN/FAIL + summary
```

Exit code **0** = no FAILs (WARNs are allowed); **1** = at least one FAIL. Fix all
FAILs before deploying. The check is **strict about real requirements** — a pack
that passes is structurally complete and deployable.

**FAIL — a required file/config/variable is missing or wrong (must fix):**

- **`schema.yaml`** — present, parses, and a non-empty `types:` mapping.
- **`CLAUDE.md`** — present and not effectively empty (the persona).
- **`.hermes-data/config.yaml`** — when present, must parse and carry
  `terminal.backend: local` + the `okengine` and `okengine-write` MCP servers.
  This is runtime state seeded at deploy: in a **definition repo** (where
  `.hermes-data/` is gitignored) its *absence* is a WARN, not a FAIL — only a
  deploy-ready dir that should have seeded it, or a present-but-wrong config,
  fails.
- **`engine.version`** — present, pinned to a `vX.Y.Z` release, **and matching the
  engine you're validating with**. The pin isn't a guess: `framework init` stamps
  it from the engine's `engine-manifest.yaml` (`engine_release` + the Hermes
  `pinned_tag`), and `framework validate` reads that *same* file and FAILs on a
  mismatch — so a fresh scaffold always matches, and a pin that trails the engine
  (e.g. `v0.1.0` against a `v0.2.0` engine) fails until you re-validate against the
  new engine and bump it. A bare `latest` or the retired `engine-vX.Y.Z` form fails.
- **`README.md`** — present, substantive (has `##` sections, not a stub), **and a
  Deploy/Install/Quickstart section** so an operator can bring the pack up.
- **`LICENSE`** — a non-empty `LICENSE` / `LICENSE.md` / `LICENSE.txt` / `COPYING`.
- **No unrendered `{{TOKEN}}`** left in any declarative file (schema, persona,
  README, `pack.yaml`, crons, config, compose, `.env.example`).
- **Crons** — `domain-crons.json` is a JSON array; **every cron has a name, a
  usable schedule `expr`, and a script or a prompt**; `engine-template-prompts.json`
  is an object with **no empty prompt**; every `crons/scripts/*.py` compiles.
- **`pack.yaml`** (if present) parses and its `trust` is `public` or `private`.
- **`.env`** is **not** git-tracked; and if `OKENGINE_BIND` exposes the stack
  beyond localhost, a real `OKENGINE_MCP_TOKEN` + `OKENGINE_READER_PASSWORD` are
  required (not the built-in defaults).

**WARN — valid and deployable, but worth fixing:**

- Unfilled persona placeholders, or a `README.md` with no layout/structure section.
- Empty/absent feeds or unreachable feed URLs (a pack ships **inert** by design),
  a missing `.env.example` or no model-provider key documented.
- `pack.yaml` absent (recommended for composition), or owning no types/namespaces.
- Optional/engine-supplied schema blocks absent (`partitioning`, `hot_set`), a
  pack-level `strict_types` (engine-owned — ignored), or a `type_aliases` /
  `classify_hints` / `operational_types` reference to a type not in this pack's
  `types:` (it may be owned by a pack you `requires:` — resolved at deploy).

Run it after every change; it's offline except `--probe-feeds`.

---

## 7. Deploy

Copy `.env.example` to `.env` and fill it (model key, delivery, optional
`OKENGINE_MCP_TOKEN` / `OKENGINE_READER_PASSWORD`), then — **from the pack dir** —
one command runs the whole bring-up in the right order:

```bash
bash $ENGINE_DIR/scripts/deploy.sh                 # validate -> seed -> build (if needed) -> compose up -> crons
```

`deploy.sh` is the single entry point so the seed-before-compose step can't be
skipped (a fresh `git clone` of a library pack has no `.hermes-data/`). Flags:
`--rebuild`, `--skip-build`, `--skip-validate`, `--no-crons`, `--fix-perms`.

The gateway runs as `HERMES_UID`, which **defaults to your own uid** (`$(id -u)`), so a
pack you cloned as yourself is writable out of the box — `deploy.sh` writes the tree as
you and the gateway remaps internally. No `chown`, nothing to export. (`deploy.sh
--fix-perms` is a fallback that makes the tree world-writable for a non-matching uid.)

The **fixed-uid** model — for a vault you'll move between hosts or operate as several
users — pins a uid and chowns the tree to it, so ownership doesn't depend on who
deployed. Don't `sudo chown -R 10000:10000 .` while running `deploy.sh` as your own user,
though — that breaks the deploy, which must write the tree (okengine#33). The fixed-uid
model is:

```bash
export HERMES_UID=10000 HERMES_GID=10000           # a fixed uid (portable/shared vault)
sudo chown -R 10000:10000 .                        # the tree must be owned by it
bash $ENGINE_DIR/scripts/build-engine-image.sh     # build the gateway image once
bash $ENGINE_DIR/scripts/ensure-runtime.sh         # seed .hermes-data/config.yaml — MUST precede compose
ENGINE_DIR=$ENGINE_DIR docker compose up -d        # gateway + reader + mcp
CRON_PACK_DIR=$(pwd) bash $ENGINE_DIR/scripts/deploy-cron-scripts.sh
CRON_PACK_DIR=$(pwd) bash $ENGINE_DIR/scripts/deploy-cron-plus-jobs.sh
```

Then run the **smoke gauntlet** (deploy §2 step 6): a `no_agent` cron succeeds, an
LLM agent cron succeeds, a delivery lands, the MCP answers
(`curl :8730/mcp` → 401 without a token), and the conformance gate is green.

> **Private vs public.** A private instance should gate its surfaces:
> `OKENGINE_READER_PASSWORD` (reader Basic auth) and `OKENGINE_MCP_TOKEN` (MCP
> bearer). A public reference instance may run the reader open **but must mount
> only public content** — never co-mingle public and private in one vault.

---

## 8. Iterate

The vault compounds: feeds → `raw/` → ingest → compiled
sources/entities/concepts/predictions → digests, with the engine's maintenance
fleet re-shelving, re-sharding, repairing wikilinks/YAML/schema, and refreshing
the index/health/tiers. Agent writes flow through the **enforced MCP write path**
(`okengine-write`), which validates every write against your `schema.yaml` and
applies the field-loss / reserved-file / permission guards — so as you evolve the
schema, the contract stays enforced. Re-run `framework validate` whenever you
change the schema, crons, or feeds, then redeploy crons with the two
`deploy-cron-*.sh` scripts.

To **upgrade the engine** under a pack without rewriting the pack, see
[`deploy-a-new-domain.md`](deploy-a-new-domain.md) §3.
