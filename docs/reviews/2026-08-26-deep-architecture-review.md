# Deep architecture review — 2026-08-26

**Reviewed revision:** `main` at `6b5d96c6398542f8695e8403e5a9fe96c73d23d6`
**Scope:** product goals, runtime execution, deployment, write governance, scheduling,
retrieval/projection, testing, scaling, and maintainability.

## Executive assessment

OKEngine has a strong and differentiated core idea: compile evidence into durable,
governed knowledge rather than repeatedly synthesizing answers at query time. Its
architecture is unusually serious about provenance, schema enforcement, repair,
deployment verification, and operational failure modes.

The primary risk is no longer missing functionality. It is accumulated machinery:

- 68 baseline engine jobs before pack and extension jobs (52 deterministic, 16 agent-capable);
- 22 patches carried against Hermes;
- approximately 74,000 lines of production Python and 97,000 lines of tests;
- 153 `rglob`/`os.walk` scan sites across cron and serving code;
- 176 dynamic imports or `sys.path` manipulations;
- several application monoliths;
- independently copied deployment configurations.

The repository is evolving from an engine into an operating system for
agent-maintained knowledge. It now needs a consolidation phase.

## Product goal

OKEngine is best understood as a **governed compilation and maintenance plane for
evidence-backed knowledge products**.

A live deployment combines:

```text
pinned Hermes runtime
        +
domain-independent OKEngine
        +
one domain pack or composed bundle
        =
autonomous knowledge system
```

Hermes supplies the agent loop, model providers, tool execution, messaging, and base
scheduler. OKEngine supplies schemas, write governance, repair, search, health,
deployment, extensions, and read surfaces. A pack supplies sources, domain schema,
prompts, schedules, and content policy.

The durable product value is not a particular model or UI. It is the combination of
governed writes, canonical knowledge, provenance, deterministic maintenance, and
replaceable projections.

## How the system executes

### Deployment

`scripts/deploy.sh` validates engine and pack pins, composes schema and policy,
generates the cron fleet, seeds the Hermes runtime, builds images, starts services,
stages scripts and schedules, and runs live verification. The verifier exercises the
reader, MCP surfaces, PostgreSQL projection, write policy, scheduler heartbeat,
ownership, search index, and image provenance.

This is one of the repository's strongest subsystems: deployment success is defined
by inspecting the target, not by trusting orchestration output alone.

### Evidence ingestion and compilation

Connectors and feed jobs capture evidence below `raw/`. Deterministic extraction and
normalization prepare it. Wake-gates select bounded work, and an agent starts only
when judgment is required. The agent receives a pack persona, effective schema,
bounded evidence selection, output contract, and scoped toolset.

Agent writes go through `okengine-mcp/write_server.py`, which applies schema,
canonical-path, namespace-permission, field-preservation, tombstone, versioning,
review, and audit rules.

### Continuous maintenance

The baseline engine fleet currently contains 68 jobs. Deterministic `no_agent` jobs
handle indexing, reshelving, validation, projection, repair, dashboards, health,
review reconciliation, and deduplication. Packs and extensions add domain judgment
and analytical jobs; real deployments therefore run materially larger fleets.

### Consumption

Markdown remains canonical. The reader, cockpit, read MCP, PostgreSQL projection,
qmd/ripgrep search, generated indexes, dashboards, hot sets, and graph data are
replaceable consumption surfaces or derived state.

## Strengths to preserve

1. **Boundary enforcement over prompt convention.** Generic file mutation is blocked,
   write policy is enforced at MCP, tools are lane-scoped, and output contracts bound
   effects.
2. **Deterministic work stays deterministic.** Models are reserved for judgment;
   extraction, validation, indexing, and repair are primarily code.
3. **Operational honesty.** Runbooks record stale artifacts, orphaned processes,
   startup-only configuration, baked/staged drift, OOMs, and scheduler failure modes.
4. **Strong test culture.** The suite spans unit, integration, contract, security,
   invariant, mutation, resilience, performance, and deployed-stack verification.
