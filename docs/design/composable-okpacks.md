# Design note (RFC): composable okpacks

**Status:** **Shipped** — multipack composition is live (this began as an RFC; see [design/README.md](README.md) for the authoritative status index).
**Scope:** how multiple okpacks combine to build one compounding vault.
**Relationship to current code:** the engine is domain-agnostic (it reads
types/namespaces/aliases from `schema.yaml`, hardcodes no domain). That is a
*prerequisite* for composition, but the engine has **no pack-merge layer
today** — `cron_pack_split` takes one pack, schema is one file per vault (+
walk-up for sub-trees). Composition is a new subsystem.

---

## 1. The vision

A pack is primarily a **declarative definition** of *what to build* and how to
maintain it. It may also ship an optional **seed corpus** (pre-built pages) so a
new install starts populated instead of from zero. The engine is the
**builder/maintainer**. Multiple packs compose into one vault:

```
okengine  +  { pack_A, pack_B, pack_C, ... }   →   one compounding vault
```

Each pack contributes some of:

1. **Definitions** — the page `types:` it owns + their schema (partitioning,
   tiers, permissions, required fields) and relationship rules.
2. **Acquisition + processing** — feeds + cron jobs that *pull* data into a shared
   **`incoming/`** landing area, *classify/compile* raw items into typed pages
   (the domain-judgment step, driven by the pack's prompt), plus any **custom
   scripts** the pack needs (it ships them and declares where they live, e.g.
   `crons/scripts/`).
3. **Curation rules** — a scoped persona section telling the agent how to
   maintain the pack's types.
4. **Seed corpus (optional)** — pre-processed baseline pages (e.g. thousands of
   curated `threat-actor` profiles) the engine installs once, then maintains as
   normal vault content. See §4.13.

The engine's own operations are deliberately **small and fixed** (index, graph,
tier, reshelve/reshard, repair, health, search, delivery transport). All domain
knowledge — data shape, frontmatter conventions, relationships, processing,
scripts, seed content — comes from packs.

Everything after a typed page exists — indexing, the wikilink/backlink graph,
tiering, reshelve/reshard, YAML/link/schema repair, health, search, delivery
mechanics — is **engine-generic and shared across all packs**. That shared graph
is where the compounding value comes from: a `threat-actor` profile from pack A
gets cross-referenced by a `hunt` from pack B because they live in one graph.

End state: custom packs are ~unbounded, and because a pack is *config*, packs
become **shareable**.

### The honest boundary (what "the engine does the rest" does NOT include)

| Step | Owner | Why |
|---|---|---|
| What to pull (feeds, queries) | **pack** | domain choice |
| Pull → `incoming/` | **pack** (cron) | domain endpoints/credentials |
| Classify raw → a `type` | **pack** (engine-template loop + pack prompt) | domain judgment; the engine cannot know "this is a threat-actor" |
| Required fields per type | **pack** (schema) | domain contract |
| Index / dedup / tier | engine | generic over any OKF page |
| Wikilink + backlink graph | engine (IWE) | generic; links anything to anything |
| Reshelve / reshard / repair | engine | structural, schema-driven |
| Search / read / MCP | engine | generic over the vault |
| What to deliver (digest content) | **pack** (cron + prompt) | domain output |
| Delivery mechanics (Telegram, render) | engine | generic transport |

The crux: **the raw→typed compile step is irreducibly pack-domain.** "The engine
does the rest" is true for everything *downstream of a typed page*, not for
producing it. Keep this explicit or the boundary will rot.

---

## 2. Data flow (proposed)

```
pack feeds ─┐
            ├─► pull crons (pack) ─► incoming/<pack>/ ─► compile crons (pack prompt)
pack queries┘                                                     │
                                                                  ▼
                                              wiki/<namespace>/...  (typed pages)
                                                                  │
   ┌──────────────────────── engine, generic, shared ────────────┴───────────┐
   │ index-tree · hot-set · tiers · IWE graph · reshelve/reshard · repairs ·  │
   │ health · search · MCP · dedup · delivery transport                        │
   └───────────────────────────────────────────────────────────────────────────┘
```

