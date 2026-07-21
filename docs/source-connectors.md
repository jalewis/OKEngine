# Declarative source connectors

OKEngine source connectors let a pack acquire structured data without adding a
source-specific engine script. A connector is a YAML manifest under
`connectors/`; the engine validates and deploys the manifest, then the generic
`source_connector.py` runtime dispatches it.

The v1 contract is deliberately bounded: HTTP `GET`, JSON or JSON Lines,
declarative field paths, and no embedded code. A source needing a browser,
custom protocol, signature algorithm, or complex transformation belongs in a
reviewed pack script or isolated extension instead of weakening this contract.

## Operating modes

All modes produce the same provenance envelope. Their difference is lifecycle,
not record shape.

| Mode | Use | Default materialization expectation |
|---|---|---|
| `bundle` | Stable or periodically regenerated reference collections | Archive normalized revisions and, when permitted, raw responses |
| `query` | On-demand lookup of ephemeral or non-redistributable data | Return results; normally do not archive |
| `enrichment` | Keyed attributes for an entity that already exists | Return an additive result; normally do not archive |
| `stream` | A bounded batch read from a stream-like endpoint | Checkpoint each scheduled batch; this is not an unbounded socket process |
| `poll` | Incremental change collection | Persist cursor/validators and immutable record revisions |

`stream` and `poll` are intentionally finite per invocation. Scheduling remains
the responsibility of the pack's cron declaration, so rate and resource bounds
are visible to the operator.

## Pack layout and deployment

```text
pack/
  connectors/
    vendor-catalog.yaml
  crons/
    domain-crons.json
```

`framework validate <pack>` validates every `connectors/*.yaml` and `*.yml`.
Deployment stages valid manifests at `/opt/data/config/connectors/` and stages
the generic runtime at `/opt/data/scripts/source_connector.py`. Fixture files
are not deployed.

A no-agent cron can invoke a manifest like this:

```text
python /opt/data/scripts/source_connector.py \
  --manifest /opt/data/config/connectors/vendor-catalog.yaml \
  --state-root /opt/data/state/connectors \
  --archive-root /opt/vault/raw/connectors \
  --health-root /opt/vault/wiki/diagnostics/connectors \
  --summary-only --wake-on-new
```

Production-shaped, zero-seed examples live under
[`examples/source-connectors/`](../examples/source-connectors/): GitHub's
vendor-operated status incident API, the Federal Register document API, and SEC
EDGAR company submissions. They are fixture-tested but never activated or
scheduled automatically; copy and review the selected manifest in a pack first.

Query and enrichment inputs use repeatable `--param NAME=VALUE` arguments.
`--summary-only` keeps large archived payloads out of cron stdout;
`--wake-on-new` sets the cron-plus `wakeAgent` gate only when immutable revisions
were actually created. Both are opt-in, so interactive Query/Enrichment calls
still return their normalized items and never wake an agent by surprise.

## Manifest contract

The machine-readable schema is
[`config/source-connector.schema.yaml`](../config/source-connector.schema.yaml).
Every block below is required, even when disabled. Explicit disabled state is
preferable to an omitted field whose behavior an operator has to guess.

```yaml
connector_version: 1
id: example.catalog
name: Example catalog
mode: poll

trust:
  permission: authenticated       # public | authenticated | licensed | internal
  data_sensitivity: internal      # clear | internal | restricted
  source_authority: Example Inc.

permissions:
  network: true
  allowed_hosts: [api.example.com]
  allow_private_network: false
  write_raw: true

auth:
  type: bearer
  secret_refs:
    token: EXAMPLE_API_TOKEN       # environment variable name, never its value

inputs:
  required: []

request:
  url: https://api.example.com/v1/objects
  method: GET
  headers:
    Authorization: "Bearer ${secret.token}"
  query: {}
  timeout_seconds: 20
  max_bytes: 5242880

response:
  format: json
  records_path: data.objects
  stable_id_path: id
  revision_path: modified_at
  deleted_path: deleted

pagination:
  type: cursor
  max_pages: 10
  request_param: cursor
  response_path: data.next_cursor

checkpoint: {path: example.catalog.json}
conditional_requests: {enabled: true}
rate_limit: {max_requests: 10, per_seconds: 60}

archive:
  enabled: true
  raw_responses: true
  path: example.catalog
  retention_days: 30

license:
  name: Vendor terms 2026-01
  url: https://example.com/terms
  redistribution: restricted
  max_retention_days: 30

health: {path: example.catalog.json}
```

