# okengine — Testing Adoption Record

Adoption Record for the global testing standard (`standards/TESTING-STANDARD.md`,
maintained outside this repository) — §16. **A section of that standard is policy for this repo only if a row below says
so. Unmarked means not adopted** — absence from this table is never assumed
compliance.

This repo opts out of the SYSTEM_DOCS.md / ROUTE_REFERENCE.md protocol (it is an
engine/CLI repo, not a routes-and-database app), so the record lives here.

**Every statement below was verified against this repository on the date given,
not copied from another project's record.** Measured figures carry their date and
expire; re-measure before relying on one, and treat an undated figure as
unverified (§16).

- Verified: **2026-08-29**
- Verified against: `77ab4ea6b56b9a183329db91518218dfa482cd7c` (`main`)
- Named owner for all gates: `okengine-maintainers` (§3)

---

## Trust rules (§1)

All seven are in force. They are not adjustable per project. Where this repo has
a concrete mechanism, it is named.

| # | Rule | Mechanism here |
|---|---|---|
| 1 | Silence is never success | `staged_drift.py` reports `UNDETECTABLE` and exits non-zero on an empty listing; `fleet_health.qmd_search_health()` returns `unknown`, never `ok`; the scrub gate WARNs loudly when `.scrub-patterns` is absent |
| 2 | Harness failure is never a pass | `mutation_gate` records a target that could not run as an infrastructure error against that target and fails the gate (`unrunnable_target`) |
| 3 | Publish evidence before the verdict | `mutation_gate` collects per-target results in a loop, so one failure no longer discards completed targets; artifacts are written before the pass/fail branch |
| 4 | Evidence belongs to a revision | `artifacts/ci-runtime.json` records pip/pytest/python per job; `performance-environment.json` records git SHA and the **cgroup CPU quota** (not the host core count) |
| 5 | Required lanes fail closed | `coverage-floor` is blocking-manual on an MR (`allow_failure: false`) with `only_allow_merge_if_pipeline_succeeds` enabled, so an unstarted floor blocks the merge |
| 6 | Baselines move one direction | `fail_under = 100` / `branch_fail_under = 100` are pinned and tested; mutation floors live in `mutation/targets.json` |
| 7 | A gate with no named owner is not a gate | All 173 mutation targets carry `owner: okengine-maintainers`; **0 `TBD`** (verified 2026-08-29) |

---

## Section adoption

