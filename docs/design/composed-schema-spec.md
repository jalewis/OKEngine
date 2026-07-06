# Composed schema — implementation spec

**Issue:** okengine#133 · **Gate:** okengine#131 · **Joint with:** okengine#90
(composable-okpacks — owns the merge engine) · **Parent design:**
[`extension-system.md`](extension-system.md) §5
**Status:** **implemented (MVP)** — the #90 P2/P3 schema fold + the #133 composed artifact are
built (see "Implemented" below). **NOTE:** §1 "Current state" and §2 "Gap" describe the
PRE-implementation baseline this spec was written against; they are historical design context, not
current state — see the "Implemented" paragraph for what now exists.
**Depends on:** #132 (the write-path provenance stamp keys the orphan check); #90 P3
(the N-way merge fold this builds on)

**Implemented:** the N-way fold `schema_lib.compose_schema(root, fragments)` (the #90 P3
schema slice — engine base ⊕ pack ⊕ Σ extension fragments, with an `owners` map and
fail-loud Own/Reuse/Extend rules); generation of the `<vault>/.okengine/
composed-schema.yaml` artifact from enabled extensions' `schema:` fragments
(`extension_compose.write_composed_schema`, wired into `framework extensions
enable/disable` with a fail-before-runtime dry-run); and the write-path validator
(`tools/schema_validator`) now **prefers the composed artifact** when present (so
extension-owned types validate), falling back to the pack `schema.yaml` walk-up
otherwise. Back-compat: no enabled-extension schema → no artifact → pre-#133 behavior.
**Deferred:** the disable→**orphan verdict** (surfacing pages of a disabled extension's
type) — a flagging enhancement, not correctness: such pages are preserved and pass
validation as untyped (`strict_types=False`). Tracked as a follow-up.

The decision is fixed (§5): **the merge engine is owned by #90 and extensions consume
it.** This spec is the extension-facing contract on that engine — it does **not** build a
parallel merger #90 later replaces.

## 1. Current state

**Validator — `tools/schema_validator.py`.** Schema discovery is a walk-up to the first
`schema.yaml`/`.okf-schema.yaml` (`_find_schema` :41, :78-107), mtime/TTL-cached
(`_FIND_TTL` ≈10s :53). Type model is `types: {<type>: {required: [...]}}` (:266,:273) —
**`required` is just key-presence** (`_present` :142); there is **no per-field type system
and no namespace declaration inside `types`**. The §5 grammar (`{type: ref, to: entity}`,
per-field types) **does not exist**. Enums are the one structured mechanism: `enums` +
`field_enums: {<field>: {enum|by_type, extensible}}` (:162-198); **`extensible: true`
already exists** and means "skip the membership check" (:187) — the only `extensible`
marker in the codebase. Engine base (`config/base-schema.yaml`) merges *under* the pack for
`okf.required`/`should`/`strict_types` only (`_base_schema` :61-75). **No owner is tracked
at validator level.**

**Pack schema — `templates/pack/skeleton/schema.yaml`.** Namespaces are declared under
`partitioning.namespaces` (:20-28), **not** under `types`. `types:` carry `required:` plus
**already-wired ownership keys** `owner: {{PACK}}` and `field_owners: {field: pack}`
(:84-92) — but **no `fields:`, no per-field `type:`**. So the §5 `owns/reuse/extends`
grammar is net-new.

**Write path — `okengine-mcp/write_server.py`.** Every write helper re-validates via
`schema_reject_reason(str(p), content)` (`_create`:502, `_update`:555, …). It passes **no
explicit schema path** — the effective schema is whatever the validator's walk-up finds
from the page ⊕ base. Reload is mtime/TTL polling; no explicit signal. `converge_entity`
(`_converge`:800) already reads `type_owner`/`field_owners` and runs
`converge.merge_frontmatter` (`converge.py:44-109`) — **the page+field ownership conflict
engine exists**, but keys on `pack`, not `ext:<id>`.

**#90 — baseline (when this spec was written).** `schema_lib.merged_schema(root, ns)` was a
**two-layer** merge (base ⊕ one walk-up pack), not N-way; there was no materialized composed-schema
artifact; `pack_meta.validate_composition` enforced disjoint pack-granularity ownership only.

**#90 — now (okengine#90 P2/P3, implemented).** `schema_lib.compose_schema(root, fragments)` is the
**N-way fold** (engine base ⊕ pack ⊕ Σ extension fragments) with an `owners` map and fail-loud
Own/Reuse/Extend. `schema_lib._merge_base_pack` merges the engine-owned **core** (types, namespaces,
tiering + the cross-cutting optional fields/enums) UNDER the pack, so a pack inherits the core and
owns only its domain — and a pack/extension that tries to OWN a core id now correctly conflicts with
`engine` ownership. The materialized `<vault>/.okengine/composed-schema.yaml` IS generated from
enabled extensions and the write validator prefers it.