5. **Explicit composition.** Pack and extension discovery, ownership, schema, policy,
   schedules, enablement, and collisions are modeled rather than implicit.

## Prioritized findings

### P0 — Gateway images are not isolated by deployment version

The pack skeleton refers to the shared `hermes-agent` image, while the build updates
`hermes-agent:latest`. A new build can therefore affect an unrelated deployment the
next time Compose recreates it. Two deployments on one host cannot safely remain on
different engine revisions even though packs declare independent engine pins.

Use immutable `hermes-agent:<engine-version>-<git-sha>` tags or digests in effective
Compose. Build once, verify the digest, and update each deployment explicitly. Never
use `latest` in a deployed service definition.

### P0 — There is no corpus-wide transaction protocol

The agent write path is carefully governed, but deterministic lanes legitimately
bypass the server and mutate Markdown directly. Atomic replacement protects a single
file, but concurrent scanners and multi-file writers can still observe mixed epochs.

Introduce a shared mutation library, scoped locks, a monotonic corpus epoch, and an
append-only mutation journal. Full audits should read a stable epoch or retry when the
epoch changes. Direct canonical writes outside the transaction layer should become an
invariant violation.

### P1 — The 100k-file scaling story is scan-heavy

There are 153 filesystem traversal sites across maintenance and serving code. The
included benchmark projected individual 100k-page audits at roughly 6–8 seconds,
which is acceptable in isolation; the concern is dozens of consumers repeatedly
parsing the same corpus.

Promote the PostgreSQL projection into an incremental metadata plane fed by the
mutation journal. Migrate selectors, audits, dashboards, and structured reads to it,
while retaining periodic filesystem scans for reconciliation.

### P1 — The Hermes relationship is operationally a soft fork

The engine carries 22 upstream patches across runtime-critical behavior. Regardless
of repository layout, that creates fork-like upgrade and compatibility obligations.

Create a patch budget and classify every patch as upstream candidate, temporary
compatibility change, or permanent product differentiation. Track retirement criteria
and stock-Hermes compatibility. Reconsider a formal adapter or maintained downstream
runtime if the patch set continues to grow.

### P1 — Core services are becoming monoliths

Notable files include `okengine-cockpit/app.py` (~5,664 lines),
`okengine-mcp/write_server.py` (~3,626), `okengine-reader/app.py` (~1,881),
`scripts/framework_validate.py` (~1,661), and `scripts/cron/corpus_audit.py`
(~1,498). Routing, storage, rendering, policy, and workflow logic are increasingly
intertwined.

Split applications into thin transport entrypoints plus repositories, domain
services, rendering, authorization, validation, review, and transaction modules.

### P1 — Shared Python code is not packaged

The codebase contains 176 dynamic import or `sys.path` manipulation sites. This
creates import-order sensitivity, duplicate module instances, weak type boundaries,
and host/container differences.

Create an installable `src/okengine/` package and versioned wheel. Keep cron files as
thin entrypoints and replace file-location imports with ordinary package imports.

### P1 — Performance evidence does not qualify a 100k deployment

The performance suite usefully exercises 100 concurrent users, real MCP calls, and
10,000-row algorithms, but its deployed stack uses a small smoke vault. It does not
measure reader, cockpit, MCP, projection, maintenance overlap, backup, and recovery
against a realistic 100k-page deployment.

Add a scheduled 100k qualification environment with latency, rebuild time, incremental
catch-up, disk growth, steady-state memory, overlapping writer/audit behavior, and
backup/restore criteria.

### P2 — Coverage and typing claims are narrower than the architecture

The 100% line-and-branch floor is valuable for measured files, but the configured
coverage source does not include `okengine-operations` or `okengine-projection`.
Static typing is deliberately limited to three files.

Include all production services in the coverage boundary, publish covered-versus-total
production files, expand typing by subsystem, and require new modules to be typed.

### P2 — Cron and prompt definitions are centralized and prose-heavy

Large prompts are embedded in the cron JSON, sometimes preserving legacy instructions
and later corrective instructions in the same prompt. This increases token cost and
the risk of contradictory behavior.

