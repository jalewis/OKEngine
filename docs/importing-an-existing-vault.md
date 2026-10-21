# Importing an existing (foreign) vault into a pack

Adopt a large markdown/YAML corpus that was **not** created by OKEngine — bring it under a target
pack's schema (typed pages, stable `id:`, partitioning, validation). This is an *assembly* over
existing primitives, not new machinery; the apply rides `framework upgrade`'s snapshot / rollback /
validate harness (okengine#154).

> Reference case: a ~48k-page vault where only a handful carry an `id:`. Treat scale (snapshot
> size, single-pass memory, validation time) as an acceptance criterion — dry-run first, always.

## The two-step flow

### 1. Plan — `framework import` (read-only)
```
python scripts/framework.py import <pack-dir> --vault <foreign-vault-dir>
```
Produces a complete, reviewable **change report without writing**:
- page count + typed/untyped split;
- **type distribution vs the pack schema** — each foreign type tagged `ok` / `via type_aliases` /
  `NOT IN PACK` (needs a retype map entry or a new pack type);
- **id backfill**: how many pages would be stamped, slug collisions (auto-disambiguated), and
  **authority-id collisions surfaced as a human-merge worklist** (never silently collapsed);
- a `next` pointer.

Add `--scaffold` to write a pack-local skeleton migration
(`<pack>/.okengine/migrations/m_900_import_foreign_vault.py`).

### 2. Apply — fill the skeleton, then `framework upgrade`
The skeleton sequences the import in the order that matters:
1. **retype / realias** — `RETYPE_MAP` ({foreign_type: pack_type}) for the deterministic part, and
   `RETYPE_SLUGS` ({slug: type}) for the curated/classifier part (e.g. which generic `concept`
   pages are market `segment`s).
2. **field reconciliation** — `FIELD_REMAP` ({type: {rename, default}}) to map a foreign schema's
   field shapes onto the target.
3. **id backfill** — `backfill_ids` (IRREVERSIBLE). Resolve the authority-duplicate worklist from
   the import report **first**; ids depend on final types, so this runs after retype/reconcile.
4. **partition** — assert the source layout already conforms, or run `okf_migrate` per oversized
   namespace (link-preserving, single O(n) pass; see **Link integrity** below for what to compare).
5. **validate** — `framework validate` is the roll-forward gate `upgrade` runs automatically.

```
python scripts/framework.py upgrade <pack-dir>            # dry-run: change descriptions, no writes
python scripts/framework.py upgrade <pack-dir> --apply    # snapshot -> apply -> validate; auto-rollback on gate failure
```

## Guarantees & gotchas
- **Dry-run first.** `import` never writes; `upgrade` (no `--apply`) only reports.
- **Snapshot + auto-rollback.** A failed validation gate restores the pre-import source.
- **Link integrity.** `okf_migrate` does not verify links itself. It rewrites every
  `[[<namespace>/…]]` wikilink to a moved page in one pass and prints
  `link rewrite: N links in M files`; the dry-run count is what `--apply` should report, with
  `move errors: 0` and no unwritable files listed. To confirm nothing broke, run
  `backlinks_refresh.py` (with `VAULT_DIR=<vault>`) before and after and compare the `edges` count
  it prints (also stored in `wiki/.backlinks.json`). It should not drop. `okf_migrate` does not
  rewrite bare frontmatter path references (`subject: entities/foo`); check the corpus audit's
  "Dangling path references" section after the move.
- **`id` is required** under base-schema — the backfill + duplicate merge must complete **before**
  the vault is governed. This is the one irreversible step; do it deliberately.
- **Gated on the pack schema.** retype/reconcile/validate can't be finalized until the target
  pack's `schema.yaml` exists; run `import` again after it does for accurate authority-collision
  detection.

## Primitives reused (do not rebuild)
`scripts/backfill_ids.py` (id stamping + collision report) · `scripts/cron/okf_migrate.py`
(link-preserving re-layout) · the normalize/dedup suite (`normalize_entity_schema`,
`normalize_publishers`, `normalize_bare_name_links`, `backfill_typeless_type`,
`canonical_assemble`, `okf_dedup_entity_shards`) · the #66 migration harness
(`framework_upgrade.py`). `scripts/import_lib.py` is the thin ordered glue.
