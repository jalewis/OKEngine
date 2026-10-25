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
make test-mutation-full-preflight   # plan one weekly rotation run
make test-mutation-full             # one shard of it (MUTATION_REUSE_PLAN, MUTATION_BASELINE_PROOF)
```

Merge requests automatically validate target registration; their changed-target
campaign is manual. The daily schedule selects critical targets and the weekly
opt-in schedule rotates through the full manifest (see "The weekly full lane is a
rotation" below). Campaigns carry an 11.5-hour internal budget below the
twelve-hour runner timeout.

Before creating any mutant, the critical preflight job groups targets by effective test command,
timeout, and exit-first semantics and runs each distinct baseline once in a fresh worktree (with
one fresh-worktree retry). It publishes a proof bound to the manifest digest and complete critical
target set. Every shard requires that proof. A failed or stale proof stops the campaign before
mutation; a shared baseline is never repeated once per module or shard.

`runtime_costs.worker_seconds` records measurement-backed work estimates separately from each
target's pathological-execution kill deadline. Shards use longest-processing-time balancing over
those costs. Each shard's estimated wall time at its configured worker count must fit inside 80%
of the campaign budget. A manifest that cannot fit is rejected before mutation execution and must
be reduced, re-measured, or split into CPU-bounded shards. A changed-mode target that a fresh
coverage map selects is charged its selection cost instead: covering-test time per in-scope
mutant, the selection baseline, and a bounded full-command confirmation of assumed survivors
(`selected_cost_seconds` in `ci/mutation_gate.py`, okengine#941). A missing or stale map keeps
the whole-file cost, and a refusal names which estimate its targets used. Whole-file (full-mode)
campaigns instead read `runtime_costs.selected_worker_seconds`, a measured selected cost per
critical target (okengine#940, basis in `selected_basis`): a target runs selected only when that
measurement is below its full-command cost, and is charged it; otherwise it runs its full
`test_command`. The formula over-charged whole files several times over and would have refused
every nightly critical shard. The four daily shards
use one worker each, so their aggregate worker count does not exceed the verified four-CPU allocation.

## Coverage-guided test selection

A target's `test_command` is a hand-written list, and it drifts both ways: too narrow produces a
false survivor (the `transport.py` target never ran the test that kills its `or`->`and` mutant,
!1145), too broad produces a baseline timeout (!1171, okengine#895). Every campaign therefore
selects tests from coverage: changed mode since okengine#900, the nightly critical and the full
campaigns since okengine#940:

- **The map.** The sharded `full-suite` job records per-test coverage contexts
  (`--cov-context=test`, subprocesses included), and `full-suite-combine` publishes
  `artifacts/mutation-coverage-map.json.gz` (`ci/coverage_map.py build`). No second test run.
- **Selection.** `mutation-changed`, `mutation-critical-preflight` and `mutation-full` download
  that map from their own pipeline's `full-suite-combine` (`MUTATION_COVERAGE_MAP`); the critical
  shards read the copy the preflight relays. Critical and full mode mutate whole files, so every
  mapped line of a target is selected. Each mutant runs
  only the tests whose context covers its line, fewest-files-touched first, under `-x`
  (`ci/mutation_selection.py`, with `ci/pytest_plugins/okengine_mutation_select.py`). A line's
  selection is capped at half the per-mutant timeout of measured test time.
- **Confirmation.** Every mutant that survives selection is re-run against the target's full
  `test_command`, after that command's own baseline passes in a fresh checkout. Selection only
  makes kills cheaper; it never reports a survivor the full command would kill.
- **Fail closed.** A missing, unreadable or stale map (target or test files changed since the
  map's commit) runs the full `test_command` and says so in `summary.json` (`selection`,
  `warnings`) and the job log. So does a selection baseline that cannot pass.
- **The nightly baseline proof.** With a usable map, `mutation-critical-preflight` proves the
  full-command baseline only for targets that will run on their full `test_command`, lists the
  selected ones under `baseline_deferred` in `baseline-proof.json`, and copies the map it planned
  from into its artifacts (`coverage_map_sha256`). A deferred target proves its selected-test
  baseline in its own campaign, and the full command's before any survivor confirmation, exactly
  as in changed mode. A shard fails if it would run a deferred target on its full command, or if
  its map is not the one the proof was planned from. The preflight and `mutation-full` run even
  when the full suite is red (okengine#783); without a map every baseline is proved up front.
- **Inputs.** Each target in `summary.json` records `inputs`: the git blob ids at HEAD of the
  target, of the test files that could decide its mutants, and of every `tests/**/conftest.py`,
  plus its `test_command` and map commit, and the evidence-reuse `key` below.
- **Drift.** `test_command_gaps` names test files that execute a target but its `test_command`
  omits; `kills_outside_test_command` names the tests outside the list that killed mutants.
  Correct the list from them.

## Evidence reuse (nightly critical lane)

The nightly critical campaign does not re-mutate a target whose inputs are unchanged since its
last passing measurement (okengine#943, `ci/mutation_reuse.py`):

- **Key.** A SHA-256 over the target's blob; the blobs of every test file its campaign can run
  (its `test_command`'s files and, when it runs selected, the map's covering test files); the
  `conftest.py` files pytest loads for them; the map's per-line covering test sets (selected
  mode); its raw manifest entry; its survivor-disposition entries and matching patterns; and the
  tooling: Cosmic Ray version and operator set, Python version, pytest's configuration
  (`pyproject.toml`'s `[tool.pytest]` table, `pytest.ini`, `setup.cfg`, `tox.ini`) and the gate's
  own files. Each fresh result records `inputs.key` and per-component digests, so a re-measure
  names what changed. Anything that cannot be hashed re-measures. Production modules the target
  imports are not in the key; the re-baseline window bounds that gap.
- **Evidence.** `mutation-critical-preflight` (`--reuse-evidence`) reads, through the private CI API
  with `CI_JOB_TOKEN` (or a `read_api` token in `MUTATION_EVIDENCE_TOKEN`), the shard summaries
  of scheduled `main` pipelines inside the window. A source counts only when its job finished
  (`success` or `failed`), its summary records the pipeline's commit, and that commit is an
  ancestor of HEAD. Per target, the most recent fresh measurement decides: it is reused only when
  it is complete, passed its own checks when measured (`verdict`), is inside the window, and has
  the same key. Any listing or artifact that cannot be read (expired, missing, refused,
  unparseable) re-measures every target, and the plan and log say why.
- **Plan.** The decision is `reuse-plan.json` in the preflight's artifacts. Every shard shards
  only the targets it re-measures (a shard with none passes at once), the budget pre-check prices
  only those, and `mutation-critical-aggregate` carries the reused results, marked `reused` with
  source pipeline, job and date in `summary.json`, the JUnit report and stderr, and re-scores them
  under the current floors, waivers and disposition checks exactly as fresh ones.
- **Re-baseline.** Evidence older than 7 days (`--rebaseline-days`, `MUTATION_REBASELINE_DAYS`)
  is refused. Each night also re-measures the oldest reusable evidence up to 1/7 of the critical
  set's measured cost, so re-baselining is staggered. The dropout watcher (`ci/mutation_history.py`)
  counts a carried score as measured only within its own 7-day bound.
- **Narrow commands (okengine#955).** A target that runs its full `test_command` is keyed on that
  command's files, so a shared list re-keys every target that shares it. Coverage does not narrow
  the write-service lists, because every `test_write_server*.py` file executes every service.
  Kills do. A narrowed command is the union of the test files that killed the target's mutants
  in a full-command campaign (the first failure under `-x`, so a sufficient set), plus any
  killers outside the list that selection found while the target ran selected. Prove it with the
  same local campaign on the old and the new command: the score must be equal or higher, with no
  survivor the old command killed. A target that runs selected is also keyed on the map's
  covering files. Narrowing its command changes its key only if the narrower command is measured
  cheaper than selection, so that it switches to full-command mode.

## The weekly full lane is a rotation (okengine#1071)

The whole manifest does not fit one weekly run, and never will by tuning: priced with the gate's
own estimates it is about 4.77M worker-seconds, while one run has 3 shards x 33,120 usable seconds
x 4 workers = 397,440. Pipeline 14253 had all three `mutation-full` shards refused at admission.
So the weekly lane measures part of the manifest each week, and **every target is measured at
least once per 42-day window (six weekly runs) or the lane fails**:

- **Plan.** `mutation-full-preflight` (`make test-mutation-full-preflight`: `--preflight-only
  --reuse-evidence --rotation-shards 3 --rotation-workers 4`) reads the same scheduled evidence
  as the nightly, over `MUTATION_FULL_REBASELINE_DAYS` (42; the nightly keeps its 7). Nightly
  critical shards count as evidence for the critical targets. Unchanged, passing, in-window
  targets are reused exactly as in the nightly.
- **Admission.** Targets that need a re-measure are admitted in priority order: the latest
  measurement failed; inputs changed, incomplete, unhashable, or evidence unreadable; never
  measured in the window; evidence older than the window. Within a priority the oldest
  evidence goes first (none first), then the path. Leftover capacity re-measures the oldest
  reusable evidence. Each admitted target goes to the least-loaded shard (each shard holds at
  most `floor(0.8 x deadline) x workers` worker-seconds); what fits nowhere is **deferred** with
  its reason, including "exceeds one shard's capacity". The plan records the deferrals, each
  shard's list (`shards`, in admission order), and the cost charged per target (`costs`).
- **Self-calibrating cost.** A target is charged its own most recent complete measurement,
  `elapsed_seconds x` that shard's `budget.workers x 1.25`, even when its inputs changed since.
  Otherwise a formula-priced target (`selected_cost_seconds`) is charged formula x the median
  measured/formula ratio over this plan's targets that have both (clamped to [0.1, 1.0]; 1.0
  below five samples), and any other target its whole-file cost. `rotation.basis` and
  `rotation.calibration` in the plan say which applied.
- **Shards.** Shard N measures exactly `shards[N]`, in order, and its admission check charges the
  plan's `costs`, so the preflight and the shard cannot disagree. A shard refuses a plan packed
  for another shard count, worker count or deadline. At its deadline a shard stops starting
  targets; the ones it did not reach are a warning in the shard and a deferral in the aggregate,
  never a crash. The preflight's shard count is pinned to `mutation-full`'s `parallel:`.
- **Aggregate.** `mutation-full-aggregate` scores measured and reused targets exactly as before
  and lists every deferral (`deferred` in `summary.json`, a JUnit case each) with its reason and
  latest evidence. A deferral **fails the lane** when the target has no complete measurement in
  the window ("never measured", "stale"), or its latest complete measurement failed. A deferral
  whose last complete measurement passed inside the window (its inputs changed since) is only a
  warning: `mutation-changed` covers that diff at merge time. The report states the backlog
  (count, estimated worker-seconds) and the weekly runs needed to clear it at this capacity.
- **Staleness.** `mutation-full-staleness` runs in every nightly critical pipeline
  (`ci/mutation_staleness.py`) and **fails when the weekly lane stops**: no finished
  `mutation-full-aggregate` on a scheduled `main` pipeline, or the newest one started more than
  `MUTATION_FULL_MAX_AGE_DAYS` (8) days ago. An unreadable API fails it too: unknown is not fresh.

Locally, build a map from any `--cov-context=test` run with `ci/coverage_map.py build` and pass
`--coverage-map` (or `MUTATION_COVERAGE_MAP`) to `ci/mutation_gate.py`.

## Timeouts

Every target's `timeout_seconds` must be at least twice its measured full-command baseline: the
sum of its test files' CI wall times in `ci/test-durations.json`. `tests/ci/test_mutation_gate.py`
enforces it on every change, not only when a target's own file changes (the two #895 baselines
were broken for five days). Re-derive timeouts from re-recorded durations; never raise one by
hand. A baseline killed by its watchdog prints `OKENGINE_MUTATION_TIMEOUT after <n>s` on stdout,
the stream Cosmic Ray reports for a failed baseline.

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

A disposition key is a position (`module:line:column:operator:occurrence`), so
an edit above a recorded survivor moves the mutant off its key. The diff-scoped
merge-request lane never re-scores those untouched lines; the nightly critical
campaign then fails on a reviewed survivor (okengine#1047). Every merge request
therefore runs `python ci/mutation_dispositions.py check`
(the `mutation-dispositions` CI job), which fails on any key or pattern
that names no current mutation site. Repair the drift in the same change with
`python ci/mutation_dispositions.py rekey --base <merge base>`: it carries each
stale entry through the module's unchanged lines and reports, rather than
guesses, an entry whose own line changed. Re-review such an entry, or delete it
if the reviewed code is gone.

## Evidence

Each lane publishes:

- `artifacts/mutation/summary.json`, including every target, score,
  infrastructure failure, survivor fingerprint, owner, and disposition;
- `artifacts/mutation/junit.xml`, for the CI test report; and
- per-target Cosmic Ray configuration, session database, and JSONL result dump.

Merge-request artifacts are retained for 30 days. Scheduled full-scope
artifacts are retained for 90 days.