Move prompts to individual Markdown files, keep cron JSON structural, generate common
tool guidance from output contracts, and lint prompts for obsolete paths, contradictory
tools, repeated constraints, and unreachable instructions.

### P2 — The scheduler is a single-instance control plane

Each deployment depends on one gateway and one externally cloned cron-plus plugin.
Heartbeats make failure visible but do not remove the single point of failure.

Declare an availability target. For single-host operation, optimize durable leases,
external watchdogs, idempotent restart reconciliation, and recovery time. If higher
availability is required, separate scheduling into a durable service with fencing
rather than extending shared PID-file coordination.

### P2 — Deployment configuration drifts through copying

Pack Compose files and vendored validators age independently from the engine skeleton.
This has already produced missing services, inconsistent auth wiring, unapplied port
offsets, and different validation verdicts.

Compose deployments from a versioned engine base plus pack overrides, validate the
effective model against a schema, replace vendored validators with the engine-pinned
validator, and expose a `framework deployment diff` command.

### P2 — Workspace provenance is easy to get wrong

The initial review checkout was on a feature branch 138 commits behind `origin/main`
and contained generated `.okengine` artifacts. The source guard correctly prevents a
deployment build from that state, but review, audit, and local benchmarks can still
inspect stale code.

Show branch, SHA, distance from main, and dirty state in framework command headers.
Gate release/benchmark/audit commands on an attributable revision and keep generated
policy state outside the engine source tree.

## Product and measurement recommendations

### Sharpen the product boundary

Treat Hermes as a replaceable execution adapter and reader/cockpit/applications as
consumers. Keep OKEngine's core centered on schema, policy, transactions, provenance,
maintenance, and projections.

### Measure knowledge quality, not only machinery

Primary product measures should include:

- evidence-to-knowledge latency;
- disposition completeness and correctness;
- unsupported-claim rate;
- canonical duplicate rate;
- contradiction age;
- review precision and queue age;
- stale-knowledge rate;
- cost per accepted update;
- retrieval usefulness, not only latency.

### Make abstention a first-class result

Every judgment lane should finish each selected item with a durable disposition such
as `accepted`, `merged`, `updated`, `rejected-out-of-scope`,
`insufficient-evidence`, `duplicate`, `deferred-for-review`, or `failed`. Optimize
for correct disposition rather than artifact count.

## Recommended sequence

### Next 30 days

1. Replace shared/latest gateway image references with immutable release images.
2. Add operations and projection to the coverage boundary.
3. Externalize cron prompts and add contradiction lint.
4. Generate effective Compose from an engine base plus pack overrides.
5. Put source revision/provenance in every framework command.

### Next 60–90 days

1. Introduce the corpus mutation journal and transaction library.
2. Route selectors and audits through the incremental metadata projection.
3. Break up cockpit and write-server monoliths.
4. Establish the `src/okengine` package.
5. Add scheduled 100k-page qualification.

### Longer term

1. Decide whether Hermes remains an adapter or becomes a maintained downstream runtime.
2. Define scheduler availability requirements.
3. Consolidate the cron fleet around event-driven triggers and shared materialized facts.
4. Make knowledge quality and cost per disposition the primary fleet health signals.

## Tracking issues

Umbrella: #626 — Architecture consolidation

| Priority | Issue | Scope |
|---|---|---|
| P0 | #627 | Immutable gateway images per deployment |
| P0 | #628 | Corpus transactions and mutation journal |
| P1 | #629 | Incremental PostgreSQL metadata plane |
| P1 | #630 | Hermes patch governance |
| P1 | #631 | Core-service decomposition |
| P1 | #632 | Installable Python package |
| P1 | #633 | Production-like 100k qualification |
| P2 | #634 | Complete coverage and typing boundaries |
| P2 | #635 | External prompts and contradiction lint |
| P2 | #636 | Scheduler availability contract |
| P2 | #637 | Generated deployment Compose |
| P2 | #638 | Workspace provenance |
| Design | #639 | Primary product boundary |
| P1 | #640 | Knowledge-quality and disposition metrics |

