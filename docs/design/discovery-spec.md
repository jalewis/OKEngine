# Extension discovery — implementation spec

**Issue:** okengine#134 · **Gate:** okengine#131 · **Sharpens:** okengine#113
(discovery + enablement-state + cron-composition lifecycle) · **Parent design:**
[`extension-system.md`](extension-system.md) §9, §10
**Status:** design — implementation spec

#134 closes §13 open question 4: the exact engine/pack/operator roots, cross-tier
shadowing, and duplicate-id behavior. It delivers the **file-layer contract** #113's
lifecycle assumes but leaves abstract.

## 1. Current state

**No code exists yet.** `.okengine/` appears only in the design doc (`extension-system.md:316,337`)
— no `extensions.yaml`, no discovery scanner, no `framework extensions` subcommand (confirmed
by grep across `*.py *.md *.yaml *.json *.sh`).

**Framework CLI — `scripts/framework.py`.** Subcommands today: `init`, `pull`, `list`,
`validate`, `budget` — a flat dispatch dict (`framework.py:40-46`) routing `argv[0]` to a
`framework_<cmd>.py` module's `main(rest)` (:49-59). Adding `extensions` = one dict entry +
a new `framework_extensions.py`. Pack resolution: positional `<pack-dir>` arg, every pack
file is `pack / "<name>"` (`framework_validate.py` reads `pack/"schema.yaml"` :81,
`pack/"crons"` :305). Engine dir = `_HERE.parent` (`framework.py:30`) = repo root.

**The precedent — `cron_pack_split.py`.** The existing "engine layer + pack layer → generated
artifact" model #134 mirrors. Source roots: engine = `config/engine-crons.json` (:53); pack =
`<pack>/crons/*` (:217). Tiers classified by `config/cron-tiers.yaml` (`_tier_map` :99-105).
**Collision handling is explicit and fail-loud:** `merge_packs` (:150-190) pack-prefixes
domain jobs `<pack>:<job>` (:185), and a `seen` dict appends a **hard error on any id
collision** (:179-180,186-187); a non-empty error list blocks deploy (`regen_composed` raises
`SystemExit` :243). Unclassified job → `SystemExit` (:123). **Fail-loud is the norm.**

**Ownership precedent — `pack_meta.validate_composition`** (`pack_meta.py:82-114`): additive /
disjoint / fail-loud — two packs may not own the same type/namespace (:91-98); duplicate
ownership appends an error, never a silent win. §5 already states "exactly one owner …
fails loud" (`extension-system.md:103-108`); §9 "namespaced … collisions FAIL" (:300).

**Pack layout** (`docs/deploy-a-new-domain.md:22-34`): `<pack>/` holds `schema.yaml`,
`CLAUDE.md`, `engine.version`, `pack.yaml`, `feeds/`, `data/`, `crons/`, `.env`, `wiki/`.
**`.okengine/` and `extensions/` are not in the layout yet.** Engine artifacts live under
`config/` and `scripts/cron/`; **there is no engine `extensions/` dir** — it must be created.

## 2. Gap

#134 must specify, testably: (1) the three exact roots, (2) shadowing, (3) duplicate-id
resolution, (4) how enabled-state references a multi-tier id, (5) integration with the
`cron_pack_split` regen flow, (6) a regression test.

## 3. Design

### 3.1 The three exact discovery roots

| Tier | Root (concrete path) | Anchor |
|---|---|---|
| 1 — engine | `<engine>/extensions/<id>/extension.yaml`, `<engine>` = repo root (`framework.py:30` `_HERE.parent`) | new dir; peer of `config/`/`scripts/` |
| 2 — pack | `<pack>/extensions/<id>/extension.yaml` | sibling of `<pack>/crons/` |
| 3 — operator/vault | `<pack>/.okengine/extensions/<id>/extension.yaml` | exactly `extension-system.md:337` |

Discovery = scan all three, presence-based (an `extension.yaml` makes it discovered),
identical to `discover_packs` enumerating subdirs carrying a `pack.yaml`
(`cron_pack_split.py:203-223`). **Discovered ≠ enabled** (§9). Each record is
`{id, tier, dir, manifest}`, keyed by manifest `id`
(`^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$`, `okengine.*` reserved first-party).

### 3.2 Precedence / shadowing — **no shadowing; reject duplicates**

**An id may appear in at most one tier. A duplicate id across any two tiers is a hard FAIL
at discovery, before enable and before any generated file is written.** No tier shadows
another; there is no "highest-tier-wins."

Rationale (grounded, not invented):
- The whole model is disjoint / fail-loud / one-owner-per-id: `validate_composition` rejects
  duplicate type/namespace ownership (`pack_meta.py:91-98`); `merge_packs` rejects job-id
  collisions (`cron_pack_split.py:179-180`); §5/§9 mandate single-owner fail-loud.
