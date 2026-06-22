# Sharded-scan discipline (`rglob`, not `glob`)

**Rule:** a scanner that means *"every page in this namespace"* MUST use
`Path.rglob("*.md")`, never `Path.glob("*.md")`, on a namespace root.

## Why

Engine namespaces are **hierarchically sharded on demand**, driven by a pack's
`schema.yaml` `partitioning` config — there is no fixed layout:

- `sources/<year>/<month>/…` (by-date)
- `entities/<type>/<letter>/…` (by type + first letter)
- any namespace a pack declares `reshard_by` / `sharded_types` for

`reshard_oversized.py` moves pages into shard dirs once a directory crosses the
threshold, so a namespace that is flat today becomes nested tomorrow **without
any code change**. A non-recursive `glob("*.md")` on the namespace root then sees
only the handful of files still sitting at the top level — in a fully sharded
corpus that is roughly **1%** of the pages. The scan silently succeeds and
silently undercounts.

This is not hypothetical: in the origin deployment the OKF sharding migration
broke **13 cron scripts + the daily digest** this exact way — each had a bare
`glob` that quietly stopped seeing the corpus. None errored; they just went
blind.

Because sharding is **pack-decided**, engine code cannot assume a namespace is
flat. The only correct default for a whole-namespace scan is `rglob`.

## The enforced convention

Every bare `.glob(` in the engine source tree (`scripts/`, `tools/`,
`okengine-mcp/`, `okengine-reader/`) must be either:

1. **`rglob`** — the default for any whole-namespace content scan, **or**
2. **waived** with an inline `# glob-ok: <reason>` comment at the call site.

`tests/cron/test_sharded_scan.py` fails CI on any bare `.glob(` that lacks a
waiver, and on any waiver that omits a reason. The waiver makes each exception
explicit and reviewed instead of an accident waiting for the next reshard.

### When a bare `glob` is legitimately correct (waive it)

- **Per-directory walker that recurses itself** — e.g. `build_index_tree.py`
  writes one `INDEX.md` per directory and recurses into subdirs, so each call
  intentionally sees only its immediate children.
- **Already a shard-leaf dir** — the path being globbed is itself a resolved
  leaf (`reshard_oversized.py` globs `namespace/<suffix>`, then globs `*.md`
  inside each leaf), or an explicit list of leaf dirs (`build_hot_set.py`'s
  recent-month dirs).
- **Flat, non-content dir** — a pack's `feeds/` or `scripts/` directory is flat
  by definition and never sharded.
- **Namespace-dir discovery** — globbing `*/<namespace>` to find per-pack
  namespace roots (not page content); follow it with `rglob` over each root.
- **Migration "flat only" pass** — `okf_migrate.py` deliberately scans only the
  unsharded top level (already-nested pages are done).

### The audit rule

When you hit a `glob`, **resolve its target dir**:

- Does the call mean "all pages in this namespace"? → `rglob`.
- Is the target genuinely a single leaf / flat / non-content dir, or does the
  function recurse on its own? → bare `glob` + `# glob-ok: <reason>`.

Leave recursive walkers and shard-aware scanners alone — don't blind-sweep a
working per-directory recursor into a single `rglob` that breaks its pagination.

### Emitting links from a sharded scan

When a scan that now `rglob`s also emits wikilinks, build the link from the
**vault-relative path**, not `"<namespace>/<slug>"`:

```python
rel = p.relative_to(VAULT / "wiki").with_suffix("").as_posix()
out.append(f"- [[{rel}]]")
```

For a flat namespace this is byte-identical to the old `"<namespace>/<slug>"`
form; for a sharded namespace it is the only form that resolves
(`[[entities/weapon/m/m777]]`, not the now-nonexistent `[[entities/m777]]`).