## 2. Gap

| Need (§5) | Today | Gap |
|---|---|---|
| Composed artifact `okengine-write` reads | live walk-up of one `schema.yaml` ⊕ base | no materialized artifact; no Σ(extensions) |
| Per-field type model (`{type: ref, to: x}`) | `required:` key-lists + enums only | net-new field-type/ref system |
| `owns`/`reuse`/`extends` grammar | flat `types:` | net-new |
| Owner on type/field/namespace/enum-value | `type.owner` + `field_owners` (pack-level, converge-only) | not in a composed artifact; no `ext:<id>` |
| Conflict = FAIL at enable/deploy | `validate_composition` (disjoint types/ns, pack-granular) | no field/enum-value owners, no ext layers |
| Disable removes a layer; pages orphaned | nothing | net-new orphan verdict |
| Reload on enable/disable | mtime/TTL polling | needs regenerate-and-reload |

## 3. Design

### 3.1 Composed-schema artifact

- **Path:** `<pack>/.okengine/composed-schema.yaml` — generated, never hand-edited, beside
  the enabled-state (`extensions.yaml`, §9) and token store (#132).
- **Generator:** add `schema_lib.compose_schema(vault) -> dict` folding, in precedence
  order, **engine base (`config/base-schema.yaml`) ⊕ pack `schema.yaml` ⊕ Σ(enabled-extension
  fragments)**, reusing the existing two-layer `merged_schema()` as the base⊕pack core. This
  **is the N-way fold #90 P3 needs** — build it in `schema_lib` (the engine's shared schema
  module) so #90's N-pack merge and #133's Σ-extension merge are the **same code path**
  (packs and extensions are both additive layers with an owner).
- **When:** regenerated by the framework CLI on `extensions enable/disable` and at
  `deploy-*.sh` time (§9 generated-from-source / fail-before-runtime). The artifact is the
  single thing the write path validates against: teach `schema_validator._find_schema` to
  prefer `<pack>/.okengine/composed-schema.yaml` when present (walk-up remains only for
  pack-private subtrees, exactly as composable-okpacks §3a already decided).

### 3.2 Owner metadata

Owner grammar: `engine` | `pack:<name>` | `ext:<id>`. The compositor stamps an explicit
owner map; source fragments use the §5 `owns/reuse/extends` blocks:

```yaml
# <pack>/.okengine/composed-schema.yaml  (generated)
owners:
  namespaces:  {predictions: "ext:okengine.predictions", entities: "pack:okpack-sec"}
  types:       {prediction: "ext:okengine.predictions",  entity: "pack:okpack-sec"}
  fields:      {"entity.predicted_by": "ext:okengine.predictions"}        # Level-3 extend
  enum_values: {"source_kind.forecast-derived": "ext:okengine.predictions"}
types: {...}                 # merged
enums: {...}; field_enums: {...}
```

This generalizes the existing pack-level `type.owner`/`field_owners` (`schema_lib.py:134,142`):
those become *inputs* the compositor reads, with `owner` now spanning `engine`/`pack:`/`ext:`.
`converge.merge_frontmatter` already arbitrates on an owner token — feed it `ext:<id>` and
cross-extension field writes get the same page+field conflict rule for free.

### 3.3 Precedence + conflict (when composition FAILs)

Map the three §5 levels onto the existing fail-loud machinery (extend
`pack_meta.validate_composition` to field/enum granularity, run by
`framework extensions validate`):

- **Own** (new ns/type ids): FAIL if the id already has any owner. (Same disjointness rule
  as `validate_composition`, extended to ext ids.)
- **Reuse** (`{type: ref, to: X}`): FAIL if `X` is absent from the composed type set — the
  §5 `requires.schema_refs` check. New validation.
- **Extend** (additive field / enum-value): FAIL unless (a) the target type/enum is
  `extensible: true` by its owner (reuse the enum marker at `schema_validator.py:187`; add
  the same marker to types), (b) the new field is `optional: true`, (c) the field/value id is
  unclaimed → owner stamped `ext:<id>`. FAIL on re-typing or flipping required-ness.
- Engine globals (`strict_types`, `okf.required`) stay engine-only (already enforced
  `schema_lib.py:69-74`); an extension touching them FAILs.

All fire **at enable and at deploy, before any generated file is written** (§9).

### 3.4 Load + reload

