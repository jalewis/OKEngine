# Mutation testing

OKEngine uses Cosmic Ray to measure whether tests distinguish meaningful
changes in critical production behavior. Statement and branch coverage prove
that code ran; this gate proves that assertions reject changed behavior.

## Reviewed scope

[`targets.json`](targets.json) is the explicit production boundary. It maps
each selected module to its owning category, criticality, per-mutant timeout,
whole-session deadline, and focused test command. The critical floor is anchored by:

- model routing;
- completion receipts;
- deployment guards;
- composed schema behavior and the enforced schema validator;
- policy evaluation; and
- write authorization scopes.

The reviewed noncritical boundary also maps every currently changed production module, including
write orchestration, release evidence/resource enforcement, deployment and co-install checks,
backfill selectors, canonical assembly, extension composition, backup, install, upgrade, and
validation. These targets must each meet the 80% floor and cannot borrow kills from stronger
modules.

Changed Python production code beneath `scripts/`, `tools/`, `okengine-mcp/`,
or `okengine-reader/` must have a manifest entry. The changed-file gate fails
when a changed module is not mapped; it never treats an empty selection as
proof.

## Runner decision

Mutmut was evaluated first. Its import-hook mutant module used the package name
`scripts.model_profiles`, while the deployed cron tests intentionally import
the same file as top-level `model_profiles`. Tests therefore exercised the
original module and produced false zero-sensitivity results. Cosmic Ray mutates
the reviewed source file in an isolated detached Git worktree, so the deployed
import shape and the test import shape resolve to the same bytes.

The active developer checkout is never mutated. Missing binaries, zero
mutants, malformed or absent results, per-mutant timeouts, session timeouts,
incompetent mutations, abnormal outcomes, and runner errors are hard failures.

## Commands

```sh
make test-mutation-changed MUTATION_BASE=origin/main
make test-mutation-full
```

Merge requests automatically validate target registration; their changed-target
campaign is manual. The daily schedule selects critical targets and the weekly
opt-in schedule selects the full manifest. Campaigns carry an 11.5-hour internal
budget below the twelve-hour runner timeout.

Before creating any mutant, the critical preflight job groups targets by effective test command,
timeout, and exit-first semantics and runs each distinct baseline once in a fresh worktree (with
one fresh-worktree retry). It publishes a proof bound to the manifest digest and complete critical
target set. Every shard requires that proof. A failed or stale proof stops the campaign before
mutation; a shared baseline is never repeated once per module or shard.

`runtime_costs.worker_seconds` records measurement-backed work estimates separately from each
target's pathological-execution kill deadline. Shards use longest-processing-time balancing over
those costs. Each shard's estimated wall time at its configured worker count must fit inside 80%
of the campaign budget. A manifest that cannot fit is rejected before mutation execution and must
be reduced, re-measured, or split into CPU-bounded shards. The four daily shards use one worker
each, so their aggregate worker count does not exceed the verified four-CPU allocation.

## Live progress and shard diagnosis

The runner writes flushed JSON records to stderr, each prefixed with
`mutation-progress `. private CI therefore exposes progress before artifacts are
available. The lifecycle is:

- `shard_start`: manifest digest, shard index/count, selected target count,
  campaign deadline, and worker count;
- `baseline_preflight_start` / `baseline_preflight_complete`: one transition
  per distinct effective test suite, including the number of targets sharing it;
- `target_start`: target index/count, path, criticality, and target deadline;
- `target_heartbeat`: target path and elapsed seconds, at least once every 60
  seconds while the target is active;
- `target_complete`: measured/error outcome, mutant totals, score, target
  elapsed time, and completed/selected counts; and
- `campaign_terminated`: measured, errored, and unreached target counts when
  the runner receives `SIGTERM`.

The `manifest_sha256` and shard coordinates in `shard_start` correspond to
`manifest_sha256` and `shard` in that shard's eventual `summary.json`.
`target_complete.path` corresponds to an entry in `summary.json.targets`, and
its `completed_target_count` identifies how many target entries the partial
summary must contain. A running target with no `target_heartbeat` for more than
120 seconds is an observability failure and should be investigated before the
campaign is allowed to consume its full deadline.

## Scoring and survivor triage

The aggregate floor is 80%. Every critical target and the aggregate critical
scope must score at least 90%. A target cannot borrow kills from another target
to cross its floor.

Every survivor must materialize an owner and disposition in
[`survivors.json`](survivors.json). Reachable survivors remain in the
denominator. A survivor may be removed from the denominator only when it is
proved equivalent and records all of:

- `owner`;
- `approved_by`; and
- a concrete `rationale` explaining why no reachable input can distinguish it.

Pattern dispositions are reserved for mechanically identical classes, such as
operators acting only on postponed annotations. Broad patterns for reachable
code use `assertion-gap-counted`, remain in the score, and are candidates for
the next ratchet.

## Evidence

Each lane publishes:

- `artifacts/mutation/summary.json`, including every target, score,
  infrastructure failure, survivor fingerprint, owner, and disposition;
- `artifacts/mutation/junit.xml`, for the CI test report; and
- per-target Cosmic Ray configuration, session database, and JSONL result dump.

Merge-request artifacts are retained for 30 days. Scheduled full-scope
artifacts are retained for 90 days.