### Open-issue audit — 2026-08-27

This replaces the stale point-in-time snapshot above without rewriting the historical
tracking table. The audit inspected every issue that GitLab reported open in OKEngine
and OKPacks, related merged MRs and issue corrections, current source/tests, the latest
main and scheduled pipelines, and the deployed `okcti-test` runtime.

The starting inventory was **75 open issues**: 71 OKEngine and four OKPacks. The audit
closed 19 OKEngine issues and one OKPacks issue as delivered, superseded, misattributed,
non-applicable, or a completed design decision. The two audit-time implementation
issues, #643 and #644, close with their MRs and do not add permanent backlog. The
resulting steady state is **55 open issues**: 52 OKEngine and three OKPacks.

#### Closed as OBE or already delivered

| Issues | Evidence-based disposition |
|---|---|
| #461–#466, #472–#474 | The coverage program reached and now enforces 100% statements and per-file branches; lifecycle, diagnostic, exclusion, and mutation controls exist. |
| #559, #560, #565 | Exact regressions merged and pass: non-vacuous canonical MCP-name scan, pinned date fixtures, and branch-triggered raw scan race. |
| #568 | Superseded by the PostgreSQL projection decision and completed qmd latency/saturation telemetry. |
| #583 | Measured as non-applicable: OKEngine has no auto-sized worker pool exposed to the host/core mismatch, and mutation workers are explicitly bounded. |
| #523, #502 | The 500-second pool contract, jittered retry, attribution, conversation affinity, and one-shot headers are implemented and deployed. |
| #538 | Its own follow-up proved the traffic belonged to a neighboring deployment, not an anonymous OKEngine auxiliary path. |
| #482 | Delivered as the canonical `docs/llamacpp-migration-findings.md`. |
| #550 | Completed design decision: retain read-only web surfaces and use the governed named-review CLI. |
| OKPacks #31 | Its three-pack baseline and prerequisite list are obsolete; the library now contains eleven packs and concrete product issues supersede it. |

#### P0 — stop other work

| Issue | Why it remains open | Next proof |
|---|---|---|
| #612 | Scheduled pipeline 6804 still failed `mutation-critical` on 2026-08-27; the enforced write path remains outside the campaign deadline. | A scheduled critical campaign measures `write_server.py`, publishes a non-null score, and passes on exact main SHA. |
| #539 | Current source still refuses minted-slug collisions and routes them into review/reconciliation; the dominant historical queue inflow is not resolved at the write boundary. | Collision returns/converges on the existing identity without a human row; live queue inflow falls accordingly. |

#### P1 — active correctness and reliability

| Issue | Current assessment |
|---|---|
| #611 | Active: main and scheduled pipelines still go red without a repository-owned notification path. Do immediately after #612. |
| #608 | Active: transport-loss recovery can permanently remove MCP tools while a lane continues. |
| #604 | Active and live-confirmed: post-deploy verification found five dead-worker operations still marked `running`. |
| #613 | Active policy/capacity decision: 26% of retained DeepSeek Pro runs starved at concurrency one. |
| #598 | Active: extension-owned partitioned namespaces remain outside a composed-schema tier gate. |
| #592 | Active: authority IDs, minted IDs, titles, and filenames can still encode one subject at multiple spellings. |
| #591 | Active after #641/#642: admission is repaired, but Cockpit still amplifies unvalidated news-match counts when ranking actors. |
| #584 | Active: mutation targets still have no minimum-mutant anti-vacuity floor; pairs directly with #612. |
| #567 | Active data-integrity rule: raw-promoted source pages do not universally require a backlink to the declaring capture identity. |
| #553 | Active UI/data-contract mismatch: Cockpit still conflates `confidence` with the more populated attribution-confidence signal and hides meaningful low values. |
| #551 | Active design/build item: the adjudication harness qualified a model, but governed agent decision authority and audit sampling have not shipped. |
| #548 | Active in source: `review_autoverify` explicitly holds tombstoned pages instead of dropping moot review work. |
| #545 | Active in source: generic error classification still precedes hard-timeout classification; deployment verdict parity also remains. |
| #544 | Active runtime sizing/query problem for two oversized lanes; resolve with #545 so the outcome is observable. |
| #542 | Active: infrastructure/run receipts can still enter the human page-review queue. |
| #529 | Rebased from historical P0 to a P1 residual epic over #584–#588 and #603; most of the original testing rollout is complete. |
| #516, #515 | Active migration pair: URL identity is enforced for new writes, but the legacy duplicate-source corpus and weak IDs still require a verified cleanup. |
| #513 | Active pool-consumer follow-through: adopt complete pool capacity/outcome signals and address the context-wall lane. |
| #506 | Active but partially mitigated: ordinary security prose now passes, while quoted actionable directives still need structural trusted/untrusted separation. |
| #505 | Active fleet-efficiency invariant: every cost-bearing composed lane needs explicit iteration, batch, and least-privilege bounds. |
| #504 | Active observability contract: deterministic jobs still cannot attest the artifacts they produced in authoritative run records. |

