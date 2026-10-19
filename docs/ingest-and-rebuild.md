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

Feed full-text capture distinguishes discovery from acquisition: after an initial
capture failure, the first successful document capture emits a separate raw revision
instead of being suppressed as an already-seen feed item. Existing capture history
is matched by the requested article URL within its feed as well as the exact GUID,
so rotating publisher GUIDs do not repeatedly emit unchanged articles or hide real
content revisions. Historical capture state lacking a requested URL retains exact-GUID
matching. Existing compiled excerpt pages still require the supported hydration repair;
this producer fix does not rewrite them or claim that the backlog is repaired.

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

## Discovery is not captured evidence

A connector search hit and a feed summary are discovery leads. `raw-compile`
records `content_state: discovery` for those inputs; it records `captured` only
for a raw landing with capture provenance and a nonempty `## Captured content`
section. A `capture_dead_letter` yields `capture-failed`; unrecognized inputs
remain `unknown`. These states describe acquisition, never appraisal, source
reliability or independent verification.

Inquiry analysis derives effective state from each source's raw references,
including legacy pages without the new field. A writer-supplied `content_state`
cannot promote a snippet. Captured text must actually be present in the source
body: merely attaching a new full capture to an old summary does not hydrate
that page. Missing, malformed, oversized or out-of-root raw references remain
unverified. Discovery, failed and unknown sources stay in the collection
inventory but do not count toward the analysis evidence floor or appear as
article evidence in its digest. Exclusions are collection gaps, not qualified
evidence of absence.

Captured text still requires appraisal: a downloaded page may contain
boilerplate or make an unsupported claim. Neither length nor successful
extraction certifies quality. A short complete notice can be captured evidence;
a long search snippet remains discovery.

`sources hydrate --missing-body` now inspects existing source pages rather than
re-fetching configured feeds. It reports the proposed captures without network
requests or writes by default. Use `--source sources/...md` (repeatable) to choose
pages, `--limit N` to bound capture attempts (default 20), and `--apply` to capture
and repair. Without `--source`, it inspects all source pages; the run limit is a
batch bound, not a permanent corpus scope. Already captured pages do not spend
that budget. Invalid and unknown inputs are reported, never silently discarded.

The operation uses `web_capture`'s bounded HTML/plain-text fetch and extraction.
PDF and other unsupported formats remain explicit failures. Request, final and
canonical URLs, retrieval time, response hash and extracted-text identity live in
immutable raw observations beside the captured object and revision. A failed
capture gets a dead letter and a `capture-failed` observation; it never falls back
to calling the original snippet captured content.

Repairs append a labeled full-text acquisition section through the enforced
raw-compile writer contract. Source identity, URL, existing text and raw
references are retained. An optimistic SHA check defers a concurrent edit;
rejected writes remain visible. A successful repeat skips the captured page.
Capture does not grant appraisal or reviewed status, and extraction can still
contain boilerplate: review must assess the document before making claims.

`framework sources hydrate --missing-body --apply` executes the staged repair
inside the deployment's running `gateway` service, using its configured writer,
actor contract and vault. Preview runs read the host deployment without invoking
Docker. A host checkout does not substitute for a deployed writer; deploy the
matching scripts before applying repairs. The equivalent gateway command is:

```bash
python /opt/data/scripts/source_hydrate.py --source sources/<existing-page>.md --limit 1 --apply
```

This provides an operator-triggered repair path; it does not yet schedule corpus
backfill or certify that the historical backlog has been hydrated and appraised.
Those completion requirements remain tracked in #923 and the coverage tracker.