`framework extensions enable/disable` → (1) update `extensions.yaml`, (2) validate
composition, (3) regenerate `composed-schema.yaml`, (4) signal reload. Reload: the write
server re-resolves by mtime within `_FIND_TTL`, so **touching the artifact's mtime is the
signal** for the stdio server (respawned per cron, so a restart also suffices). For
determinism clear `schema_lib._SCHEMA_CACHE`/`_BASE_CACHE` and
`schema_validator._schema_cache` on regenerate. (A long-lived networked write server — once
#132 lands — needs explicit cache invalidation on regenerate; tracked as an open item.)

### 3.5 Disable → orphan (not delete)

On disable the fragment leaves the fold; its owned types/namespaces vanish from the
artifact; its pages persist (§9 preserve-content). Add a third validator outcome: in
`_evaluate` (`schema_validator.py:201`), when a page's `type` is owned by a disabled
extension — type absent from composed `types` **and** the page carries the extension
provenance stamp (the `extension_id` field written by `okengine-write`, **built in #132**) —
return `("orphan", reason)`. `schema_reject_reason` (runtime :285) must **NOT reject**
orphans (don't brick reads/edits of preserved pages); only surface them via a new
`orphan_reason()` that health/lint and reader-nav consume to flag/hide. Reuses the existing
skip-vs-fail tri-state rather than adding a reject path.

## 4. Alignment with #90 (shared, not parallel)

- **Shared — the merge engine:** `schema_lib.compose_schema()`/`merged_schema()` is the one
  fold. #90's N-pack merge (v1-plan P3) and #133's Σ-extension merge are the **same function
  with more layers**. Build the N-way fold in `schema_lib` under #90's ownership; #133
  supplies only the extension *fragment grammar* (`owns/reuse/extends`) and consumes the fold.
- **Shared — conflict arbitration:** `pack_meta.validate_composition` and
  `converge.merge_frontmatter` are *extended* to recognize `ext:<id>`, not reimplemented.
- **Extension-specific (#133 only):** the artifact path under `.okengine/`, the enable/disable
  reload trigger, the `orphan` verdict, the §5 three-level grammar, the provenance stamp
  consumption. These bolt onto #90's engine; #90 does not later replace them.
- **Sequencing:** #133 gates on #90 P3 (the N-way fold). Do **not** build an extension-only
  merger first.

## 5. Test plan

1. **`tests/cron/test_schema_lib_base.py`** — `compose_schema` three-layer fold produces
   merged `types`/`enums` + correct `owners`; engine globals stay engine-owned across an ext
   layer.
2. **`tests/test_framework_validate.py`** — Own-collision FAILs; Reuse with missing `ref.to`
   FAILs; Extend a non-`extensible` type/enum FAILs; Extend that flips required-ness FAILs;
   a valid three-level fragment passes.
3. **`tests/test_schema_validator_base.py`** — the `orphan` verdict: a disabled-extension-type
   page with a provenance stamp returns orphan (not fail) at runtime, is surfaced by
   `orphan_reason()`, and is NOT rejected by `schema_reject_reason`.
4. **`tests/test_converge.py`** — `ext:<id>` as owner/`field_owners` routes through
   `merge_frontmatter` with the same page+field semantics as `pack:`.
5. **New `tests/test_composed_schema_artifact.py`** — enable writes the artifact; disable
   removes the layer; regeneration clears caches; the write path validates against the
   artifact, not the bare walk-up.

## 6. Open questions

1. **Per-field type system scope** — does #133 introduce a full `{type: string|date|ref|enum}`
   model (the validator has none today), or MVP only `ref.to` existence + the existing enum
   mechanism? *Recommend MVP-minimal: `ref.to` existence + enum, defer full field-typing.*
2. **Provenance stamp location** (§13 Q3) — OKF envelope field (e.g. `extension_id`/
   `maintained_by`) vs a sidecar index. *Recommend the envelope field; `write_server` already
   stamps `maintained_by`/`discovered_by`.*
3. **Artifact authority vs walk-up** — confirm the artifact fully supersedes per-page walk-up
   for the shared tree (composable-okpacks §3a: walk-up = pack-private-subtree only).
4. **Reload under a long-lived write server** — mtime-poll is fine for stdio; the networked
   server (#132) needs explicit invalidation on regenerate.

**Anchors:** validator `tools/schema_validator.py:41,142,162-198,201,266-294`; pack schema
`templates/pack/skeleton/schema.yaml:20-28,84-92`; write load
`okengine-mcp/write_server.py:43,502,800-863`; merge `scripts/cron/schema_lib.py:53-78,134-142`;
ownership `scripts/cron/pack_meta.py:82-114`; #90 `docs/design/composable-okpacks{,-v1-plan}.md`.