#### P2 — bounded hardening and standards work

| Issue | Current assessment |
|---|---|
| #603 | Active, bounded coverage-boundary cleanup for `ci/`; distinguish untested code from subprocess-measurement and test-harness exclusions. |
| #588 | Active: release observation windows and rollback thresholds remain undefined. |
| #587 | Active: no dedicated lane validates emitted metrics/logs/traces as product contracts. |
| #586 | Active: no owner/expiry/rerun policy exists for nondeterministic or environment-contended tests. |
| #585 | Re-scoped: one autospec fixture now exists, but remaining mock-based tests have not adopted the standard. |
| #556 | Active external-infrastructure exposure: the IPv6-only Docker CDN path can still red otherwise valid pipelines. |
| #555 | Active compatibility-test defect: one `Path.stat` race fixture was repaired, but invocation-count coupling remains in the confirmed inventory. |
| #552 | Active developer-experience debt in the changed-mutation responsibility boundary. |
| #543 | Active but latent: name-regex marking still recruits deploy-tooling tests into the security lane even though it is currently green. |
| #512 | Re-scoped: immutable images and session reaping landed; served-model fingerprint pinning and same-harness bimodal generation evidence remain. |
| #497, #495 | Valid carried-patch/upstream design records. Keep bounded to upstream retirement criteria; do not expand local brand-specific behavior. |
| #350 | Active cross-product contract for exporting reviewed, non-circular identity-boundary dispositions to the upstream grouping system. |
| #201 | Active strategic collection gap: reusable primary/structured/private connector capability still trails the analysis layer. |

#### P3, externally held, and product backlog

| Issues | Disposition |
|---|---|
| #617 | Monitor only: measured max-token truncation is 0.05%; gather per-lane completion distributions before changing the cap. |
| #615 | Externally held for `ollama-friday#17`; recalibrate only after the serving-side timeout contract settles. |
| #468, #467 | Small contained test-isolation and dead-policy-branch defects; safe after the active correctness queue. |
| #176, #175 | Retain as low-priority vertical PRDs; they are product choices, not engine correctness work. |
| #149 | Explicitly deferred until a real multi-pack or sidecar deployment makes secret scoping operationally necessary. |
| #124 | Retain the signing/supply-chain remainder; the immediate trust gate and sidecar sandbox already landed. |
| #112 | Retain as a security-pack product expansion, sliced one vertical lifecycle at a time. |
| #77 | Partially delivered by `framework export`; remaining work is public/static publishing, release formats, and redaction policy. |
| #71 | Retain for multi-user deployments; scoped extension tokens exist, but user-facing RBAC does not. |
| #68 | Partially delivered by ops health and CLI rebuild; remaining scope is a safe operator UI and governed triggers. |
| #62 | Retain as ecosystem/catalog UX, below correctness and deployment integrity. |
| #45 | Retain only as a measured semantic-recall optimization; do not GPU-accelerate qmd without a current value/cost result. |
| OKPacks #80 | High-priority CTI-framework epic: grounded reference pages, structured fields, and grading. |
| OKPacks #78 | High-priority actor-model follow-through: motivation, graded alias crosswalk, and promotion gates; coordinate under #80. |
| OKPacks #43 | Product discovery backlog for an investor/thesis pack; no correctness dependency. |