- Silent shadowing is exactly the split-brain / ambiguous-provenance class the design exists
  to prevent (`extension-system.md:28-30`). Two extensions sharing an id makes the per-page
  provenance stamp (§4, built in #132) non-unique and `extensions purge <id>` (#127)
  undecidable.
- Highest-tier-wins would let an operator silently override a paid first-party `okengine.*`
  op by dropping a same-id tier-3 file — a support/security footgun with no upside (the
  operator can just rename their copy).

**`okengine.*` ids are reserved to tier-1** (`extension-system.md:225`): a tier-2/tier-3
extension claiming an `okengine.*` id is a FAIL regardless of duplication (namespace-squatting
guard).

### 3.3 Duplicate-id behavior (spec line)

`FAIL: extension id '<id>' found in multiple tiers: engine (<path>) and operator (<path>)`.
Emitted by the discovery scanner; surfaced by `framework extensions list/validate`; blocks
`regen`/deploy (same gate as `regen_composed` `SystemExit` on a non-empty error list, :243).

### 3.4 Enabled-state referencing

Because (3.2) forbids an id in two tiers, **enabled-state never has to disambiguate.**
`<pack>/.okengine/extensions.yaml` references extensions by **bare `id`**:

```yaml
# <pack>/.okengine/extensions.yaml — operator-owned, vault-level, edited via the CLI
enabled:
  okengine.contradictions:        # bare id; resolves to exactly one tier
    config: {horizon_days: 90}    # per-deployment overrides
```

The loader cross-checks each enabled id against the discovery result; an enabled id resolving
to **zero** discovered extensions is a FAIL (referenced-but-absent), and by (3.2) it can never
resolve to more than one. A tier-qualified handle (`engine/okengine.contradictions`) is
**deliberately rejected** as over-engineering under the no-shadow rule — recorded so a future
reviewer doesn't re-add it.

### 3.5 Integration with the cron regen flow

An `operation`-kind extension with a cron schedule (§6) contributes a cron job. Wire discovery
in as a **fourth source**, after pack crons, in `cron-plus-jobs.json`:

1. `framework extensions enable` writes `extensions.yaml`.
2. The regen path (`cron_pack_split.regen`/`compose`, :226-269) gains an extensions pass: for
   each **enabled** extension, synthesize a cron job from `operation.schedule` + `entrypoint`,
   named with the extension namespace `<id>:<local>` (§9), reusing the `merge_packs` collision
   guard (:179-180) so an extension job can't collide with an engine/pack/other-extension job.
3. **No new static tier** in `config/cron-tiers.yaml` — extension jobs are discovered
   dynamically, classified by their `<id>:` namespace prefix inside the merge so
   `test_every_live_job_classified_exactly_once` (`test_cron_pack_split.py:30`) stays valid.
4. Fail-before-runtime: an enabled-but-invalid extension (bad manifest, missing `schema_refs`,
   duplicate id) makes regen raise before writing `cron-plus-jobs.json` (§9), exactly as
   `regen_composed` does (:243).

## 4. Relationship to #113 (sharpens, does not duplicate)

#113 owns the **lifecycle** — discovery + enablement-state + cron-composition as a flow (the §9
invariants and the `framework extensions` verb set). #134 is the **narrow file-layer spec
#113 assumes but leaves abstract**: the three concrete roots (3.1), the cross-tier collision
rule (3.2/3.3), the bare-id-resolution guarantee that lets #113's `extensions.yaml` stay
unqualified (3.4), and the dynamic-tier hook into `cron_pack_split` (3.5). #134 does **not**
redefine enable/disable verbs, provenance/purge, or generated-from-source — those stay in #113.
Concretely: **#134 delivers the discovery scanner + duplicate-id guard + root constants;
#113 consumes that scanner in the enable/disable/regen lifecycle.**

## 5. Test plan

New `tests/extensions/test_discovery.py` (mirror `tests/cron/test_cron_pack_split.py`:
load module by path, skip if the discovery module is absent):

1. `test_three_roots_discovered` — one extension in each root → all three discovered with
   correct `tier`.
2. `test_duplicate_id_across_tiers_rejected` — same id in tier-1 and tier-3 → FAIL naming both
   paths (**the load-bearing regression**).
3. `test_okengine_namespace_reserved_to_engine` — `okengine.foo` in tier-2/tier-3 → FAIL.
4. `test_enabled_state_resolves_bare_id` — enable a bare id present in exactly one tier →
   resolves; enable an absent id → FAIL.
5. `test_present_not_enabled` — a discovered-but-not-enabled extension contributes zero jobs to
   `compose` output (§9 present≠enabled).
6. `test_extension_cron_namespaced_no_collision` — an enabled operation-extension's synthesized
   job is `<id>:<local>`; a forced collision with a pack job FAILs.
7. Extend `test_every_live_job_classified_exactly_once` (`test_cron_pack_split.py:30`) to accept
   `<id>:`-namespaced extension jobs as classified.

## 6. Open questions

1. **Engine tier-1 root name** — top-level `<engine>/extensions/` (recommended; first-party ops
   read as peers of the engine) vs `config/extensions/`. Either needs an `engine-manifest.yaml`
   `engine_layer` entry.
2. **Discovery-time vs enable-time duplicate check** — recommend discovery-time (so `list`
   shows the conflict before anyone enables), but the deploy gate must re-run it (a tier-3 dir
   can be dropped between list and deploy).
3. **Symlinked / `--into` packs** — `framework pull` can deploy a pack into an arbitrary dir;
   confirm tier-3 `.okengine/extensions/` resolves relative to the *deployed* pack dir.
4. **Tier-3 manifest trust default** — tier-3 is the private/paid home; does discovery require
   an explicit `trust:` or default to `sidecar`? Ties into #135; defer.

**Anchors:** dispatch `scripts/framework.py:30,40-59`; pack resolution
`scripts/framework_validate.py:81,305`; precedent `scripts/cron_pack_split.py:99-105,123,150-190,
203-223,243`; tiers `config/cron-tiers.yaml`; ownership `scripts/cron/pack_meta.py:82-114`;
layout `docs/deploy-a-new-domain.md:22-34`.