`incoming/` is the handoff: pack-owned acquisition writes there; pack-owned
compile drains it into typed pages; the engine processes typed pages uniformly,
ignorant of which pack produced them. (`raw/` stays the immutable archive of
fetched material, as today.)

---

## 3. The composition model (how N definitions merge)

| Contribution | Merge rule | Notes |
|---|---|---|
| `types:` | union | collisions are the hard case (§4.1) |
| `feeds/` | union | dedup by URL (§4.6) |
| `crons/` | union, pack-namespaced job names | per-pack engine-template instances (§4.5) |
| `partitioning:` / `tier:` / `hot_set:` | per-namespace, single owner | the pack that owns the namespace sets these (§4.2) |
| `permissions:` (G2) | per-namespace, single owner | no cross-pack override |
| `type_aliases` / `protected_fields` | union | collisions/cycles to resolve (§4.1) |
| persona `CLAUDE.md` | ordered, **scoped sections** — never concatenated voices | §4.4 |

Ownership is **page + field scoped** (§5a): a pack *owns* the types it declares
and the pages of those types; non-owners may *read/link*, *add new keys*, or hold
an explicit **per-field grant**, but never mutate an owner's fields or redefine
its types. That scoping — not a coarse "namespace+type" claim — is what keeps the
merge deterministic without blocking cross-pack enrichment.

### 3a. Schema model — layered, mandatory + passthrough

Schemas stack from most-general to most-specific, and the runtime contract is the
**merge** of all layers:

```
OKF base            (the spec: `type` is the one mandatory field — universal)
  └─ engine base    (universal field set every page gets: type, id, created,
                     updated, … — engine-owned, pack-independent)
       └─ pack schema(s)   (per-type required fields, partitioning, tiers,
                            permissions — each pack owns its types)
            └─ sub-domain schema.yaml (walk-up override for a subtree)
```

Two load-bearing properties — **both already implemented today** (see
`tools/schema_validator.py` + the write-path field-loss guard):

- **Mandatory is validated; everything else passes through.** A page must satisfy
  `okf.required` + its type's `required:` fields. Any *additional* frontmatter is
  **allowed, never validated, and preserved** across edits (the field-loss guard
  forbids dropping existing keys). Undefined types are allowed unless
  `strict_types: true`. This is what makes the format **open/extensible**: packs
  add fields freely; the engine never strips "extra" data it doesn't recognise.
- **Published, reusable pack schemas.** A pack defines *and publishes* its schema
  (e.g. `okpack-cti` publishes the security types), so others reuse it instead of
  reinventing. Variants/forks of a published schema are a later concern.

Implication for composition: merging packs = unioning their `types:` on top of the
shared engine base, with the **owning pack** setting each type's `required:` and
each namespace's partitioning/tier/permissions. Because extra fields pass through,
one pack reading another pack's pages "just works" even across schema versions —
unknown fields don't fail validation, they ride along.

**Precedence (resolved).** For a **shared** namespace, the **merged base+pack
schema is authoritative**; **walk-up** schemas are permitted **only for
pack-private subtrees** (else a sub-domain `strict_types: true` would reject valid
composed writes). The global toggles — `okf.required`, `strict_types`,
`common_optional` — are **engine-base only** (a pack can't set them) and validated
against the **merged** type set.

---

## 4. Issues, risks, and open questions (the point of this note)

### 4.1 Schema / type collisions
- Two packs declare the same type with different `required:` lists — union,
  intersection, or hard error? (Recommend **error** in v1; force disjoint types.)
- Same type name, different *meaning* across packs (`report`, `entity`) — needs
  type **namespacing** (`packA:entity`) or a global type registry.
- `type_aliases` collisions and **cycles** across packs (A: x→y, B: y→x).
- A pack writes pages of a type **another** pack owns — allowed? (Read yes,
  create maybe; needs an explicit grant.)
- `okf.required`, `strict_types`, `common_optional` are vault-global today — which
  pack sets them under composition?

### 4.2 Namespace governance
- Shared `entities/` (max compounding) vs per-pack subtrees (`wiki/<pack>/`, zero
  new machinery via walk-up, but little cross-linking). **The core fork.** The
  vision needs shared namespaces → real merge, not walk-up.
- If two packs both write `entities/`, who owns its `partitioning`/`tier`/
  `permissions`? Conflicting partition strategies can't coexist (one on-disk
  layout per directory).
