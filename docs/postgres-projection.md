# PostgreSQL structured-query projection

OKEngine compiles a large Markdown vault into a standard PostgreSQL read model. It provides
structured analysis repeatedly walks and parses the corpus, or when a result must prove complete
coverage. Markdown remains canonical and all normal write paths are unchanged.

## Enable it

`deploy.sh` enables the projection by default and generates distinct random writer/reader secrets
in the deployment `.env`. Existing deployments receive an engine-managed Compose overlay under
the ignored `.hermes-data/` runtime tree; new deployments contain the services directly. To set
credentials manually:

```dotenv
OKENGINE_PROJECTION_WRITER_PASSWORD=replace-with-a-long-random-value
OKENGINE_PROJECTION_READER_PASSWORD=replace-with-a-different-long-random-value
OKENGINE_PROJECTION_READER_DSN=postgresql://okengine_reader:URL_ENCODED_READER_PASSWORD@postgres:5432/okengine
```

Then deploy normally; PostgreSQL and the projector start with the rest of the stack:

```bash
ENGINE_DIR=/path/to/okengine docker compose up -d --build
docker compose logs -f okengine-projection
```

The projector validates UTF-8 database initialization, applies the idempotent DDL, creates or
rotates the SELECT-only `okengine_reader` role, performs one rebuild immediately, and repeats each
hour. Tune its interval, maximum query age, and deletion guards with the corresponding
`OKENGINE_PROJECTION_*` environment variables documented in the Compose template.

The percentage and absolute removal limits must both be exceeded before a rebuild is refused. This
allows legitimate deletion in small vaults while stopping an unmounted or misdirected vault from
emptying a mature projection.

## Operate and verify

```bash
# One rebuild
docker compose run --rm okengine-projection --once

# Freshness, count parity, last-run state, and sampled digest parity
docker compose run --rm okengine-projection --health

# Scratch-schema rebuild and deterministic row/link comparison
docker compose run --rm okengine-projection --verify
```

Schedule `--health` hourly and `--verify` weekly in deployment monitoring. A nonzero exit is an
alert; neither operation silently repairs or suppresses drift.

## Query tools

The read MCP exposes `projection_status`, `count_pages`, `find_projected_pages`,
`get_projected_page_meta`, and `find_projected_links`.

Completeness-sensitive calls refuse to answer when no successful epoch exists or the latest epoch
is over age. Successful list responses contain matched and returned counts, truncation, filters,
searched object classes, epoch, and age.

Use qmd `search` for lexical candidate discovery and `get_page` for canonical prose. Use projection
tools for exact filters, joins, counts, and completeness-sensitive analysis. Page queries support
bounded `published_*` and `updated_*` date predicates plus allowlisted `path`, `updated_desc`, and
`published_desc` ordering, so recurring analysis lanes do not need a full vault scan to construct a
recent working set. Agents never receive arbitrary SQL.

## Disaster recovery and backup

The named `projection-data` volume is disposable and intentionally outside the vault backup. To
recover it, stop the projection services, resolve its exact named volume with `docker compose` or
`docker volume ls`, remove only that volume, and restart the services. Never target a broad Docker
or filesystem path. The weekly `--verify` run continuously proves this recovery path.

## Troubleshooting

- **`server_encoding is SQL_ASCII`**: replace only the disposable projection volume and let the
  supplied `POSTGRES_INITDB_ARGS` initialize it as UTF-8.
- **Typed tools say the projection is stale**: inspect projector logs and run `--health`. Stale data
  is withheld deliberately.
- **A rebuild refuses mass deletion**: verify the vault mount and namespace configuration before
  temporarily changing both guards for an intentional removal.
- **Many unresolved links**: inspect resolution distribution first. Exact, ID, alias, slug,
  ambiguous, and unresolved are deliberately distinct facts.
