# Changelog

Notable changes to the OKEngine layer. Versions track `engine_release` in
`engine-manifest.yaml` (and `pyproject.toml`). Issue refs are `okengine#NN`.

> **Highlights since the public v0.3.5.** The public snapshot sat six releases behind the
> working engine; the arc v0.4.0 → v0.10.1 (detailed per-version below) added, in broad strokes:
> **multipack composition** — one engine → one vault → *many* packs, with globally-disjoint type
> ownership, `framework compose-preview`, and `framework install-domain` (walk-up + taxonomy shapes);
> the **extension system** — three-tier discovery + schema-owning `okengine.*` operations (glossary,
> predictions, lacuna, events, …); the engine-owned **OKF core** (`config/base-schema.yaml`) so packs
> compose without a base pack, plus `type_aliases` + `schema_type_drain` for non-breaking renames; the
> **pack family + publish pipeline** — a catalog, `deploy_matrix.py`, and publish-parity gates; the
> **About panel** (reader/cockpit deployment purpose + composition from live state); and now **pack
> bundles** (v0.10.0). If you are jumping from v0.3.5, read v0.4.0 onward.

## v0.10.4

A follow-on to the v0.10.3 release that closes the version-reporting drift the release itself
exposed.

### Fixed
- **Runtime version stamp self-heals against the running engine** (okengine#192) — the About panel
  and the `deployment_validate` pin check read the engine version from a deployment stamp written
  ONLY by `ensure-runtime` at initial deploy, so an image roll (rebuild+recreate) without a re-stamp
  left it stale — About reported a version the deployment wasn't running (found live: the whole fleet
  showed v0.9.1 while running v0.10.3). `build-engine-image` now bakes the running version into the
  image (`/opt/hermes/.okengine_release`); `check_pins` reads it (the Hermes INSTALL dir, not
  `HERMES_HOME` which is the data dir) and, on a stamp desync, self-heals the stamp to the running
  version + WARNs — so About self-corrects and the missing re-stamp in the roll is surfaced.

## v0.10.3

A live-analyst iteration on v0.10.2: making cockpit aggregates navigable, load-balancing web
search across providers, enabling Agent Chat safely, and closing three enforced-write-path gaps
the pre-release invariant audit surfaced.