- Reshelve/reshard operate per namespace from one config — a shared namespace
  needs one agreed layout.

### 4.3 Cross-pack dependencies
- Pack B (hunting) **requires** pack A (threat-actor) types/pages. Without A
  installed, B's links dangle (orphans) and its compile prompt references missing
  types. Needs a `requires:` declaration + install-time validation.
- **Version coupling:** B depends on A's type *shape*; A bumps `required:` →
  B breaks. Pack semver + compatibility ranges.
- **Ordering / eventual consistency:** must A populate before B enriches, or does
  the engine repair cross-links over time as both fill in? (Lean on eventual
  consistency + the existing link-repair drains.)
- Diamond deps / conflicting transitive requirements.

### 4.4 Persona / agent behaviour (hardest)
- You cannot union two prose curation voices — merged instructions degrade
  behaviour and bloat context. Each pack must contribute a **scoped section**
  ("you own types X/Y; curate them thus"), loaded per-cron, not one global voice.
- The engine-template model already scopes prompts per job — composition extends
  that: each pack's compile/maintain crons carry *their* prompt over *their*
  types. Good. But a cron that touches a **shared** namespace sees multiple packs'
  rules — whose win?
- Contradictory rules on a shared type (A: "tombstone stale after 90d"; B:
  "never delete"). Needs per-type rule ownership.
- Context-window pressure: N packs' combined persona/schema may not fit; the
  agent must load only the relevant pack's section per task.
- **Prompt-injection across packs:** pack B's content (or a pulled source) could
  contain text that steers an agent running pack A's job.