| Section | Status | Evidence / Issue |
|---|---|---|
| §1 Trust rules | **Adopted** | table above |
| §2 Quality objectives | **Adopted** | `private pipeline configuration` stages: `lint, gates, audit, e2e` |
| §3 Ownership | **Adopted** | `mutation/targets.json` — 173/173 targets owned, 0 `TBD` |
| §4 Risk classification | **Adopted with exception** | `critical: true/false` per mutation target with a stricter floor (90 vs 80). No broader risk-tier taxonomy beyond that binary |
| §5 Selecting a layer | **Adopted** | unit / conformance / integration / contract / e2e / smoke / resilience / performance lanes exist as separate CI jobs |
| §5.1 Property-based and fuzz testing | **Adopted with exception** | `hypothesis>=6.100,<7` in `requirements-dev.txt`; property testing is used where it fits. There is no dedicated lane and no fuzz target |
| §6 Mock fidelity and drift | **Adopted** | `tests/test_mock_fidelity.py` AST-scans every test module and rejects generated `Mock`/`MagicMock`/`AsyncMock`/`patch` doubles unless they are contract-bound with `autospec`, `spec`, or `spec_set`; concrete fakes and `patch.dict` are explicitly distinguished. Delivered by #585 and verified 2026-08-29 |
| §7 Test design rules | **Adopted** | one behavioural contract per test, descriptive names, failure messages — enforced by review, not tooling |
| §8 Coverage | **Adopted** | `pyproject.toml`: `branch = true`, `fail_under = 100`; `coverage-floor` job runs `--cov-fail-under=100` plus `scripts/check_branch_coverage.py --line-min 100 --per-file` |
| §8.1 Two-number design | **Adopted with exception** | Target and enforced ratchet are **both 100** (`fail_under` / `branch_fail_under`). They coincide because the ratchet has reached its destination, so no separate ratchet variable exists. If coverage ever has to be lowered, the two must be split rather than the target reduced |
| §9 Mutation testing | **Adopted** | `mutation/targets.json` (173 targets), floors `overall: 80` / `critical: 90`; jobs `mutation-targets` (every MR), `mutation-changed` (automatic on main), `mutation-full` (scheduled) |
| §9.1 Enforcement model | **Adopted** | `mutation-targets` asserts every changed production module is registered and runs no campaign (measured 0.09s) |
| §9.2 Scope (decision logic, not reference data) | **Adopted** | manifest lists modules explicitly; reference data is not a target |
| §9.3 Anti-gaming floor | **Adopted** | Every target declares `min_mutants`; full campaigns fail below it, while diff-scoped campaigns may legitimately select no mutable changed lines. Manifest validation also rejects absent target paths. |
| §9.4 Trusted report requirements | **Adopted with exception** | `summary.json` + `junit.xml` are retained per run with per-target detail and survivor fingerprints. Provenance is not cryptographically attested |
| §9.5 Harness hazards | **Adopted** | infrastructure outcomes and timeouts are classified as infrastructure errors, never as kills (`INFRA_OUTCOMES`, `TIMEOUT_SENTINEL`) |
| §9.6 Sharding and execution | **Adopted with exception** | per-target `timeout_seconds` / `session_deadline_seconds`; `MUTATION_JOBS: "4"` pinned against the measured per-container 4-CPU cgroup quota (`cpu.max 400000 100000`), which the docker executor applies to each shard container independently — it is not a pipeline-wide budget to divide between shards. The critical and full lanes each run 3 shards of 4 workers, matched to the dedicated mutation runner's `limit = 3`, against the fleet ceiling of `concurrent = 7` × `cpus = 4` = 28 cores. Targets run sequentially within a shard. Scheduled critical campaigns require `RUN_CRITICAL_MUTATION=1`; generic schedules execute no mutants. Mutation jobs are interruptible stale-revision evidence, so a newer pipeline on the same ref releases their runner slots rather than overlapping them. |
| §9.7 Result reuse | **Not adopted** | no caching or reuse of prior mutation results between runs; every campaign recomputes. Deliberate — reuse needs provenance guarantees this repo does not have |
| §9.8 Survivors | **Adopted** | survivors are retained with fingerprints and require an owner + disposition (`compliance_errors` fails on a survivor lacking either) |
| §10 Flake policy | **Adopted** | `docs/testing-flake-policy.md`; no automatic test/job retries; failures remain red and are classified; bounded prerequisite acquisition is distinct; `config/test-quarantine.yaml` requires an owner, issue, evidence, and <=14-day expiry; `ci/flake_policy.py` validates it and publishes the standing count |
| §11 Observability validation | **Adopted** | `observability-validation` runs after `fleet-health` and `health-export` against each live deployment; it fails on absent/stale/empty signals and verifies fleet identities/counts, qmd search-vs-maintenance telemetry, deployment-validation visibility, and Prometheus heartbeat agreement |
| §12 Change-to-test mapping | **Adopted** | `mutation-targets` fails when a changed production module has no registered target — the mapping is enforced, not documented |
| §13 CI execution model | **Adopted** | `.code-gates` and `.pack-gates` select affected evidence; `.docs-change-gates` provides the narrow docs path; `.layer-gates` / `.release-gates` / `.campaign-gates` retain main, tag, and schedule evidence. `full-suite` is the single correctness+coverage execution; no separate `coverage-floor` population exists. See #788. |
| §14 Environments and data | **Adopted** | `tmp_path` fixtures throughout; disposable Postgres service for `postgres-projection-integration`; no test depends on pre-existing production-like data |
| §15 Evidence and release gates | **Adopted** | `docs/release-checklist.md` (145 lines), `scripts/post_deploy_verify.sh`, the invariant audit (checklist step 2b), `staged_drift.py` for deploy verification |
| §15.1 Retain per pipeline | **Adopted** | JUnit + coverage + mutation artifacts retained 30 days |
| §15.2 Release gates | **Adopted** | release checklist requires green CI, merged code, deployed runtime, live end-to-end evidence |
| §15.3 Post-deployment verification | **Adopted** | `post_deploy_verify.sh`; `deployment_validate` cron; the CLAUDE.md rule to verify by inspecting the target rather than an exit code |
| §16 Adoption Record | **Adopted** | this document |
| §17 Adaptation worksheet | **Adopted** | below |
| §18 Checklists | **Adopted with exception** | the pre-commit gate block in `CLAUDE.md` serves this purpose; the standard's checklists are not reproduced |

---

## Adaptation worksheet (§17)

