# Engine migrations registry (okengine#66)

Versioned migrations applied by `framework upgrade <pack> --apply` when a pack's
`engine.version` pin lags the running engine. Each migration runs once per vault (idempotent
via `<pack>/.okengine/migrations-state.json`), in `to_version` order, for the range
`(pinned, target]`.

## A migration file

One module per migration, named `m_<from>_<to>_<slug>.py` (the `m_*.py` glob is what the loader
discovers; the filename sorts them). It exposes:

```python
ID = "v0.5.0-example-slug"          # stable, unique; recorded in migrations-state.json
FROM = "v0.4.0"                      # the series this migrates from
TO = "v0.5.0"                        # applies when pinned < TO <= engine target
DESCRIPTION = "one line — what it changes and why"

def apply(pack: pathlib.Path, dry_run: bool) -> list[str]:
    """Return human-readable change descriptions. PERFORM the changes only when
    dry_run is False — in dry-run, just describe what would happen."""
    return []
```

## Where to ship a migration

- **Engine migrations** live here (`migrations/`) — they apply to every pack on the version bump.
- **Pack-local migrations** live in `<pack>/.okengine/migrations/` — for a transform specific to
  one pack's schema/content. `framework upgrade` merges both, ordered by `to_version`; a
  pack-local migration with the same `ID` as an engine one **overrides** it.

## Phases (#66)

- **Phase 1 (done):** the registry + `framework upgrade`'s pin reconciliation + idempotent state.
- **Phase 2 (done):** `apply()` carries real **vault/schema transforms**; **dry-run** (the
  default) previews them via `apply(pack, dry_run=True)`; `--apply` performs them then runs a
  **roll-forward validation gate** (`framework validate`, skip with `--no-validate`); **pack-aware
  hooks** (`<pack>/.okengine/migrations/`). There are no engine-shipped migrations yet — v0.5.0 is
  the registry baseline (nothing to migrate *from* within it); the contract is exercised by tests.
- **Phase 3 (done):** `--apply` **snapshots the pack source** to `.okengine/snapshots/<ts>/` before
  running migrations, and if the roll-forward gate fails it **automatically rolls back** (reverts
  modified files, deletes added ones, recreates deleted ones) so a bad migration never leaves a
  half-upgraded pack. `--no-snapshot` disables it (then a gate failure is not auto-recovered);
  `--keep-snapshots N` bounds retention (default 3). The snapshot scope is the pack source —
  runtime/VCS trees (`.git`, `.hermes-data`, `data`, `logs`, …) are excluded, so migrations should
  only transform source.

## Pack-VERSION migrations on update (okengine#312)

The phases above key on the **engine** release. Packs additionally ship their own migrations —
`<pack>/migrations/m_*.py`, the **same module contract**, but with `FROM`/`TO` keyed on **pack**
versions (`pack.yaml version:`, okpacks-library `VERSIONING.md`). They run through the same
snapshot / dry-run / roll-forward machinery when a deployed pack is updated:

- `framework pull <pack> --update` computes the span `(installed, incoming]`, surfaces each
  CHANGELOG section's `Migration impact:` line, and **previews** pending migrations (dry-run).
  `--apply-migrations` performs them: snapshot → apply → record → validation gate → automatic
  rollback on any failure.
- `framework install-domain` over an **existing member** does the same for the guest's
  migrations against the composed host vault (preview in plan mode, full run under `--apply`).
- State lives in `<pack>/.okengine/migrations-state.json`: `pack_versions.<name>` records the
  installed version (a dry-run **floors** it, so `framework reconcile` accepting
  `pack.yaml.upstream` can't erase a pending span); applied ids join the shared `applied` set.
- An installed pack with **no recorded/parseable version** (a pre-versioning deploy) is
  baselined at the incoming version and nothing runs — apply older CHANGELOG steps manually
  once; the span machinery takes over from there.

## Authoring notes

- Keep `apply()` **idempotent and side-effect-free in dry-run**. The framework calls it with
  `dry_run=True` is *not* done today (Phase 1 only runs real applies), but Phase 2 will — write
  it dry-run-safe now.
- A migration that raises on load fails the whole `upgrade` loudly (no silent skip).
- Don't delete a migration once shipped — packs may not have applied it yet.