### 4.5 Cron fleet composition
- **Job-name collisions** (two packs' "daily-digest") → mandatory pack-prefixed
  job ids.
- **engine-template singletons:** today `entity-backfill` is one job with one
  prompt. Composition needs **per-pack instances** (`entity-backfill@A`,
  `@B`) scoped to each pack's types/namespaces — or the selector must iterate
  packs. `cron_pack_split` must become N-way and collision-aware.
- **Scheduling contention / fairness:** N packs × M crons → LLM rate-limit and
  CPU contention; one runaway pack starves others. Needs per-pack scheduling
  fairness + concurrency caps.
- **Cost:** the "infinite packs" vision hits token cost first. Per-pack **budget
  caps** and accounting (which pack spent what) are not optional at scale.

### 4.6 Acquisition / feeds / `incoming/`
- **Duplicate feeds/sources** across packs — dedup at fetch (shared fetch cache
  keyed by URL) so one source isn't pulled N times.
- **Politeness / rate limits** per upstream when multiple packs hit the same site.
- **`incoming/` contract:** structure (`incoming/<pack>/` for attribution+cleanup
  vs flat with provenance frontmatter), required metadata (source URL,
  `fetched_at`, `pack_id`, dedupe key), accepted formats (raw HTML/text/JSON?),
  atomicity of partial writes.
- **Backpressure:** N packs flooding `incoming/` faster than compile drains it →
  unbounded backlog. Need queue depth limits + drop/defer policy.
- **Cross-pack dedup:** the same article relevant to two packs — one shared page
  with multiple pack "lenses", or two pages? (Recommend one page, multiple
  `discovered_by` provenance entries.)
- **Idempotency:** a re-pull of the same item must not create duplicates
  (existing `raw:` dedupe key must be consistent across packs).

### 4.7 Secrets & credentials
- Pull crons need API keys (search, vendor APIs). Secrets are instance-global
  (`.env`) — **name collisions** (two packs both want `TAVILY_API_KEY` with
  different keys) → per-pack secret namespacing.
- A **shared/untrusted** pack that declares secret *names* could exfiltrate via a
  malicious cron. Secrets must never be readable by arbitrary pack code.

### 4.8 Trust boundary & security (hard rules)
- **Composition is intra-trust-boundary only.** A public pack + a private pack in
  one vault → the public reader/MCP leaks private pages. Packs must declare a
  trust level; the engine **refuses to compose mismatched levels**. (Orthogonal to
  composition: public vs private is about exposure, not modularity.)
- **"Just config" is dangerous.** A pack ships cron *scripts* (`crons/scripts/*.py`)
  that run with engine privileges, and *prompts* that drive an autonomous agent
  with file-write + tool access. A shared pack from a stranger = **RCE +
  prompt-injection**. Mitigations: prefer **prompt-only** shareable packs (no
  arbitrary scripts; only engine-template prompts + schema + feeds); sandbox/seccomp
  any pack scripts; sign + pin shared packs; review before install; capability
  manifest ("this pack may: fetch URLs, write `entities/`, deliver to Telegram").
- Supply-chain: a popular shared pack is a high-value compromise target.

### 4.9 Conformance / write path
- The MCP write path validates against `schema.yaml`. Under composition it must
  validate against the **merged** schema (walk-up does not compose same-namespace
  types). `protected_fields`/field-loss guard = union across packs.
- Conflicting conformance gates (G2/G3) on a shared namespace.

### 4.10 Lifecycle / operations
- **Adding** a pack to a running vault: backfill existing pages against the new
  types? Re-index? Cold-start the pack's crons without flooding.
- **Removing** a pack: its pages — tombstone, delete, or orphan? Its types vanish
  → existing pages become non-conformant; its inbound cross-links dangle.
- **Upgrading** a pack: schema migration (`okf_migrate`) across a pack version bump;
  content built under the old shape.
- **Failure isolation:** pack A's cron crashes — B must keep running; one pack's
  bad write must not corrupt the shared graph.
- **Observability:** per-pack health, error rates, cost, and "which pack owns this
  page / broke this link."

### 4.11 Identity, registry, distribution
- Pack **identity**: name + semver + owned namespaces/types + `requires` + trust
  level + capability manifest — a `pack.yaml` / extended `engine.version`.
- **Discovery vs registry:** "presence enables it" → the engine scans a packs dir
  and self-describes each; but it still needs to *enumerate* installed packs (for
  merge, deps, conflict detection). Presence-based discovery + an in-memory
  registry, not a hand-maintained enable/disable file.
- **Distribution** for shareable packs: format (dir/repo/tarball of config),
  versioning, a hub/index, provenance/signing, trust on install.
- **Determinism:** same packs + same feeds ≠ identical vault (LLM compile is
  nondeterministic; feeds drift). The *definition* is reproducible; the *vault* is
  not. Fine — but means "share a pack" reproduces behaviour, not content.

### 4.12 Scale
- Composition accelerates vault growth (N packs × continuous ingest). Index/tier/
  search scaling (see guide-4, 100k) arrives sooner; per-namespace sharding and
  the derived tiers must keep up.

### 4.13 Seed / preprocessed corpus ("batteries-included" packs)
A pack may bundle pre-built pages so a new install starts populated (e.g. a
threat-actor pack shipping a curated baseline). **Deferred — build later, but
architect now** so it isn't painful to add: the only thing v1 must guarantee is
**stable page IDs** + **seed provenance** (`created_by: pack:<id>@<ver>`) so seed
can be added/updated/removed later without a redesign.

**Architecturally this is nearly free** — seed pages are OKF pages; the engine
copies them in at install and then maintains them like any other content, no
special handling. The hard parts are operational:

- **Updates are the killer.** Once installed, seed pages become living, agent-
  mutated vault content (edited, evidence appended, some tombstoned). When the
  pack ships v2 (more pages + corrections to existing ones), merging upstream
  changes into a locally-diverged vault is **3-way merge on mutated content**.
  Two models:
  - **Snapshot / bootstrap (recommended v1):** seed is copied **once** at install,
    then it's the user's vault. Upstream seed updates do **not** propagate. Simple,
    fully decoupled; user forgoes later upstream improvements.
  - **Tracked baseline / subscription (hard, future):** the pack seed is an
    upstream the user can pull, with conflict resolution against local edits.
    Powerful but it's package-data-migration on continuously-mutated content.
- **Stable identity / dedup.** Seed pages need durable IDs so a later update can
  *match-and-update* rather than duplicate, and so two packs shipping the same
  entity (both ship "APT28") dedup into one page with merged provenance.
- **Licensing / redistribution — policy, not machinery.** Ship **only openly-
  redistributable baselines** (e.g. MITRE ATT&CK / D3FEND, which permit reuse
  *with attribution*); **never** redistribute licensed/proprietary source data.
  This keeps it a simple publish-time policy (redistributable-only + carry the
  required attribution) rather than per-page license tracking. A pack that has
  processed proprietary data ships the *schema + processing*, not the corpus —
  each user rebuilds that corpus from their own licensed access.
- **Size / distribution.** A seed corpus makes a pack a **data distribution**, not
  "just config" — potentially large. Changes the share format (config + a content
  bundle/LFS/separate artifact), download/verify story, and versioning.
- **Security (injection).** Seed markdown is a **prompt-injection vector** the
  moment the maintenance agent reads it. A malicious shared pack can seed content
  that steers the agent (tool calls, writes). Sandboxing the agent + content
  provenance + signing the seed are prerequisites for installing third-party seed.
- **Schema migration on install.** Seed built under the pack's schema vX may need
  `okf_migrate` if the installed engine/pack schema differs.
- **Provenance separation.** Mark seed-origin pages (e.g. `created_by:
  pack:<id>@<ver>`) so health/audit/cost don't misattribute them, and so a pack
  *removal* can find what it brought in.
- **Trust-boundary inheritance.** A public seed corpus is fine in a public
  instance; the same isolation rules apply (no private seed in an exposed vault).

> Net: seed data is one of the highest-value features (it kills cold-start and
> amplifies cross-pack compounding) **and** one of the highest-risk
> (licensing + injection + the update problem). The engine work is trivial; the
> distribution/governance work is not.

---

## 5. Decisions

**DECIDED — topology: one engine → one vault → many packs.** A single instance
serves exactly one vault, built and maintained from a composed set of packs. A
new vault means a **new install** (its own engine deployment + pack set). This
removes multi-vault routing/registry concerns entirely; the engine only ever
merges packs into *one* vault. The engine's operation set stays small and fixed;
all domain logic, scripts, relationships, and (optional) seed content come from
packs.

**DECIDED — engine owns a base schema.** The engine ships a `base-schema.yaml`
(universal fields applied to every page regardless of pack: `type`, a stable
`id`, `created`, `updated`, provenance) and the merge always includes it. Packs
then declare **only their domain types** on top of the base — they no longer
re-declare the universal layer. Critically, the base owns the **stable `id`**
that shared-namespace dedup depends on (see below).

**DECIDED — shared namespace + merged schema.** Packs write into shared
namespaces (an `attack-pattern` page is one page, not one-per-pack), and the
runtime schema is the merge of base + all packs. This is what makes cross-pack
compounding real — but it makes **canonical identity + dedup-on-write
mandatory**, and requires the build-out below.

This decision creates four hard requirements (full spec in §5a):

1. **Type-independent identity.** Every page has an immutable `id` (engine base
   schema) — an **authority id** (`mitre:T1059`, `cve:CVE-2024-12345`) when the
   owning type declares one, else a **minted slug stamped once at creation**.
   `type` is mutable and never part of the id.
2. **Dedup / converge on write (authority ids only).** A write to an existing
   **authority** id **merges** into that page (provenance union + page/field-scoped
   ownership rule). Minted-slug pages **never auto-merge** — collisions route to
   review / the dedup drains.
3. **Type + field ownership.** Each type/namespace has one owning pack that sets
   its `required:`/partitioning/tier/permissions and owns its pages; non-owners
   add new keys or hold explicit per-field grants, never mutate owned fields.
   Shared *reference data* (ATT&CK) is **factored into a base pack** others
   `require:`, not re-pulled.
4. **N-way, collision-aware merge** in `cron_pack_split` + per-pack engine-template
   instances + the persona-sectioning convention.

### Worked example — two packs and MITRE ATT&CK
A threat-actor pack and a threat-hunting pack both want ATT&CK techniques.
- **Right way:** an `okpack-attack` base pack owns `attack-pattern` (declares
  `id_authority: mitre`) + the technique pages; both packs `require:` it and
  reference `mitre:T1059`. One canonical copy, cross-linked by both = the compounding.
- **If both pull it anyway:** authority id `mitre:T1059` → atomic id-claim →
  **one** page; the actor pack adds `tactic`, the hunt pack owns `detection` via a
  per-field grant (both coexist, no clobber). No duplicate.
- **Failure mode designed against:** divergent identity (`mitre:T1059` vs a slug
  `attack-pattern:t1059`) → two pages. Prevented by the owning type's
  `id_authority` being mandatory for that type.

### 5a. Identity scheme (DECIDED)

Every page carries an **immutable, type-independent `id`** (engine base schema,
mandatory). The `id` — not the file path, and **not** the type — is the unit of
identity: reshelve/reshard move the file and reclassification changes the `type`,
but the `id` never changes. Two kinds:

- **Authority id** — when the owning type declares `id_authority`, the id is
  **`<authority>:<localid>`** (`mitre:T1059`, `cve:CVE-2024-12345`). The authority
  sub-namespace is mandatory — a bare local id isn't globally unique across
  authorities. This is what lets independent packs converge on one page.
- **Minted slug** — otherwise, a stable slug **stamped once at creation and never
  recomputed**, frozen even if the page's `type` or name later changes.

`type` is a *mutable attribute* that drives **placement/path**, not identity — so
the engine's own reclassification (`schema_type_drain`) is safe (it never changes
an `id`). *(This supersedes the earlier `id: <type>:<key>` form, which embedded a
mutable `type` in a supposedly-immutable id.)*