#### Recommended execution order

1. Restore the trustworthy engineering signal: #612, then #611 and #584.
2. Stop review/control-plane pollution: #539, #542, #548, then reconcile #604.
3. Repair runtime recovery and bounded execution: #608, #613, #544/#545, #505.
4. Finish identity and analytical integrity: #592, #591, #553, then OKPacks #78/#80.
5. Complete legacy source identity cleanup: #515/#516 and provenance guard #567.
6. Add deterministic artifact accountability (#504), then the remaining P2 testing,
   release, connector, and upstream-retirement work.

This ordering intentionally puts trustworthy mutation/CI signal before additional feature
delivery, and write-boundary correctness before cleanup jobs or UI polish.

### Status refresh — 2026-08-29

This refresh is the current operational view; the 2026-08-27 audit above remains as the
historical decision record. GitLab reports **35 open OKEngine issues**. Work completed since
that audit materially changes the recommended order:

- The actor-identity stack #515 → #592 plus #646 has merged. Canonical title selection now
  prefers the human-readable actor name over a wiki slug; assessed origin is rendered as an
  estimative judgment; generic descriptors, malware/ransomware names, and country names are
  excluded from actor ranking. #516 and #567 remain open for legacy source cleanup and live
  corpus proof, and #647 remains open for direct-writer separator-equivalence enforcement.
- The testing-standard children #584–#588 and #603 are closed. Exact pipeline 7203 exercised
  all release lanes; pipeline 7205 measured 45,551 statements and 17,170 branches with zero
  misses or partial branches. This document's testing record now reflects the delivered mock
  fidelity and flake controls. #529 is retained only until this reconciliation merges.
- #612's gate semantics now fail closed on baseline failure, keep waived survivors in the
  denominator, and require a validated owner/approver/rationale/issue/expiry record. Scheduled
  pipeline 7207 is the current exact-main full-campaign proof and remains in progress; #612
  therefore remains open.
- A live deploy exposed a new P0, #650: the gateway held the corpus lock for more than eleven
  hours while TCP health and the scheduler watchdog remained green. Forced recreation could
  not stop the D-state container. MR !971 adds bounded lock acquisition and holder evidence;
  age-aware watchdog failure and live host recovery are still required.
- #539, #542, #548, #604, and #608 have merged implementation work but stay open until the
  deployed runtime proves collision convergence, queue deflection/drop, orphan reconciliation,
  and MCP registry recovery on the exact released revision.

#### Current open-issue list

| Priority / class | Issues |
|---|---|
| P0 | #539 review collision convergence; #612 full mutation proof; #650 wedged corpus lock and false-green health |
| P1 correctness/reliability | #506, #513, #516, #529, #542, #548, #551, #567, #598, #604, #608, #647, #649 |
| P2 / bounded hardening | #201, #350, #495, #497, #512 |
| P3 / monitor | #467, #468, #615, #617 |
| Product, design, or deferred backlog | #45, #62, #68, #71, #77, #112, #124, #149, #175, #176 |

#### Revised execution order

1. Complete #612's exact-main full mutation campaign and resolve any real campaign or
   Python-forward failure without treating partial output as coverage.
2. Finish #650: merge bounded acquisition, make stale lock/run evidence fail health, recover
   the live host, and deploy once from a clean immutable revision.
3. On that deployment, prove #539/#542/#548/#604 and #608 end to end, then close only the
   issues whose live acceptance evidence is satisfied.
4. Verify the Cockpit actor list has neither generic/non-actor rows nor separator-equivalent
   duplicates, and complete the remaining #516/#567/#647 cleanup boundary.
5. Close #529 after the testing record merges, then address the remaining P1 product and
   reliability queue by measured user impact.
