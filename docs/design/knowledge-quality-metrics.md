# Knowledge quality and disposition contract

Machine health is not product quality. A completed judgment lane must account for every selected
item with exactly one durable disposition: `accepted`, `merged`, `updated`,
`rejected-out-of-scope`, `insufficient-evidence`, `duplicate`, `deferred-for-review`, or `failed`.
The selection manifest supplies the denominator; a missing item is an incomplete receipt, never a
silent no-op. Historical `skipped`, `rejected`, and `deferred` values remain readable as migration
aliases, but new producers must emit the canonical values.

`knowledge_quality.py` rolls up stored receipts into the fleet-health sidecar and dashboard.
Every measure has `state: measured` with a value (including a real zero), or `state: unknown` with
a reason. Consumers must not coerce unknown to zero. Release evidence stores the same snapshot so
changes can be compared longitudinally by release SHA rather than by a mutable dashboard.

## Measures

The product scorecard contains evidence-to-knowledge latency, disposition completeness and
correctness, unsupported-claim rate, canonical duplicate rate, contradiction age, review precision
and age, stale-knowledge rate, cost per accepted update, and retrieval usefulness. Receipt-derived
completeness and duplicate rate are automatic. The remaining measures require a versioned
adjudication snapshot under `/opt/data/quality/adjudication.json`.

## Sampling and adjudication

For each release, draw a deterministic stratified sample keyed by release SHA. Stratify by pack,
lane, disposition, content type, and risk tier; include every rare or failed disposition and a
random sample of common successes. Record the population, seed, inclusion probability, sample
size, and query/task set in the snapshot.

Two reviewers independently score claim support, correct disposition, contradiction state,
freshness, and retrieval usefulness against the cited source material. They are blind to model and
release identity. Disagreements go to a third reviewer; retain both original judgments and the
adjudication. Report sample size, confidence interval, reviewer identities or stable pseudonyms,
rubric version, and inter-rater agreement beside each metric. Cost uses metered run cost divided by
accepted + merged + updated items; latency uses source-observed to canonical-write timestamps.

Rollback is data-only: stop publishing the adjudication snapshot and fleet health returns those
measures to `unknown`; receipt-derived measures remain available. Never copy a prior snapshot
forward to make a missing measurement look current.