**Normalization.** One engine-owned, **named + versioned + test-vectored**
normalizer produces every key — unicode fold rule, delimiter escaping, empty-result
handling, max length + disambiguating hash. It must be byte-identical across packs
and engine versions, or convergence silently breaks.

**Resolution + dedup-on-write.** The engine keeps an **`id → path` index**; the
write path resolves `id` and **claims it atomically** on create (not via the
eventually-consistent batch index, or concurrent same-id creates race into
duplicates). Convergence is **authority-id only**: a write to an existing
authority id **merges** (provenance union + the field rule below). **Minted-slug
pages never auto-merge** — a slug collision with materially different declared
fields is a **create-time review-flag**, and name variants are surfaced as
**candidates** to the existing dedup drains. *(This revives the deferred
identifier→path manifest — no longer optional.)*

**Ownership + conflict rule (page + field scoped — NOT declared-field scoped).**
- The type's **owner** owns the page; **only the owner may mutate existing fields.**
- A **non-owner** may **add new keys** (attributed + flagged) or hold an explicit
  **per-field grant** (`field_owners:`) — so cross-pack enrichment (hunt-pack owns
  `detection` on `attack-pattern`) works *without* last-writer corruption.
- Non-owner mutation of an owned field → **refused/flagged**, never silent. The
  review queue is the **exception** path; per-field ownership keeps normal
  enrichment deterministic (not review-spam).
