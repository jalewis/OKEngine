# Composable okpacks — v1 implementation plan

Implements the decisions in [`composable-okpacks.md`](composable-okpacks.md).
**v1 = additive / disjoint / fail-loud**, with identity + dedup-on-write and a
real base pack as the proving ground. Conflict resolution beyond owner-wins,
seed corpus, and shareable-pack distribution are **out of v1** (see §Deferred).

## v1 goal

Two packs, sharing namespaces, compose into one vault where overlapping
real-world things (MITRE `T1059`) **converge to one canonical page** instead of
duplicating — with the engine owning a base schema, a stable `id`, and an
`id→path` index that makes dedup-on-write deterministic.

---

## What already exists (genuinely reusable — verified against code)

| Need | Existing primitive | Status |
|---|---|---|
| Merge-by-redirect | **`_tombstone(path, reason, superseded_by=)`** (sets `status: tombstoned`, retains file) | ✅ reuse |
| Surgical field/body edits | `_patch` / `_append_section` (re-validate + review-gate) | ✅ reuse |
| Conflict → human | **`flag_for_review`** (G3) + queue — **soft flag, does not gate the write** | ✅ reuse (arbitration must actively skip the clobber) |
| Value-canonicalization *pattern* | `normalize_entity_schema`, `select_publisher_canonical_drain`, `schema_type_drain` | ✅ pattern only |
| Schema accessors | `schema_lib` (canonical_types, type_aliases, namespaces) | ✅ extend (merge is new) |
| Backfill template | `okf_migrate` (single O(n) pass; id backfill needs no link rewrite) | ✅ template |
| Cron merge half | `cron_pack_split.merge()` (engine + one pack) | ✅ reuse the merge core |

## What does NOT exist (BUILD — several were mis-labeled "reuse" before)

| Need | Reality | Phase |
|---|---|---|
| Engine base schema + merge | base fields live in each pack's `schema.yaml`; `governing_schema` is single-file walk-up, no merge | P0 |
| Validator WARN tier | `schema_validator` is binary (required→reject, else silent) — the id WARN→MUST rollout needs a `should:` tier | P0 |
| Mandatory, type-independent `id` | today optional `SHOULD`; must be stamped-once, never recomputed | P1 |
| Write-**synchronous** `id→path` resolver | `corpus_indexer` is a **batch full-rebuild** cron that uses `glob` not `rglob` (**omits sharded pages**); cannot give deterministic dedup-on-write | P1 |
| Engine-owned ascii-folding normalizer | none exists (`feed_fetch.slugify` has no fold, is feed-local) | P1 |
| **Converge merge** `merge(prev_fm, incoming, owner, caller)` | field-loss guard is a single-edit **drop-blocker** wired only into `_patch` — **not** a merge of two writers | P2 |
| `converge_entity` tool + caller-`pack` param | `create_entity` **refuses** on existing path and takes no caller identity; ~15 agent *prompts* depend on refuse-on-exists | P2 |
| Provenance union / page+field ownership / owner-removal | no `maintained_by`, no `field_owners`, field-loss forbids any drop | P2 |
| N-way merge + registry + pack-prefixed prompt keys | `cron_pack_split` is single-`PACK_DIR`; prompts keyed by a **flat job-name dict** (silent last-writer collision) | P3 |
| Type/namespace ownership + `requires:` | no pack dependency/ownership metadata | P3 |

> The earlier draft listed the field-loss guard, the broken-wikilinks matcher,
> `corpus_indexer`, and the `raw:` dedupe key as reusable for convergence. The
> review (verified against code) found all four overstated: the guard isn't a
> merge, the matcher is a non-autonomous *link-repair hint* (not page-to-page
> entity resolution), `corpus_indexer` is batch + `glob`-only, and the `raw:` key
> is per-feed state, not cross-pack. They're re-scoped as BUILD above.

---

## Phases (each independently shippable + testable)

### P0 — Engine base schema
- **Goal:** the engine owns `config/base-schema.yaml` (universal fields: `type`
  [MUST], `id` [MUST], `created`, `updated`, `created_by`, `aliases`); the
  governing schema = **merge(base, pack schema(s), walk-up)**.
- **Build on:** `schema_lib` (add `base_schema()` + `merged_schema(root, ns)`);
  `schema_validator` reads the merged result.