Supported templates are `${input.name}`, `${secret.name}`, and the runtime
values `${runtime.cursor}` / `${runtime.page}`. Dotted response paths traverse
objects and numeric list positions. When `revision_path` is omitted, the runtime
uses a deterministic hash of the complete source record.

Secret templates are restricted to request headers; the validator refuses them
in URLs and query parameters where access logs commonly expose them. Input and
runtime values interpolated into a URL path are percent-encoded as components.

The validator rejects undeclared hosts, URL credentials, inline sensitive
headers, parent/absolute runtime paths, unsupported methods, unbounded page or
response limits, and retention longer than the declared license allows. The
runtime repeats host and public-address checks after URL rendering and on every
redirect. Private addresses require both `allow_private_network: true` and
`trust.permission: internal`; this exception is explicit in each manifest.

## Output, state, and archival

Each normalized item contains:

- connector and mode;
- stable source-native ID and source revision;
- deletion marker and observation timestamp;
- source authority, sensitivity, and license;
- the unmodified source payload.

Record archives are append-only at
`<archive>/<manifest path>/records/<stable-id>/<revision>-<observation-hash>.json`.
Optional raw responses are preserved byte-for-byte and SHA-256 addressed.
Replaying the same fixture or upstream revision
does not create another artifact. Checkpoints and current health snapshots are
written atomically. Health records expose outcome, requests, records, new
revisions, deletions, cursor, and conditional-not-modified state without copying
the acquired records into the dashboard payload.

An upstream deletion is data: map its source field with `deleted_path`. The
normalized tombstone retains identity and revision so downstream policy can
retire or retain the corresponding knowledge without confusing disappearance
with collection failure.

## Dry runs and deterministic fixtures

Validate and inspect a request without resolving secrets, making a network
request, or writing state:

```text
python scripts/cron/source_connector.py --manifest connector.yaml \
  --param entity_id=123 --dry-run
```

A fixture is JSON with an ordered list of response pages:

```json
{
  "fixture_version": 1,
  "pages": [
    {"status": 200, "headers": {"ETag": "v1"},
     "body": {"objects": [{"id": "one", "revision": 1}]}}
  ]
}
```

Run it with `--fixture fixture.json --observed-at <fixed ISO timestamp>`. Fixture
mode makes no network request but exercises decoding, pagination, normalization,
checkpointing, archival, deletion, and health behavior. Packs should keep at
least one non-secret fixture beside their tests for every connector they ship.

The engine's five reference manifests and responses live under
`tests/fixtures/source_connectors/` and are executable examples for every mode.

## Identity-authority enrichment (okengine#314)

An `mode: enrichment` manifest may carry an `enrich:` block declaring how its records stamp
canonical **authority IDs** onto vault pages. The engine lane `authority_enrich.py` is the apply
layer — deterministic string arithmetic only, never LLM judgment:

```yaml
enrich:
  authority: ror                 # stamps authority_ids.ror
  id_path: id                    # dotted path to the canonical id in the record payload
  match:
    query_input: entity_name     # manifest input fed from the page field below
    page_field: name
    candidate_paths: [name, names.value]   # payload paths whose values must EXACTLY equal the page field
  targets:
    types: [lab, organization, publisher]  # eligible page types (optional: namespaces)
```

Rules the lane enforces: stamps are **additive** (`authority_ids.<a>` + an attributable
`authority_observations` entry; nothing else changes); an existing disagreeing ID is **never
overwritten**; ambiguous matches and the same authority ID on two pages set `needs_review` +
`conflicts` — duplicates need human convergence, never auto-merge. Coverage (eligible / stamped /
unmatched / ambiguous / duplicates) lands in `.okengine/connectors/authority/<authority>.json`.

A pack schedules it like any connector cron:

```json
{"name": "<pack>-ror-enrich", "no_agent": true, "schedule": {"kind": "cron", "expr": "40 4 * * *"},
 "script": "/opt/data/scripts/authority_enrich.py",
 "args": ["--manifest", "/opt/data/config/connectors/ror-organizations.yaml", "--limit", "25"]}
```

Reference: `examples/source-connectors/ror-organizations.yaml` (+ fixture) — the Research
Organization Registry, live-shape verified.
