# Entity & namespace partitioning — the canonical contract

How OKF pages are laid out on disk, how to **write** them, and how to **reference** them. Read this
before writing any pack script, extension lane, or importer that creates or edits `entities/` (or any
namespaced) pages. Getting it wrong produces **duplicate canonicals** that silently degrade the vault
(see [okengine#165](../) for a real case).

## The canonical layout (authoritative: `config/base-schema.yaml`)

`partitioning.namespaces` in the engine base-schema is the single source of truth; packs inherit it
and may only tune values, not invent a different scheme:

| namespace | strategy | on-disk path | reshard |
|---|---|---|---|
| `entities` | **by-letter** | `entities/{slug[0]}/{slug}.md` | `reshard_over: 500`, then by second letter |
| `concepts` | **by-letter** | `concepts/{slug[0]}/{slug}.md` | by second letter |
| `sources` | **by-date** | `sources/{YYYY}/{MM}/{slug}.md` | by day |
| `predictions` / `findings` / `briefings` / `trends` | **flat** | `{ns}/{slug}.md` | — |

`type:` is a **frontmatter field, never a path segment.** A CrowdStrike vendor lives at
`entities/c/crowdstrike.md` with `type: vendor` inside — **not** `entities/vendor/crowdstrike.md`.

## The WRITE contract

- **Write entity canonicals to `entities/{slug[0]}/{slug}.md`.** Let the enforced MCP write path
  handle it — `write_server._normalize_entity_shard` collapses a mis-picked *single-char* shard
  (`entities/c/v/foo` → `entities/c/foo`) to the canonical. But it **leaves multi-char segments
  alone**: `entities/vendor/foo` is NOT normalized — it becomes a **non-canonical duplicate** that
  the assembler never updates (okengine#48).
- **Never write `entities/{type}/{slug}`, a top-level `{type}/{slug}`, or a bare `{slug}` at the wiki
  root.** All three fork the namespace and spawn orphan/duplicate canonicals.
- **When a deterministic (no_agent) script edits an EXISTING page, emit its real `rglob`'d path** —
  do not reconstruct `entities/{slug[0]}/{slug}`, because a vault mid-migration may still hold the
  page under a legacy path. Find the file, edit it in place.
- Deterministic transform lanes write directly via `path.write_text` (the enforced-MCP rule governs
  *agent* writes); agent lanes write via `create_entity` / `update_entity` / `patch_entity`.

## The REFERENCE contract

- **Reference other entities by BARE SLUG**, not by path and not in `[[ ]]` brackets inside
  frontmatter ref-fields. The resolver (`comp_lib._entity_file`, the reader, the id index) matches on
  the **final slug** via `rglob` — so `crowdstrike` resolves wherever the page lives, while
  `entities/vendor/c/crowdstrike` is a fragile guess and `[[crowdstrike]]` breaks a `split("/")[-1]`
  resolver (it keeps the trailing `]]`).
- In **body prose**, wikilinks are `[[crowdstrike]]` or `[[concepts/<slug>]]` (the body resolver
  strips brackets). In **frontmatter ref-fields** (e.g. `direct_competitor_to`), use the bare slug.
- Bare slugs also bias resolution toward the by-letter canonical (the resolver checks
  `entities/{slug[0]}/{slug}.md` before falling back to `rglob`), which is what you want.

## The failure mode to avoid

Mixing layouts → **duplicate canonicals**. If the imported corpus is under type-dirs
(`entities/vendor/…`) but new writes land at the canonical `entities/{L}/…`, every re-touch of an
imported entity spawns a second page — and bare-slug refs to those slugs become ambiguous. This is
exactly what happened to a production deployment (80% type-dir, ~70 duplicate slugs); the reconciliation
is tracked in okengine#165. **`scripts/cron/okf_migrate.py` currently still targets the legacy
`entities/{type}/{L}/{slug}` layout — do not model new work on it; it is the stale outlier #165
fixes.**

## Checklist for a new pack / extension / lane

- [ ] Declare partitioning only by tuning the inherited base-schema values — don't invent a scheme.
- [ ] Write entity canonicals to `entities/{slug[0]}/{slug}` (or let the write path normalize).
- [ ] Editing existing pages? Emit the real `rglob`'d path, don't reconstruct it.
- [ ] Reference entities by bare slug; `[[slug]]` only in body prose.
- [ ] Never put `type:` in the path.