| Item | Value |
|---|---|
| CI control plane and runner tags | private CI CI; a privileged dind-tagged group runner (`tags: [dind]`) plus a shared instance runner. Runner identities live in the private CI config, not here |
| Production packages measured | all of `scripts/`, `okengine-mcp/`, `okengine-cockpit/`, `okengine-reader/`, `tools/`, `extensions/`, `ci/`. Exact pipeline 7205 measured **45,551 statements and 17,170 branches with zero misses or partial branches** (2026-08-29) |
| Mutation shards and their targets | 173 targets in `mutation/targets.json`; four isolated workers per target |
| Minimum mutant count per shard | `min_mutants` on every manifest target, enforced in full campaigns |
| Risk-critical modules and their stricter gates | `critical: true` targets, floor 90 vs 80 |
| Coverage target / enforced ratchet | 100 / 100 |
| Changed-code coverage and mutation targets | changed-module registration enforced; no separate changed-code percentage |
| Service-level indicators and performance budgets | `performance-release`: p95 budgets and minimum rpm in `tests/performance/test_release_performance.py` |
| Required external contracts | pinned Hermes tag; cron-plus pinned SHA; `deployment_validate.check_write_path_libs` |
| Disposable dependency provisioning | Postgres service container; `tmp_path` vaults |
| Artifact retention and schedule cadence | 30 days; `mutation-full` and release lanes on schedule |
| Release verification and rollback thresholds | **Adopted** — `docs/release-checklist.md`: named release owner, immediate artifact/health/critical-path verification, one stable repeat, signal inventory, objective rollback thresholds, and post-rollback evidence; no default fixed observation window |
| Named owners per §3 | `okengine-maintainers` |

## CI change routing and duration budget

Documentation-only diffs run `diff-check`, `docs-validation`, `secret-scan`, and
`docs-duration-budget`. The exact classifier in `ci/change_scope.py` fails closed
when the diff base is missing, invalid, or empty. A mixed documentation and code
diff is classified as `mixed-or-code`; positive `rules:changes` selectors then
retain the applicable correctness, pack composition/conformance, projection,
mutation-registration, and behavioral security gates.

The authoritative Python 3.12 source suite emits JUnit plus line and branch
coverage in one private CI execution. `public-snapshot` qualifies the assembled
public tree in private CI; `public-compatibility` consumes that exact bundle on
Python 3.11 and 3.13. These are distinct interpreter contracts, not duplicate
reporting. GitHub hosts owner-approved releases and runs no second CI pipeline.
README qualification links describe release-scoped evidence, not live branch
status. Publication approval must be recorded separately from passing tests.

Baseline measured 2026-09-18: MR !1053 changed only `CHANGELOG.md`; private CI
pipeline #10393 ran for about 66 minutes and included broad test, pack, mutation,
projection, and security jobs. The initial docs-only wall-clock budget is 300
seconds from `CI_PIPELINE_CREATED_AT` to the final verdict. The verdict writes
`artifacts/docs-duration-budget.json` before passing or failing. The first
post-merge docs-only pipeline must be recorded here with its pipeline/job IDs
and measured duration; the budget may tighten from evidence but never increase
automatically. Tracking: #788.

The authoritative measurement uses a `TESTING.md`-only merge request. Its final
pipeline must execute only the four narrow jobs named above, identify the exact
candidate SHA in the scope and duration artifacts, and finish within the budget.
Optional manual qualification jobs may remain startable but consume no runner
time. The measured pipeline is recorded below rather than inferred from local
timing.

First post-merge measurement, 2026-09-18: MR !1063 pipeline #10462 at candidate
`2a1126ff259c100c9ba4fbf383e61b8da25d2c06` executed `diff-check` #120516,
`docs-validation` #120517, `secret-scan` #120523, and
`docs-duration-budget` #120528. Its exact-SHA duration artifact reported 241.683
seconds from pipeline creation to verdict, inside the 300-second budget. Broad
correctness, pack, projection, mutation, behavioral-security, and release jobs
did not execute. The final documentation commit repeats this path to verify the
result is stable.

---

## Known gaps

Recorded so they are visible rather than discovered. Each is tracked by an issue,
as §16 requires — "Not adopted" with neither an issue nor an explicit decision is
silence, and silence is not acceptable.

No unowned adoption gap is currently recorded. Deliberate exceptions and non-adoptions
remain explicit in the table above; new gaps require an owner and tracking issue before
they are added here.

## Stale figures found while writing this

§16 requires measured figures to carry a date and expire. Two in-repo figures are
undated and at least one is wrong:

- `private pipeline configuration` states *"full-suite was 13.6 min of an 18.5 min pipeline (74%
  of wall clock)"* — undated. `coverage-floor` measured **~17.5 min** on
  2026-08-12 (pipelines 4857, 4870). The margin argument built on that comment
  should be re-measured before it is relied on again.
- Per-target `timeout_seconds` comments in `mutation/targets.json` carry no
  measurement date. The cockpit target's 45 s was raised to 120 s on 2026-08-12
  after its baseline was killed under contention.