- **Owner-authorized removal:** the owner (only) may remove/blank a declared field
  via a logged path exempt from the field-loss guard; non-owner removal stays
  forbidden. (Without it fields only ever accrete.)

**Tombstone safety.** `superseded_by` is an **id** (not free text). A write to a
**tombstoned id** follows the redirect or is rejected — it **never resurrects** the
dead page. The resolver distinguishes live vs tombstoned ids.

**Late binding.** Newly-learned ids go in an additive `aliases:` field (the
resolver consults aliases); discovering two pages are the same entity is a
**merge** (tombstone + redirect), not an id change.

**Honest limit.** Authority ids converge **deterministically**; minted slugs are
**install-local** (the slug derives from an LLM-compiled name — nondeterministic)
and are *never* auto-merged across packs/installs. The id scheme makes dedup
**possible**; entity resolution stays the dedup drains' job.

## 6. Recommended path (earn it incrementally)

1. **v1 — additive, disjoint, fail-loud.** Packs must own **disjoint** types and
   namespaces. Merge = union of types/feeds/crons (pack-prefixed job ids);
   persona = ordered scoped sections; `incoming/<pack>/` with provenance; **hard
   error on any overlap.** This already delivers "threat-actor + threat-hunting"
   when hunting only *reads/links* actor pages. No conflict resolution yet.
