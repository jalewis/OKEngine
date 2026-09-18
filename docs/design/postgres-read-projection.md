# PostgreSQL read projection

**Status:** accepted for implementation · **Parent:** okengine#566

## Decision

Markdown remains OKEngine's only canonical knowledge state. PostgreSQL is a standard,
disposable, query-optimized projection for structured reads. It may be dropped and rebuilt from
the vault; it never participates in canonical writes and is not backed up.

The projection exists for deterministic joins, temporal and frontmatter predicates, complete
counts, and bounded query latency. qmd remains the candidate-discovery/lexical-search surface and
page bodies continue to be read from Markdown.

## Engine and pack boundary

OKEngine owns the generic `pages`, `links`, and `projection_runs` schema, schema-driven corpus
selection, rebuild/recovery lifecycle, freshness and coverage contracts, read-only database role,
and generic typed query primitives. Packs own domain-specific promoted fields, views, query tools,
consumer migrations, and stricter freshness settings.

The engine normalizes only fields with framework-wide meaning. The complete pack frontmatter is
kept in `jsonb`; a field earns a column only after becoming an engine contract or a measured access
pattern.

## Synchronization model

Phase 1 is rebuild-only. Each run allocates an epoch, hashes every eligible Markdown file, touches
unchanged rows, reparses changed rows, and removes rows not seen in the new epoch. Content digests,
not mtimes, detect changes. There is no outbox, watcher, event store, trigger, or database-aware
write path. Those require evidence that scheduled rebuilds cannot meet the freshness objective.

Removal is guarded by both an absolute and percentage threshold. Tombstoned files are retained as
rows and excluded by default; deletion means that a canonical file no longer exists.

## Corpus selection and identity

Knowledge namespaces and exclusions come from the governing/composed schema, using the same rules
as other engine scanners. Reserved and derived pages are excluded centrally and reported in run
statistics.

`path` is the phase-1 primary key. Canonical `id` is indexed but not unique because existing vaults
may contain missing IDs or governed collisions. Links retain the authored target, resolved path,
section, and resolution method. Resolution is exact path, canonical ID, ID alias, or unique slug in
that order; ambiguous and unresolved targets are retained rather than guessed. Wikilinks in both
frontmatter and body are projected.

## Completeness and freshness

Every successful list query reports `matched`, `returned`, `truncated`, applied filters, searched
object classes, projection epoch, and projection age. `count_pages` is the base completeness
primitive.

A missing, failed, unavailable, or over-age projection is a tool failure, not an empty result and
not a `stale` annotation callers may ignore. The default rebuild cadence is hourly and the default
maximum query age is six hours; deployments may tighten both.

## Security and recovery

The projector and read MCP use different database roles. The MCP role has `CONNECT`, schema
`USAGE`, and `SELECT` only. Recovery drops and recreates derived state only, restores grants, runs a
projection, and verifies that the read role still cannot write. PostgreSQL data is excluded from
vault backups because scratch-schema reproducibility continuously proves rebuildability.

## Required verification

- PostgreSQL 17 runs in CI; mocks cannot establish database semantics.
- `SHOW server_encoding` must be `UTF8`; initdb locale flags are supplied through
  `POSTGRES_INITDB_ARGS`.
- Two independent builds from one vault state produce identical, non-empty semantic checksums.
- Verification fails if no rows are comparable.
- Every health alarm has a fault-injection test proving it can fire.
- Filesystem and projection counts agree for declared conformance canaries.
- A migrated deterministic consumer must produce equivalent output with measured lower latency
  before broader migrations begin.

## Explicit non-goals

- PostgreSQL is not a canonical write API.
- No arbitrary SQL is exposed to agents.
- No OpenSearch, Neo4j, embeddings, or semantic-search replacement is introduced.
- Existing file-scanning consumers are not silently switched; migrations are measured separately.