### Added
- **Cockpit drilldowns** (okengine#189) — bignums/bars/chips were dead-end counts; every aggregate
  now opens its filtered page list via `/api/drill` (or the page directly for `value_field` bars).
- **Web-search provider rotation + native Serper** (okengine#190) — `web.backend: rotate`
  round-robins across the keyed backends (carried patch 08) to spread free-tier rate-limit load; a
  `plugins/web/serper/` overlay + patch 09 add Serper as a first-class backend, keeping Hermes pinned.
- **Secure-by-default Agent Chat toolset** — `config.yaml.template` ships
  `platform_toolsets.api_server: [okengine, okengine-write]` so enabling chat can't inherit a broad
  (shell/code) toolset by omission.

### Changed
- **Source citations link the original article** — the brief's `Source: [[sources/…]]` title now
  links straight to the source page's `url:` (reader + cockpit), not an internal stub.
- **Unmapped `group_by` codes are flagged** (okengine#188) — a partial `labels:` map surfaces the raw
  code as degraded instead of masquerading as a curated label.

### Fixed
- **Partition-unaware-writer duplication** (okengine#54) — `no_agent` importers wrote flat while the
  reshelve drain sharded, re-creating duplicates (~5,800 across 8 namespaces live). `okf_migrate`
  gains `canonical_key`/`find_page`/`is_partitioned`; a `check_partition_dups` FAIL gate + a
  `dedup_partition_collisions` cleanup drain.
- **Enforced write-path gaps** (invariant-audit) — tombstone now marks the in-process id registry
  (a same-process tombstone→converge no longer resurrects a page); schema `reserved_files` is honored
  by the write path (was validator/docs-only); the future-date guard runs on converge/patch/append.

## v0.10.2

Patch+: the first day of v0.10.1 in production — every gap a live analyst hit, fixed at the
enforced boundary with a red test, plus one real feature (the cockpit analyst home tab).

### Added
- **Cockpit analyst home tab** — the cockpit was a set of disconnected tabs plus a flat
  all-page-links dashboard: a map, not a route. `/api/home` composes the vault's LIVE surfaces
  in triage order — latest briefs → what moved (watchlist + trends) → open predictions →
  knowledge gaps (lacuna) → the pack's curated dashboards as jump-off chips. Config-driven and
  domain-agnostic; an empty/unconfigured surface is omitted, so a young vault never renders
  placeholder walls. `home` joins the default tabs; packs with explicit `tabs:` opt in by
  listing it.

### Changed
- **`build-index-tree` runs intraday (every 6h)** — a nightly-only INDEX build meant pages
  ingested during the day didn't appear in namespace listings until the next morning (hit live
  twice; cyber-market had a pack-level workaround, now retired). Freshness of an engine-generated
  artifact is an engine default. Hours sit outside the 01–02 DST window; the DST guard test now
  parses comma-list hour fields, and a red test pins the intraday cadence.

### Fixed
- **Write path rejects future record-keeping dates** — a weekly-brief lane fabricated
  `published: <next Sunday>` onto an empty stub despite its prompt forbidding it; prompts are
  the unenforced half. `create`/`update` now reject future `published`/`updated`/`created`/
  `last_updated` (+1 day TZ tolerance; domain dates like a KEV `due_date` are never checked;
  update checks only patch-supplied fields so legacy pages stay fixable).
- **Briefing wikilinks must resolve** — the daily brief shipped 4 dead links from invented
  slugs ([[entities/q/quimarat]] for the real `quimat-rat`), and the broken-wikilinks drain's
  ≥3-inbound wake gate treats single-ref brief links as orphan noise forever. Create/body-update
  of a `briefings/` page now rejects unresolvable `[[targets]]` with did-you-mean suggestions
  (sources keep their legitimate forward-references); the drain treats any briefing-cited broken
  target as high-impact regardless of inbound count.

## v0.10.1

Patch: post-cut hardening from the v0.10.0 pre-release **invariant audit** (a 58-agent cross-surface
sweep). No new capabilities; every fix carries a red test. 17 confirmed findings — 8 fixed here + in
the paired security MR, 9 deferred to okengine#184.

### Fixed
- **Two MCP-token leaks** (publish-critical): the pack scaffold `.gitignore` committed generated +
  secret `.okengine/` artifacts (a stale composed-schema that governs the write path + injected MCP
  tokens); a no-secrets `framework backup` captured `config.yaml`'s live `Bearer` token while printing
  "(secrets excluded)". Now gitignored (scaffold + all shipped packs) and redacted.
- **Resharded write path** (#2): `_normalize_entity_shard` collapsed a two-level resharded entity
  shard (`entities/<l>/<2nd>/<slug>.md`) back to one level, so after the nightly reshard drain the
  enforced write path REFUSED/duplicated writes on a mature vault. Now filesystem-aware.
- **Budget guard** (#3/#14): a trip set `paused=True` even when zero crons actually paused (fail-OPEN
  — the cap leaked spend forever); `resume()` cleared `paused` even when resume calls failed (fail-
  CLOSED — crons stranded disabled). Both now honest about partial failures and retry.
- **DST-window crons** (#17): six engine fixed-hour crons sat in the America/New_York DST transition
  window (01:xx/02:xx), so the nightly derived-index chain double-fired on fall-back / was skipped on
  spring-forward. Shifted out of the window; a guard test fails any engine cron there.
- **Stale scaffold pin** (#15): `new-pack.sh` hardcoded a `v0.2.0` engine default that drifted from
  the manifest — now derived from `engine-manifest.yaml`.
- **Time-bomb / vacuous tests** (#16, messaging): the upgrade rollback test asserted a path that never
  exists (a state-cleanup regression would ship green); the content-pegs wake-gate test time-bombed
  once "today" drifted past its lookback window. Both made deterministic.

Fixes surfaced by the public mirror's CI (the first snapshot since v0.3.5 ran the reader/extension
tests on a full dependency set — locally they self-skip without flask):
- **panel-svg charts destroyed by the markdown pipeline**: the `nl2br` extension injects `<br/>`
  between SVG shape lines, and `<br>` is an HTML5 foreign-content *breakout* tag — a spec-following
  sanitizer (nh3 ≥ 0.3.6) closes the `<svg>` there and silently drops every shape. panel-svg blocks
  now bypass *markdown* (stash/re-insert) while still passing through the full `nh3` allowlist.
- **critic wake-gate dead on Python < 3.13**: `Path.glob("briefings/**")` matched directories only
  before 3.13, so a pack's `critic_flagship` glob selected 0 pages and the gate never woke on
  3.11/3.12 runtimes (deployed gateways run 3.13 — production unaffected). Trailing `**` patterns
  are normalized to `**/*`.
- Two reader tests updated for behavior that changed deliberately (About-panel empty-state fields;
  `sources/` dropped from the backlink graph by default since #179) — the stale assertions rotted
  unnoticed behind the flask self-skip.

Two further fixes surfaced by the paired library's pre-publish **deploy-matrix** (validate × compose ×
co-install over every public pack):
- **install-domain persona idempotency**: `append_persona` only recognized a prior install when the
  `## Installed domain:` marker line embedded the slug / pack-name, so a pack shipping a *friendly*
  heading (`## Installed domain: <title>`) slipped past the check and re-apply double-appended the
  persona block. Now stamps a wording-independent provenance marker on append and checks it first
  (legacy heuristic kept for hosts installed before the marker existed).
- **compose-drift false positive**: the port-offset drift check greps the raw compose text, so a
  *commented-out* example binding showing the un-offset base port (a doc line) was flagged as drift.
  Full-line comments are now stripped before the scan; an actual base-port binding still fails.

Nine further MEDIUM findings (uid-stage ownership, base-schema deploy surfaces, health deadman,
alias-shadow test gap, upgrade atomicity/downgrade, reader ns-exclusion, composed-schema staleness)
are pre-existing latent traps — not v0.10.0 regressions — tracked in okengine#184.

## v0.10.0

Minor: **pack bundles** — a pack can now compose other packs instead of owning types itself,
so a family of focused packs installs as one command. Driving case: `okpack-sec` decomposed from
a 14-type monolith into six composable packs + a bundle that recomposes them (okengine#181, #182).

### Added
- **`kind: bundle` pack type** — a pack that owns nothing and declares a recipe
  (`bundle: {host, compose[]}`). `framework pull <bundle>` resolves it: fetch the `host` as the base
  vault, then `framework install-domain --apply` each `compose` pack onto it (the recipe's
  `port_offset` governs the composed host). `framework validate` validates the **recipe** (owns-nothing,
  a host, a non-empty compose excluding the host, every member in `requires`, **no nesting**) and skips
  the schema/persona/crons/feeds checks a bundle doesn't have. `pack_meta` gains the `kind`/`bundle`
  grammar + `validate_bundle_recipe`; documented in `docs/authoring-a-pack.md` §2a.
- **`type_aliases` carried through composition** — `framework install-domain` now merges a guest's
  `type_aliases` (not just `types:`) into the composed host, so STIX/legacy names resolve to the
  friendly canonical types across a composed vault (host-wins; an alias that would shadow a
  host-owned type is skipped, and `coinstall_preflight` surfaces it).

### Fixed
- **install-domain alias merge into an inline flow-map host** — a host declaring `type_aliases` as an
  inline `{a: b, …}` map (multi-line) got a *second* `type_aliases:` key appended, and YAML
  duplicate-key resolution silently dropped the host's own aliases. The guest pairs are now injected
  into the inline map. (Caught only by an end-to-end bundle-pull parity check, not unit tests.)

## v0.9.1

Patch: post-cut hardening — the v0.9.0 features exercised against **live deployments**
(first real multipack installs, a cold agent-lane cycle, publish staging), with every
gap that surfaced fixed. No new capabilities; all fixes carry regressions. This is the
release-readiness pass.

### Added
- **Publish-parity gates + `check-public-parity.sh`** — the library publish refuses to
  stage a catalog whose `engine_version` disagrees with the PUBLIC engine snapshot;
  publish order (engine → library) is enforced, and a checker reports drift after.
  Found live: the public catalog sat six releases behind the working repos.
- **`deploy_matrix.py`** — build/deploy-test harness encoding the manual
  install→probe→teardown protocol: offline tier (validate × conformance × compose combos
  × co-install matrix into fresh scratch hosts) runs in the publish gate; `--live` tier
  drives real docker stacks (pull → deploy → post_deploy_verify → teardown on success).
  All six deployable packs pass the live cycle.
- **About panel: deployment purpose + composition** — the reader and cockpit About cards
  now show the pack's `description`/`mission` (new declared fields), the installed-
  alongside domains (from the installer's markers), sub-domains, and the enabled
  extensions **with their own manifest descriptions** — all derived from live state, so
  nothing is hand-written or rots. `framework validate` WARNs on a missing description.
- **`framework install-domain`** now merges owned namespaces (partitioning + permissions +
  tier + dirs), copies lane scripts into the host staging source, and refuses to
  auto-merge engine-template prompts — four gaps found by the first REAL co-installs.
- **Cold-start**: `ensure-runtime` stages the pinned, sha-verified `iwe` binary for the
  gateway (the `backlinks-refresh` cron needed it but the gateway image ships none — it
  silently failed on every cold deploy); `post_deploy_verify` checks it at deploy time,
  not only the weekly lane; `docs/cold-start-checklist.md` documents the first-deploy
  experience.

### Fixed
- **`deployment-validate` pin check was vacuous** — it read stamp keys `ensure-runtime`
  never wrote, so a stale v0.6.1 deployment pin sailed through weekly validation; now
  reads the real keys, with a cross-file contract test.
- **Alias-shadows-a-declared-type is a pack-side FAIL** (was WARN) — the severity now
  agrees with preflight and the deployment lane, so a shadowing alias can't reach a
  deployment through the earliest gate.
- **Subtree-shape preflight** compared the pack's standalone namespaces against the host
  root (false-positive on unrelated host dirs); now checks only the subtree.
- **`framework pull --port-offset`** collapsed multi-service container ports onto one host
  port and pinned container names (colliding with a production instance) — sequential
  ports + `-o<offset>` names now keep instances distinct; a fourth `set -euo pipefail`
  + no-match-glob silent death hardened to `find`.
- **`post_deploy_verify`** treated compose's `:0` (unpublished port) as a binding and
  false-FAILed the MCP check on every bridge-internal-MCP stack (the skeleton default).
- **review-queue dashboard** emitted dead file-relative links instead of `[[wikilinks]]`
  — every queue row walked the browser out of the reader SPA.
- **Scrub-pattern parity** — the library publish scrub synced to the engine's full pattern
  set; the internal-tracker (`GitLab #NN`) reference class added; a contract test pins the
  pre-commit patterns as a subset of the publish scrub.

## v0.9.0

Minor: **multipack operations** (install-domain, collision preflight, mode-neutral packs),
**graph-artifact analytics** (the precomputed backlink graph + the actor-risk ranker built on
it), and the **Hermes v0.18.0 pin**. 105 commits since v0.8.0.

### Added
- **Hermes pin v0.17.0 → v0.18.0** (`v2026.7.1`); ensure-runtime hardening for the roll
  (SOUL.md unlock, robust cron-plus manifest parse, pinned-plugin install fails loud).
- **`framework install-domain` (#173)** — both co-install shapes (walk-up subtree /
  taxonomy-augmenting) automated: preflight on what actually lands, key-based merges
  (type / rule id / job name / prompt key / xmlUrl / persona marker) so re-runs are no-ops,
  dry-run default. New pack convention: `subdomain/PERSONA.md`.
- **`coinstall_preflight` (#173)** — 7-surface collision checker for multipack installs;
  idempotency-aware (already-installed ≠ conflict), host type_alias shadowing is a FAIL,
  `--subtree` mode for walk-up contracts.
- **Mode-neutral packs (#173, docs §8)** — packs ship standalone + `subdomain/` co-install
  forms; single-source rule enforced by a new `framework validate` subdomain-form check;
  subtree naming standard (domain dir = the pack's domain slug).
- **`backlinks-refresh` (#168)** — engine cron precomputes the IWE backlink graph into
  `wiki/.backlinks.json`; reader + cockpit serve the artifact (48h mtime ceiling) and fall
  back to the live in-container build only when it is missing/stale. One graph build per
  deployment per day, off the UI containers entirely; the cockpit gains the generated-source
  filter + curated titles its live build never had.
- **`okengine.actor-risk-ranking` (#170, #174)** — deterministic target-relative ranking over
  the backlink artifact: explainable edge-set drivers, distinct-origin-domain confidence (a
  syndicated report can never lift a band), unknowns cap the band, person targets refused at
  config parse, aliases fold by declaration. Driver type sets are scoring config, so the
  vendor variant (okpack-vendor-risk) is config, not a fork.
- **`okengine.relevance-gate` (#167)** — consumer-side ingest scope filtering: deterministic
  prescore queue + budgeted classify; mechanism generic, scope entirely pack config.
- **`okengine.completeness` + gap-drain** — declared-expectation gap engine (field / link /
  companion / freshness rules, pack-side) with the resolution half: `fix: agent|agent-draft|
  human` tiers drain bounded batches through the MCP write path; per-rule precision surfaces
  noisy rules.
- **`deployment-validate`** — weekly in-gateway self-validation lane (pins vs runtime stamp,
  composed schema/alias shadows, sub-domains, jobs.json integrity, extension staging,
  ownership sweep, auth posture); exits non-zero on FAIL for fleet-health attention.
- **Ownership guardrails** — `vault-exec.sh` (exec as the vault uid), `fix-vault-ownership.sh`
  (container-root chown repair), the validate-lane ownership sweep: prevent/detect/repair the
  root-stray class.
- **Generic daily-brief lane (#169)** — `select_daily_brief.py` what-changed digest replaces
  the per-pack brief-cron clone class; the pack ships only the prompt
  (`crons/engine-template-prompts.json`); briefs are morning products (fixed slot, never
  `@jitter`).
- **Skeleton validator unification (#169, `VALIDATE_VERSION 2026.07.2`)** — content-layer
  checks (type aliases, enum well-formedness, page enum/required/rels/refs) generalized into
  the skeleton; pack-specific checks ride the new `validate_extra.py` hook. New
  `framework validate` structural checks: compose drift, prompt residue, validator vintage.
- **Import-migration tooling (#165)** — `dedup_entity_slugs` (pre-migration duplicate-slug
  resolution with per-pair checkpoints + live-vault race tolerance), `okf_migrate` re-nest of
  non-canonical layouts with collision hold-back.
- **Server-side strategic charts (#172)** — panel SVG rendered in the page body (true Wardley
  stage bands + value-chain edges, quadrant charts), single-line SVG emission (python-markdown
  paragraph-split class), reader nh3 allowlist for static SVG; `concept-enrich` lane.
- **Docs** — the research-shareable technical reference, the application catalog (#171), the
  federation evaluation (#166: read-only mirror recommended; the six open questions answered
  at code level), the census-grounded actor-risk design (#170), pack-building challenges.

### Fixed
- **`tombstone_entity` now clears the namespace permission matrix (#166)** — a tombstone IS an
  update; an `update: false` namespace (human-authored findings/, a federated lookup/ mirror)
  could previously still be tombstoned through this one path.
- **qmd reindex debounce** — write bursts starved MCP calls into 300s timeouts; change-triggered
  reindexing now debounces (`OKENGINE_MCP_INDEX_MIN_UPDATE_SECONDS`).
- **`.bak` sidecar convention** — single non-indexed sidecar; dated `.bak.<ts>.md` copies had
  accumulated unboundedly as indexed pages.
- **Wardley/cockpit rendering** — anchored scope, sharded-link in-degree, cache-busting, UI
  extension panels in the page overlay.

## v0.8.0

Minor: the **cockpit UI**, the **predictions audit→remediation stack**, and **release/LLM-call
discipline** — the round that closed the origin-system operational-parity port.

### Added
- **okengine-cockpit** — a config-driven, domain-agnostic intelligence cockpit reader (briefings /
  predictions ledger / dashboards / competitors + Browse + Chat tabs ported from the reader,
  A−/A+ font control, dashboard groups that never hide a page). Ships as a skeleton compose
  service (`{{COCKPIT_PORT}}`).
- **`okengine.messaging-synthesis`** — configurable vendor positioning synthesis (content-pegs,
  positioning-battle-cards, value-prop-gap-refresh + a drift-gated meta-lane). Product identity is
  pack/operator config; the extension ships zero vendor identity.
- **Predictions: the full audit→remediation discipline stack (okengine#159).**
  `prediction-schema-audit` (field hygiene) + `forecast-review` (weekly meta-layer) complete the
  measurement side; `prediction-structural-backfill` (authors missing `## What would refute this`
  falsification criteria, append-only) + `prediction-schema-drain` (normalizes frontmatter value
  drift, merge-writes, batch-container flagging) are the remediation drains that FIX what the
  audits measure.
- **`source-portfolio-watch`** — no_agent corpus-COMPOSITION dashboard (publisher concentration,
  ingest-mix drift, reliability distribution, prediction-bearing coverage); complements
  source-staleness (age decay). Generic: `signal_class` sections collapse for packs without it.
- **`llm_lib` + the call-discipline gate.** One sanctioned direct-LLM-call path for engine/pack
  scripts — reasoning/thinking OFF by default (the qwen truncation class), explicit opt-in,
  truncation raises instead of parsing as a bad answer; `tests/test_llm_call_discipline.py` FAILS
  the build on raw chat-completions calls outside it.
- **Cron operability.** Per-deployment cadence override (`.okengine/extension-schedules.json`),
  off-peak deferral for bulk drains (`CRON_DEFER_UTC_HOURS`), TZ env wiring for local-time
  schedules (default UTC).
- **`framework import` (okengine#154)** — the layout step: link-preserving cross-namespace
  re-home + `collapse_source_dates` date-depth normalization.
- **INDEX pages** — fold the namespace `_about` card into the top of its INDEX; an `Updated` date
  column (from the write-path auto-stamp) so undated slugs (lacuna/entities/concepts) show WHEN.

### Fixed
- **Release hygiene / CI gates.** `pytest` now pins `--import-mode=importlib` in `addopts` (the two
  `test_compose.py` modules collided under the default importer, breaking CI collection); cleared
  three `ruff E9/F82` errors incl. a Python-3.11-invalid f-string that broke import on the
  advertised runtime; version metadata realigned.
- **Cockpit auth parity (okengine#90 P4a).** The cockpit — a superset of the reader on the same
  host bind — now enforces the SAME Basic auth + private-vault fail-safe (shared credential); the
  compose template wires it, `framework validate` gates it. Previously it published
  unauthenticated even when the reader was password-protected.
- **Cockpit/reader UI.** The IWE backlink graph build no longer blocks requests (async
  single-flight; the hourly freeze), backlink TTL 1h→24h (env-configurable), wiki-table dates/slugs
  never wrap (the last column carries the wrapping; over-wide tables scroll), assorted cockpit
  polish (slide-over width, ledger claim wrapping, header nowrap, font-scale fixes).
- **Validator** — the standalone pack validator validates the MERGED schema (okengine#163), and
  deploys fail loud when a lane's script isn't staged.
- INDEX links are full-path wikilinks; `_`-prefixed scaffolding excluded from page lists;
  `forecast-review` writes to `briefings/` (the agent-narrative namespace), lacuna required-fields
  pinned, competitive-analytics write target pinned.

### Docs
- **`entity-partitioning.md`** — the canonical layout + write/reference contract (by-letter;
  bare-slug refs; the duplicate-canonical failure mode, okengine#165) and **`common-issues.md`** —
  symptom→cause→fix for the recurring pitfalls (cron ok≠done, model pinning, trust-the-digest,
  reasoning-model thinking, importlib collision, …). Both cross-linked from the authoring guides.

## v0.7.0

Minor: a **trust + observability + competitive-intelligence** layer on top of the v0.6.x core.

### Added
- **Trust backbone.** Source-grounding audit (does each entity cite a source that *exists*?,
  okengine#161 follow-on) + `okengine.grounding` Tier-2 semantic lane (do the sources *support* the
  claim?); human-in-the-loop **review queue + `framework review` sign-off** (okengine#69); the
  reader **provenance/trust strip** on every page (okengine#70).
- **Observability.** `fleet-health` monitor for silently-failing cron lanes (okengine#161), the
  **operator dashboard** rolling up engine + vault health (okengine#60), and a metrics+alerts
  **`health-export`** (Prometheus textfile + transition alerts, okengine#64).
- **Competitive analytics.** `discover-competitors` — propose off-watchlist candidates from the
  ingested graph (co-occurrence / segment / prominence / "alternatives-to-X" language mining).
- **Reader extension panels** (two-axis + fields kinds, okengine#160) and `okengine.viz` Wardley maps.
- **`framework import`** — adopt an existing (foreign) vault into a pack (okengine#154).

### Fixed
- **`wiki-change-check` excludes generated INDEX/reserved files** — index-rebuild churn no longer
  floods the lint changeset (was overrunning the lint agent into truncation).
- `fleet-health` precision (real ERROR/CRITICAL only); review sign-off clears any reason.
- **Scale.** libyaml frontmatter parsing for full-vault audits (~4.5x; okengine#74) + a benchmark.

### Docs
- `docs/model-selection.md`: the silent "completes but writes nothing" failure mode — an
  under-powered model on a synthesis-and-write lane reads/reasons but never emits the write tool;
  route such lanes to a capable model.

## v0.6.1

Patch: the OKF envelope's `created`/`updated` are now real ISO-8601 **timestamps**, so the UI can
track *when*, not just *which day*.

### Fixed
- **ISO-8601 timestamps for `last_updated`/`created`/`updated`.** The OKF spec defines these as
  timestamps, but the write path stamped date-only and the reader truncated to `[:10]`. Now the
  enforced write path stamps a UTC timestamp (`_now()`), the reader prefers `last_updated`, drops
  the truncation, and renders `YYYY-MM-DD HH:MM:SS`; the dashboard generators
  (kb_health/page_quality/detect_field_loss/refresh_kb_dashboards/source_staleness/contradictions/
  timeline/events) stamp timestamps too, and `sanitize_frontmatter_updated` is timestamp-aware.
  Backward-compatible (date-consumers slice/regex the prefix); existing date-only pages are left
  as-is rather than back-dated.

## v0.6.0

The **extension API** becomes a real contribution model, three new first-party extensions ship on
it, and KB-health learns to tell synthesized content from imported reference catalogs.

### Added
- **Extension API — drop-in contributions (#63).** A formal design
  (`docs/extension-api-design.md`): extensions contribute files into typed locations and the
  engine collects them forward-only (no bespoke per-type fold, no reverse-split). Phase 1 — the
  cron drop-in collector — lands: an extension supplies ops as `crons/*.cron.json` (one op per
  file) instead of a manifest `operations:` block, so adding a lane is dropping a file.
- **Cross-lane ordering — `after:` (#129).** A hard cross-job dependency (distinct from the
  advisory `tier:` hint), validated at deploy: fail-loud on a missing target, self-reference, or
  cycle. Runtime enforcement is a designed, deferred phase (`docs/cron-ordering-design.md`).
- **`okengine.lacuna` (#145)** — structural-gap discovery: maps a field from the real concept
  graph, names the force keeping a cell empty, proposes a density-confident fill. Plain-language
  TL;DR + glossed headers.
- **`okengine.frontier-watch` (#147)** — capability-frontier / demand-supply whitespace discovery
  (the first extension on the drop-in model).
- **`okengine.events` (#155)** — deterministic dated-event ledger + scoring (event types/weights
  are pack config).
- **`okengine.critic` (#157)** — subjective QC over a pack's flagship deliverable; a conditional
  wake-gate (wakes only on hard flags — a cost lever).
- **Per-lane model routing (#151)** — `@profile` model refs + `model-profiles.yaml` to switch
  host/ctx per lane; carried Hermes patches for per-job `num_ctx` + the api_server inference model.
- **Reader** — per-namespace about-cards; fact-sheet reference values + `[[wikilinks]]` render as
  clickable, title-resolved links; configurable font size.

### Changed
- **KB-health distinguishes reference-catalog imports from synthesized content.** Pack-declared
  `reference_types` / `reference_fields` keep CVE / ATT&CK / encyclopedia imports out of the
  orphan + page-quality + broken-wikilink debt metrics — they're link-target scaffolding, not
  defects — and report them as a separate reference-layer count.
- **The enforced write path normalizes frontmatter reference values** to plain wiki-relative paths
  (a bare `[[wikilink]]` in a YAML value mangled into nested sequences — #145).

### Fixed
- **Bare-name link normalizer (#153)** — a deterministic drain rewrites `[[Qilin]]`-style links to
  their canonical entity path (single exact name/alias match); de-orphans entities as a side
  effect.
- **Cron round-trip carries the extensions partition (#152)** — `merge(split(x)) == x` was red
  whenever `cron-plus-jobs.json` held deploy-folded extension jobs.
- **lacuna wake-gate counts sharded concept links (#145)** — the flat-only regex missed the
  sharded vault layout, so it never fired on a real large vault.

## v0.5.0

The extension model **grows up**: it can now express any engine operation — multi-lane,
agent-driven, prompt-customizable, dependency-declarable, default-on-capable — and the
engine's own marquee features (predictions, contradictions) ship as first-party extensions
on top of it. Plus the v0.17.0 vault-write fix and the security/cron-tooling coherence work.

### Added
- **Multi-operation extensions.** A plural `operations:` map yields one namespaced cron job
  per entry (`<id>:<op>`), for an extension with several lanes.
- **Agent operations.** An operation with a `prompt`/`prompt_file` wakes the agent
  (`no_agent: false`) with the okengine toolsets; the entrypoint becomes the wake-gate
  selector. `toolsets` overridable. No prompt ⇒ the existing deterministic `no_agent` script.
- **Bundled, overridable prompts.** Ship generic prompts as files (`prompt_file`); a
  deployment tunes them without forking via `<pack>/.okengine/extension-prompts.json`.
- **Core (default-on) extensions (okengine#142).** `core: true` (engine-tier) makes an
  extension opt-*out* — active unless explicitly disabled. `okengine.contradictions` is the
  first house-baseline core extension.
- **Pack → extension dependencies (okengine#142).** `pack.yaml` `requires:` accepts
  `ext:<id>@>=ver` (and an `ext:<id>` schema owner is an implicit dep); `framework validate`
  FAILS before deploy if a required extension isn't enabled — no silent runtime degrade.
- **Operation `tier:` hint (okengine#129).** Slot an extension job into a kickstart stage
  instead of guessing a wall-clock time (down payment on the full dependency DAG).
- **`okengine.predictions` (first-party extension).** The design's canonical example,
  migrated out of the engine cron fleet: 3 wake-gated agent lanes (candidate-watch / grade /
  regrade), bundled prompts, reuses the pack-owned `prediction` type.
- **`okengine.lacuna` (first-party extension, okengine#145).** Structural-gap discovery
  (lacuna prompting): a weekly agent op that maps a field from the **real concept graph**, names
  the force keeping a cell empty, and proposes a fill with a **density-measured** confidence.
  **Owns** its low-trust `lacuna` type/namespace (`needs_review`, never canonical pages), gated
  by concept-cluster density. Carries a **soft predictions edge** — emits testable fills as
  prediction candidates into `predictions/**` when `okengine.predictions` is enabled, with no
  hard `requires`. Opt-in; generic (all market vocabulary stays in pack config).

### Fixed
- **Agent vault writes on Hermes v0.17.0 (okengine#140).** The baked
  `HERMES_WRITE_SAFE_ROOT=/opt/data` silently denied agent file-tool writes to `/opt/vault`;
  the skeleton + packs now set `=/opt` (creds/host still denied). Guarded by a test.
- **cron_pack_split is extension- and pack-tier aware (okengine#141, #143).** Synthesized
  extension jobs carry an `extension:` marker and pack-domain crons a `pack:` marker; split/
  dump route them to their partitions instead of crashing as "unclassified". Fail-loud kept
  for genuinely-unclassified jobs.
- **Extension trust gate (okengine#124).** Operator-tier in-gateway extensions are refused
  without `--allow-untrusted` (no OS isolation yet).
- **api_server exposure guard (okengine#120).** `post_deploy_verify` flags a non-loopback
  api_server bind.

### Docs
- Complete the extension manifest reference (`extension-system.md §6`) for the new grammar,
  and add a step-by-step `docs/authoring-an-extension.md`.
- Document the engine-contract `schema.yaml` keys missing from `authoring-a-pack.md`.
- **Doc/code parity guards** (extensions + packs): every author-facing manifest / engine-read
  schema key must be documented — the authoring docs are now self-maintaining.

## v0.4.0

The **extension system**: optional, separately-packaged operations over a vault's wiki
data, opted into per deployment, isolated behind the MCP-client contract. Design:
`docs/design/extension-system.md` (+ the per-area specs). Architecture gate okengine#131.

### Added
- **Extension discovery (okengine#134).** Three tiers — engine (`extensions/`), pack
  (`<pack>/extensions/`), operator (`<pack>/.okengine/extensions/`) — keyed by manifest
  `id`, fail-loud on duplicate-id-across-tiers (no shadowing) and on `okengine.*` claimed
  outside the engine tier. `extension.yaml` parse + §6 validation. CLI: `framework
  extensions {list,inspect,validate}`. present ≠ enabled.
- **Enable/disable + cron composition lifecycle (okengine#113).** `framework extensions
  {enable,disable}` manage vault-level state; the composer synthesizes one namespaced
  deterministic cron job per enabled `operation`, fail-before-runtime, folded into the
  generated `cron-plus-jobs.json` (no-op when nothing is enabled). Installer-vs-composer
  documented (`docs/design/extension-lifecycle.md`).
- **In-gateway script staging (okengine#128).** Deploy streams an enabled extension's
  scripts into the gateway at `/opt/data/scripts/<id>/`; the synthesized job uses the
  absolute namespaced path. Copy (not mount); fail-loud staging plan.
- **Scoped MCP (okengine#132).** Per-extension tokens (minted on enable, revoked on
  disable; sha256-only store in the vault), scope-enforced auth in BOTH MCP servers
  (admin token = full, back-compat), a network-reachable `okengine-write` transport so
  out-of-process sidecars can write, and a server-side `extension_id` provenance stamp.
- **Composed schema / bring-your-own-schema (okengine#133 + the #90 P3 N-way merge
  slice).** `schema_lib.compose_schema` folds engine base ⊕ pack ⊕ Σ(extension fragments)
  with an owner map and fail-loud Own/Reuse/Extend rules; the generated
  `.okengine/composed-schema.yaml` is what the validator and write-path guards enforce, so
  an extension's own types validate. Back-compat: no artifact ⇒ the pack schema.yaml.
- **Sidecar contract (okengine#135).** Manifest image-entrypoint (digest-pinned), a
  generated trigger job + `framework extensions sidecar-generate` (compose override +
  trigger wrapper with injected scoped-token/MCP env). Live launch is operator-opt-in.
- **`extensions purge` (okengine#127).** Delete a disabled extension's pages by the
  provenance stamp — disabled-required, dry-run unless `--yes`.
- **`okengine.contradictions`** first-party reference extension (the §11 first slice),
  migrated out of the engine cron fleet into an opt-in operation.

### Fixed
- **cron-plus.sh uid (okengine#136).** The host wrapper auto-detects the gateway's uid
  (the `/opt/data` owner) instead of defaulting to 10000, fixing EACCES on packs that
  override `HERMES_UID`.
- Resolved and verified-closed during extension dogfooding: okengine#110, #114, #115,
  #116, #117, #119 (each had a code fix + regression test; closed).

### Tests
- Extension + schema suites (~95 tests) incl. cross-component **integration** and
  **adversarial scope-matcher** suites. The full chain — discovery → enable → compose →
  schema → stage → run → reader → disable → purge, plus scoped read/write — was
  **live-verified** on a deployed `okpack-ai-research` test stack.

## v0.3.5

### Added
- **Field-enum enforcement in the conformance contract.** `schema_validator` rejects a page whose
  `field_enums` value isn't in the schema's `enums` (honoring `by_type` overrides + `extensible`).
  `feed_fetch` no longer stamps a non-enum `source_kind: feed` on raw items — it writes
  `source_channel: feed` (raw provenance), and the wake-gated ingest agent assigns the real
  enum-valid `source_kind` from the pack schema.

### Fixed
- **Backlinks skip generated dashboards.** Surfacing `dashboards/` for browsing (#117) accidentally
  let its auto-generated digests appear as "what links here" edges; `_skip_backlink_src` now skips
  the surfaced-derived dirs as backlink SOURCES too (browse-visibility ≠ backlink-skip). The reader
  suite skips locally without `fastapi`, so this only failed in CI.

### Docs
- **One-command Docker path surfaced.** INSTALL.md now opens with a 3-command quickstart
  (`clone engine → framework pull → deploy.sh`) and states `docker-compose.yml` ships with the
  pack, not the engine; §7 leads with `deploy.sh`; README stops routing users through the manual
  patch/overlay delta.

## v0.3.4

### Changed
- **Hermes pin bumped v0.16.0 → v0.17.0** (upstream tag `v2026.6.5` → `v2026.6.19`, ~1476
  commits — almost all in code the engine doesn't ship: desktop app, web, i18n). The 6 core
  carried patches apply unchanged; **dropped 2**: patch 07 (cron trusted-digest looser scan) —
  v0.17.0 implements it natively (`_scan_assembled_cron_prompt`'s `has_injected_data` tier) —
  and patch 08 (dockerfile recursive-chmod avoidance) — declined, to stay on upstream's new
  immutable-install permissions model (the ZFS build cost is negligible here: 4m58s with the
  recursive chmod present). cron-plus is unchanged (`run_job`/`_deliver_result` signatures match).
  The new cron-script env-sanitization is no-impact (no engine cron script reads a stripped var).
- **Reader About reports the live runtime** — ensure-runtime stamps the actual deployed engine/Hermes
  (`.hermes-data/engine-runtime.yaml`); the About prefers it over the pack's declared engine.version
  pins, which can be stale/wrong vs the running engine (the deploy even warns on mismatch). (#119)
  Full record (internal): `docs/hermes-upgrades/v2026.6.19-v0.17.0.md`.

## v0.3.3

Closes the recurring **split-brain vault** class, adds the cold-start **kickstart**, and makes
the **reader** surface the vault's synthesized value after install.

### Fixed
- **Split-brain vault (root cause).** `terminal.cwd` was the vault root, not the page tree, so a
  stray relative `file_write` (e.g. `source/x`) forked content out of the canonical
  `<vault>/wiki` tree that the MCP write path + every dashboard use; `WIKI_PATH=/opt/wiki` also
  doubled the tree to `/opt/wiki/wiki`. `cwd` now points at the page tree, and `framework
  validate` fails any pack whose `WIKI_PATH` ends in `wiki`. (#110)
- **Write path enforces `type → namespace`.** `create_entity` rejects a write to a namespace not
  declared in `schema.yaml` (with the closest declared namespace as a hint), so a `type: source`
  page can no longer drift into a stray `source/` instead of `sources/`. (#115)
- **Hot-set / tier date resolution** falls back from the configured `date_field` to the OKF
  envelope `last_updated`, then `created` — entities/concepts (which carry only `last_updated`)
  no longer vanish from the hot set or tier cold. (#116)
- **Kickstart completion** polls `last_run_success`, not `last_run_at` (which advances when the
  selector fires), so agent lanes aren't reported done while their compile is still running. (#114)
- **Engine-version pin check** is patch-tolerant — a `v0.3.0`-pinned pack is satisfied by a
  `v0.3.x` engine. (#104)

### Added
- **Kickstart** — opt-in `deploy.sh --kickstart` populates a freshly-deployed vault now, walking
  the full build/maintenance fleet in dependency order (ingest → compile → score → entities →
  schema/repair → graph → concepts → canonical → predictions → quality → index/dashboards →
  brief) instead of waiting hours/days for the schedule. (#109)
- **Reader surfaces synthesized value.** `dashboards/` (the brief + digests) is now browsable —
  schema `exclude:` scopes conformance, not reader visibility — and the reader lands on the
  curated HOT set instead of an empty rail (brand = Home). `operational/` stays hidden. (#117)

### Deploy / cron
- Generate a real `OKENGINE_MCP_TOKEN` on fresh deploy. (#105)
- Post-deploy verify reads the runtime config at `/opt/data`. (#106)
- Expand engine-cron `@jitter` sentinels at deploy (cron-plus can't parse a raw sentinel). (#107)
- Scope the gateway to the pack's compose project on multi-pack hosts. (#108)
- `$(id -u)` default-uid model propagated across the deploy guides. (#102)

## v0.3.2
- Cron jitter never picks minute 0 — no herd-prone `:00` schedule that fails validation. (#103)

## v0.3.1
- `framework budget --resume` (manual spend-cap recovery) (#97); default image tag tracks
  `engine_release` rather than a hardcoded literal (#101); default `HERMES_UID`/`GID` to the
  invoking user's uid (#102).

## v0.3.0
- Identity layer (id-aware create/converge) + reader/permissions fixes + pullable
  (squashed-continuous) public-snapshot publishing; skeleton entity-path fix.

## v0.2.0
- Normative OKF conformance profile + strict fail-closed validator (#22/#27); cron-plus promoted
  to a first-class pinned dependency.

## v0.1.0
- Initial OKEngine layer extracted from the pinned Hermes-Agent.