2. **v2 — dependencies.** `requires:` + version ranges + install-time validation;
   eventual-consistency cross-link repair (mostly already in the drains).
3. **v3 — shared namespaces + conflict resolution.** Per-namespace ownership,
   precedence rules, merged conformance. Only when a real need forces it.
4. **v4 — shareable packs.** Capability manifest, prompt-only safe tier, signing,
   a distribution format. Security-gated; do not ship before §4.8 is solved.

Keep trust-boundary isolation orthogonal throughout: compose only within one
trust level; cross-boundary stays separate instances.

## 7. Open questions to resolve before coding

- ~~One engine → one vault, or many vaults?~~ **Decided: one vault per install (§5).**
- ~~Shared namespaces or per-pack subtrees?~~ **Decided: shared namespace + merged
  schema (§5).**
- ~~Engine base schema, or packs own everything?~~ **Decided: engine owns a
  `base-schema.yaml` (§5).**
- ~~Canonical-id scheme~~ **Decided (§5a): immutable, type-independent `id` —
  `<authority>:<localid>` when the owning type declares `id_authority`, else a
  stamped-once minted slug; auto-merge authority-ids only; page+field-scoped
  ownership; write-synchronous `id→path` resolver.**
- Type namespacing scheme (`pack:type`) — adopt now or defer? (Interacts with
  type-ownership when two packs want the same type name.)
- `incoming/` layout + item contract (the most load-bearing interface).
- **Seed corpus: snapshot/bootstrap or tracked/subscription?** (Recommend snapshot
  for v1, §4.13.)
- Seed distribution format (config + content bundle?) — licensing is settled
  (redistributable-only + attribution; ship schema+processing for proprietary).
- Shareable-pack safety tier: prompt-only, or sandboxed scripts?
- Where do pack *definitions* live relative to the built vault (the pack-vs-vault
  separation this note assumes but the scaffold/deploy don't implement yet)?

---

## 8. Changelog

- **Rev 2 (post adversarial review)** — the convergence core was found unsound as
  first written and revised **inline** above. Key reversals now folded into §3/§3a/§5/§5a:
  `id` decoupled from `type` (authority id or stamped-once slug — §5a); auto-merge
  restricted to authority ids, slug pages never auto-merge (§5a); ownership made
  page+field-scoped, fixing the last-writer corruption channel (§3, §5a); merged
  schema authoritative over walk-up for shared namespaces, engine-base owns the
  global toggles (§3a); `superseded_by` is an id with no tombstone resurrection,
  owner-authorized field removal, one engine-owned versioned normalizer (§5a). The
  implementation re-scoping (overstated "reuse" → build; invert the write tool;
  write-synchronous id claim; validator WARN tier; `incoming/` spec) lives in
  `composable-okpacks-v1-plan.md`.
- **Rev 1** — initial RFC + the decisions in §5 (topology, base schema, shared
  namespace, seed deferred, licensing).
