# Explainability and analytical exports

Explain commands report the information behind a rendered value without changing the wiki:

```bash
bin/framework explain page <deployment> entities/example --json
bin/framework explain assessment <deployment> assessments/example --json
bin/framework explain field <deployment> entities/example origin --json
bin/framework explain config <deployment> models.default --json
```

Results include schema ownership, producer metadata, source/evidence lineage, quality flags,
review reason, policy outcome, and matching operation receipts when those facts exist. Missing
facts remain absent rather than being inferred. Sensitive configuration keys are always redacted.

Create a content-addressed evidence snapshot for an actor or assessment run:

```bash
bin/framework snapshot evidence <deployment> --actor entities/example
bin/framework snapshot evidence <deployment> --assessment <run-id>
```

The snapshot copies the selected page and directly linked source/assessment evidence beneath
`.okengine/snapshots/<digest>/`, alongside a hash manifest.

Create a bounded analytical export:

```bash
bin/framework export <deployment> --scope page:entities/example --format json
bin/framework export <deployment> --scope namespace:assessments --format md
bin/framework export <deployment> --scope namespace:sources --format bundle
```

This is deliberately not `framework backup`. An export contains only explicitly selected canonical
wiki pages and a manifest. It excludes runtime state, secrets, scheduler state, container data, and
recovery metadata; it cannot be restored as a deployment.