- **New work:** `base-schema.yaml` (universal fields incl. `id`, `aliases`,
  provenance); **merge** logic (net-new — `governing_schema` is single-file
  walk-up today); a **`should:`/WARN tier** in the validator (it's binary today);
  engine-base owns the global toggles (`okf.required`/`strict_types`); scaffold no
  longer re-declares the base.
- **Files:** `config/base-schema.yaml` (new), `scripts/cron/schema_lib.py`,
  `tools/schema_validator.py`, `scripts/framework_init.py` (SCHEMA_YAML slims).
- **Acceptance:** a pack `schema.yaml` with only `types:` validates against the
  merged base+pack contract; base fields apply regardless of pack; `id` validates
  as WARN (pre-backfill) and the merged type set governs `strict_types`.
- **Risk:** existing vaults lack `id` → ship `id` as **WARN**, flip to **MUST**
  only after the P1 backfill. (The WARN tier itself is net-new — scoped here.)

### P1 — Identity + `id→path` resolver
- **Goal:** every page has an immutable, **type-independent** `id` (RFC §5a) and
  the engine resolves `id → path` **write-synchronously**.
- **New work:**
  - `id` derivation: **authority id** (`<authority>:<localid>`) when the owning
    type declares `id_authority`, else a **minted slug stamped once at creation**.
    Never recompute (`id` is immutable). Reconcile the minted slug against the
    existing on-disk path slug so they agree.
  - one engine-owned, **versioned, test-vectored normalizer** (ascii-fold,
    delimiter escaping, empty-result handling, max-length + disambiguating hash).
  - a **write-synchronous `id→path` resolver**: writes claim/resolve the id
    atomically (a batch index alone can't give deterministic dedup). A batch
    rebuild may back it, but the rebuild must use **`rglob`** — `corpus_indexer`
    today uses `glob` and silently omits sharded pages.
  - one-shot **stamp-if-absent** backfill across the vault (`okf_migrate`-style; no
    link rewrite — `id` is path-independent). Never recomputes an existing `id`.
  - the resolver consults `aliases:`; a **collision report** routes slug collisions
    to review (never auto-merge).
- **Files:** new `scripts/cron/id_index.py` (resolver + batch build), new shared
  normalizer util, a one-shot `scripts/backfill_ids.py`, `corpus_indexer.py`
  (`glob`→`rglob`), `schema_lib`.
- **Acceptance:** every non-reserved page has a unique `id`; `resolve(id)` returns
  its path including **sharded** pages; reclassifying a page's `type` does **not**
  change its `id`; slug collisions are flagged, not merged.

### P2 — Converge-on-write (the core change)
- **Goal:** a new **`converge_entity`** tool upserts by `id`: existing **authority**
  id ⇒ merge, new id ⇒ create. (`create_entity` is **left as-is** — refuse-on-exists
  — because ~15 agent prompts depend on it; only migrate prompts that want converge.)
- **Build on:** `_tombstone`/`superseded_by`, `flag_for_review` (soft — arbitration
  must actively skip the clobber), `_patch`/`_append_section`.
- **New work in `write_server.py`:**
  - add a **caller `pack` parameter** to the write tools (owner-arbitration is
    undecidable without it).
  - `converge_entity` resolves `id` (P1, write-synchronous, atomic claim); existing
    **authority** id ⇒ a real **`merge(prev_fm, incoming, owner, caller)`** (net-new,
    not the field-loss guard); minted-slug collision ⇒ create-time review-flag, never
    auto-merge.
  - **provenance union:** `maintained_by: [pack…]` / `discovered_by` (a set; trimmed
    on pack uninstall — see removal guard).
  - **page+field conflict rule (RFC §5a):** only the **owner** mutates existing
    fields; non-owners **add new keys** or hold a **`field_owners`** grant;
    non-owner mutation of an owned field ⇒ refused/flagged. **Owner-authorized
    removal** path exempt from the field-loss guard; non-owner removal forbidden.
  - **tombstone safety:** `superseded_by` is an **id**; a write to a tombstoned id
    follows the redirect or is rejected — never resurrects.
- **Files:** `okengine-mcp/write_server.py`, `tools/schema_validator.py` (field
  ownership), `scripts/cron/schema_lib.py`.
- **Acceptance:** two `converge_entity` calls with the same authority `id` ⇒ one
  page, provenance unioned, additive + per-field-granted fields coexist; a non-owner
  mutating an owned field ⇒ refused/flagged; a write to a tombstoned id ⇒ redirected,
  never resurrected; `create_entity`'s refuse-on-exists is unchanged (existing
  prompts pass).
- **Dependency:** needs a minimal type→owner map (a P3 stub is fine) — the writer
  cannot arbitrate without `pack` identity + an owner lookup.

### P3 — N-way pack composition
- **Goal:** the engine composes **engine + N packs** into one vault.
- **Build on:** `cron_pack_split` (generalize from one pack to N), `schema_lib`
  merge (P0).
- **New work:**
  - pack metadata (`pack.yaml` or extended `engine.version`): `id`, `version`,
    **owned types/namespaces**, `requires: [pack@range]`, trust level.
  - N-way `cron_pack_split`: union crons with **pack-prefixed job ids**, merge
    engine-template prompts **per pack**, **fail loud on any type/namespace
    overlap** (v1 = disjoint ownership), validate `requires:` are present.
  - an in-memory **pack registry** (presence-based discovery → enumerate
    installed packs for merge/deps/conflict detection).
- **Files:** `scripts/cron_pack_split.py`, `scripts/framework_validate.py`
  (validate pack metadata + deps + disjointness), `scripts/framework_init.py`
  (scaffold `pack.yaml`).
- **Acceptance:** compose two disjoint packs → merged `cron-plus-jobs.json`
  round-trips; an overlap or missing `requires:` is a hard error.
- **Risk:** engine-template singletons → need per-pack instances; scope to the
  packs that actually supply that template's prompt.

### P4 — Proving ground: `okpack-attack` + a dependent
- **Goal:** demonstrate convergence end-to-end on the MITRE case.
- **New work (in the pack repos, not the engine):**
  - a minimal `okpack-attack` base pack: owns `attack-pattern`, declares
    `id_authority: mitre`, a pull cron for ATT&CK, no seed (built live in v1).
  - a second pack that `requires: okpack-attack` and references `T1059`.
- **Acceptance:** both packs installed → one `attack-pattern:T1059` page,
  `maintained_by` lists both, the dependent's links resolve. The RFC §5 worked
  example actually runs.
- **Note:** this validates the whole chain (base schema → id → index → converge →
  N-way merge → deps) on real data.

---

## Sequencing & dependencies

```
P0 base schema ─► P1 id + index ─► P2 converge-on-write ─► P4 proving ground
                         └────────► P3 N-way compose ─────────┘
```
P2 and P3 both depend on P1; P4 needs all. P0→P1 must land before `id` flips from
WARN to MUST (so existing vaults don't break).

## Out of v1 (deferred)
- Seed/preprocessed corpus (snapshot or subscription) — RFC §4.13.
- Shareable-pack distribution + security tier (prompt-only/sandbox/signing).
- Conflict resolution beyond owner-wins/review (precedence graphs, 3-way merge).
- `pack:type` namespacing (v1 forces disjoint type names; revisit when two packs
  legitimately want the same type name).
- Tracked-baseline pack upgrades / schema migration across pack versions.

## Open choices to confirm before P0
- `id` punctuation: `<authority>:<localid>` / minted-slug delimiter — confirm the
  separator + the escaping rule for keys containing it (RFC §5a normalizer).
- Where the built vault lives relative to the pack definition dir (the pack-vs-vault
  separation the RFC assumes but the scaffold/deploy don't yet implement) — does it
  block P0, or ride along as the scaffold slims?
- `pack.yaml` vs extending `engine.version` for pack metadata.

---

## Cross-cutting requirements (not tied to one phase)

- **`incoming/` contract — a first-class spec before any pull/compile work:**
  `incoming/<pack>/` layout; a required envelope (source URL, `fetched_at`,
  `pack_id`, a content/URL **dedupe key**, payload type); **atomic
  temp-then-rename** landing; **consumer-deletes idempotently** keyed on the dedupe
  key; a per-pack **backpressure cap**.
- **Pack-removal guard (even though removal is deferred):** the engine **refuses**
  to remove a pack whose owned types others `require:`; removed-pack pages are
  **frozen** (validation-exempt) pending tombstone or ownership transfer. Needed
  the moment P4 installs a base pack others depend on.
- **Secrets/trust — a v1 precondition, not a feature:** v1 composes only **one
  trust level** with a **shared secret space**; the engine asserts this. Per-pack
  secret namespacing is deferred (RFC §4.7/4.8).

## Changelog
- **Rev 2 (post adversarial review):** re-scoped the overstated "reuse" claims to
  BUILD (converge merge, write-synchronous id resolver, ascii normalizer,
  caller-pack identity, N-way enumerator); adopted RFC §8 identity/ownership/merge
  resolutions inline in P0–P2; inverted the write tool (keep `create_entity`, add
  `converge_entity`); added the validator WARN tier to P0; added the cross-cutting
  requirements above.
- **Rev 1:** initial phased plan.

### Verdict
**P0 (engine base schema + WARN tier) is buildable now.** P1/P2 carry the §8
redesign (id decoupled from type, authority-only auto-merge, page/field ownership,
caller identity, write-synchronous id claim, `converge_entity`). Phase *ordering*
holds; the id/conflict core is a redesign, and the real effort is larger than the
first "reuse" framing implied.
