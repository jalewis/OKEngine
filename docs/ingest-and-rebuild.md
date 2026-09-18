# Ingest, sources, and derived rebuilds

The framework exposes connector ingestion without exposing connector implementation paths:

```bash
bin/framework ingest status <deployment> --json
bin/framework ingest run <deployment> [--connector NAME]
bin/framework ingest retry <deployment> --failed
bin/framework sources hydrate <deployment> --missing-body
bin/framework sources reconcile <deployment>
```

Connector IDs come from composed declarative manifests under `connectors/` or
`sources/connectors/`; duplicates fail closed. Connector state, health, and raw archives stay in
their declared runtime namespaces.

Derived artifacts can be regenerated independently:

```bash
bin/framework rebuild <deployment> --indexes
bin/framework rebuild <deployment> --dashboards
bin/framework rebuild <deployment> --backlinks
bin/framework rebuild <deployment> --projection
bin/framework rebuild <deployment> --all-derived
```

The rebuild registry is closed to arbitrary scripts and contains only the engine's index,
dashboard, backlink, and PostgreSQL projection generators. The command snapshots canonical page metadata before running
and fails if a registered generator changes canonical wiki content. Sources, entities,
assessments, and review decisions are never declared rebuild outputs.

`--projection` rebuilds only PostgreSQL. Because the projection is part of the standard deployment,
`--all-derived` includes it alongside indexes, dashboards, and backlinks.
