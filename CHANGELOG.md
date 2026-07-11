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

## v0.11.3

Invariant-audit **medium batches 4–8** — cron/lane robustness, the upgrade roll-forward gate,
install/build/verify hygiene, and scaffold cleanup. Every fix carries a red test and was hardened by
the checked-in adversarial re-verify runner, which ran to a clean pass across several rounds and
repeatedly surfaced deeper residuals (including a regression introduced by an earlier fix). Two
low-severity residuals are tracked, not shipped: `okengine#207` (roll-forward can't flag an
out-of-taxonomy *retype* — fail-open by engine design) and `okengine#208`'s deeper deploy-path fix.

### Fixed
- **Roll-forward upgrade gate (`framework_upgrade.py`).** `framework validate` only asserted `wiki/`
  *exists*, so a migration that corrupted page frontmatter passed and never rolled back. The gate now
  runs a **conformance REGRESSION diff** against the pre-upgrade snapshot: it fails only on pages a
  migration made non-conformant (conformant before, broken after), **never** on pre-existing
  non-conformance (a real vault carries some — older/agent-authored pages missing `id`), and it is
  skipped entirely for a pin-bump with no migrations. (An earlier baseline-less exhaustive scan
  false-rolled-back every real vault; the fleet-roll canary caught it.) The migration-id collision
  guard compares `to_version` on a prefix-normalized spelling (`v0.6.0` == `0.6.0`, but `0.6.0` ≠
  `0.6.0.1`), and a same-id pack override with a genuinely different version fails loud. Apply-time
  exceptions now roll back too.
- **Deployment validator (`deployment_validate.py`).** `check_runtime_ownership` now covers the `qmd`
  and `state` dirs; `check_extensions` drives off the **staged** extension dirs (catching a core
  default-on extension the enabled-map iteration missed); the read-MCP baked-vs-staged lib drift and
  the `check_pins` `hermes_pin` one-sided-drift case are now surfaced (M22 "undetectable, never a
  vacuous pass").
- **Panel SVG (`extensions/okengine.viz`).** An agent-authored `two-axis` panel with a malformed
  shape (non-numeric x/y, scalar node, missing coords, a bare-date/`!!set` frontmatter field) crashed
  the *entire* panel refresh lane. Coordinates coerce safely, the node/band/edge collections are
  shape-filtered, `panel_hash` serializes dates/sets **deterministically** (no per-run churn), and the
  lane-facing `svg_block` is guarded so one bad panel skips instead of aborting the sweep.
- **Nightly ledger dates.** `lint_watcher`, `detect_field_loss`, `kb_health` and `page_quality_audit`
  stamped their per-day ops ledgers with `datetime.now(timezone.utc)` — a late-evening lane in a
  TZ-behind-UTC deployment (the fleet is `America/New_York`) filed "tonight's" report under *tomorrow*
  and desynced the cohort. All four now use the deployment-local date.
- **Entity fusion (`canonical_assemble.py`).** `_key` `json.dumps`'d a frontmatter value without a
  default, so a union-mode dict field carrying a bare ISO date crashed the whole canonical-assembly
  run; it now serializes (dates/sets, deterministically).
- **Reader fresh-host cache (`okengine-reader/app.py`).** `_OBS_INDEX_CACHE` and `_SRC_REL_CACHE`
  initialized to `(0.0, {})` instead of `-inf`: `monotonic()` is seconds-since-boot, so on a
  freshly-started reader (a CI runner, or any just-deployed container) `now - 0.0 < _DIR_TTL` read the
  EMPTY initial entry as "fresh" and served a blank canonical→source drill-down + blank reliability
  labels for up to 15 minutes after every start (the sibling caches already used `-inf` per the
  documented note; these two regressed). Caught by GitHub CI (low-uptime runner) after the release.
- **Docs: composition is shipped, not future.** Corrected `README.md`, `docs/overview.md`, and
  `docs/technical-reference.md` — they claimed "one pack per instance" / that multi-pack composition
  "doesn't exist yet." It does: `framework install-domain` + `kind: bundle` merge ownership-disjoint
  packs into one vault behind a coinstall preflight (`framework compose-preview` shows the shape).
- **Health export (`health_export.py`).** Added a scheduler-independent heartbeat gauge
  (`okengine_health_export_timestamp_seconds`) so an alert fires when the exporter itself stops and
  Prometheus scrapes a frozen `.prom`.
- **Deploy / install / build hygiene.** `deploy-cron-scripts.sh` no longer swallows a `stage-panels`
  failure (a panel-type collision) as a cosmetic "skipped"; `install-cron-plus.sh` scopes git
  `safe.directory` so a HERMES_UID-owned managed clone can't brick the deploy on "dubious ownership";
  `install-extract-cron.sh` refuses to install a host cron with no `WIKI_PATH` (it would silently
  no-op on the container-only `/opt/vault`); `framework budget --status/--resume` fails loud on the
  host instead of silently no-oping; `build-engine-image.sh` refuses a *dirty* reused `HERMES_SRC`
  checkout; `post_deploy_verify.sh` checks the cockpit service; the smoke suite now exercises the MCP
  surface (and, in dev mode, a DOM layer skipped for want of playwright no longer fails the run).
- **`framework validate` false confidence (`okengine#208`).** On a loopback deploy it returned at the
  "host ports bind loopback" branch before checking the MCP token — but the containerized MCP binds
  `0.0.0.0` internally, so the built-in default token fails closed (`okengine#50`) and crash-loops even
  on loopback. It now WARNs on the default token with no `ALLOW_DEFAULT_TOKEN`; `.env.example`'s
  misleading "safe on loopback" comment is corrected. (`deploy.sh` is unaffected — `ensure-runtime`
  generates a secret token.)
- **Scaffold cleanup.** Removed the dead `--brief-hour`/`{{BRIEF_HOUR}}` knob (the real control is the
  `OKENGINE_BRIEF_HOUR` env; the knob substituted nothing and even documented a different default) and
  the sibling dead `{{CRON_ID_2}}`; a new test forbids a repl token no skeleton file uses. Dropped a
  specific pack name from a first-party engine extension README (the engine layer stays
  domain-agnostic). The cron-generator test suite no longer silently skips its whole self-contained
  half when the gitignored generated artifact is absent (CI/fresh clone).

## v0.11.2

Invariant-audit **medium batch 3** — cron-generator contracts + the file-tool write-guard. Each fix
with a red test, hardened by the checked-in re-verify runner (multiple rounds; see
`docs/testing-and-audit.md` §6).

### Fixed
- **Cron generator (`cron_pack_split.py`).** A duplicate deployed job **id/name** silently ran one
  job's definition and dropped its twin (cron-plus keys by id) — `validate_unique_ids` now gates every
  write path (regen / regen_composed / the `compose` + `check` CLIs). `dump-from-live` clobbered the
  source's `@jitter`/`@morning` schedule sentinels and `@profile` model refs with one install's
  resolved values — it now restores the source representation across all three schedule shapes
  (name-or-id matched). Multi-pack compose now rewrites intra-pack `after:` targets (domain **and**
  driven engine-template) and runs `validate_ordering`, so a composed fleet can't ship a dangling
  dependency; a domain job that shadows an engine/engine-template lane fails loud.
- **File-tool write-guard (carried patch 01).** The Hermes file tool was a weaker second write path
  around the enforced `okengine-write` MCP: a cron agent carries both, and the file tool enforced only
  schema + read-echo. It now mirrors the write path's structural refusals for `.md` writes under the
  vault — engine-managed **reserved files**, pack-declared **`reserved_files`**
  (`schema_validator.reserved_files_for`), and **tombstoned** pages (never resurrect) — on **every**
  write leg (`write_file` / `patch_replace` / `move_file` / `delete_file`; the V4A patch mode delegates
  through them), so the enforced contract can't be bypassed via the file tool.

## v0.11.1

The invariant-audit **medium burn-down** (the follow-up to v0.11.0's critical/highs + first
medium fold). Two fix batches, each finding fixed with a red test and hardened by the
adversarial **re-verify loop** (see `docs/testing-and-audit.md` §6); the batch-2 render work
took **seven** re-verify rounds to reach a consistent fix across every surface.

### Fixed
- **Write-path integrity** (batch 1). Tombstone-resurrect: the never-resurrect guard existed only
  on the converge lane, so `update_entity`/`patch_entity`/`append_to_section` could silently
  un-tombstone a retained tombstone — a shared `_tombstone_refuse` now guards all three. Converge
  provenance-forgery: `merge_frontmatter` took the caller's value for server-managed keys; it now
  preserves `created`/`created_by`/`discovered_by` (a caller could otherwise forge provenance).
- **Cockpit + reader render on partitioned & walk-up vaults** (batch 2 — the flat-glob-vs-
  partitioned / hardcoded-namespace / reserved-subdir class). A stream/doc/dataset/dashboard/
  observation/prediction/backlink glob that didn't recurse showed nothing on a date-partitioned
  dir; a hardcoded namespace tuple 404'd pack-owned and walk-up sub-domain pages; a leaf-only
  reserved check surfaced `_archive/` retired pages in some surfaces but not others. Now a single
  discoverability guard (`_visible_page`/`_reserved_seg`/`_hidden_page`, and `_skip_backlink_src`)
  is applied uniformly across **every enumeration surface in both apps**, so browse count, served
  ledger, dataset tabs, dashboards, observations, predictions, backlinks, and search all AGREE:
  partitioned/walk-up content is found, `_archive/` retired pages are hidden everywhere, and the
  engine's bare-`_` reshard bucket (`entities/x/_/x-force.md`) stays visible in both browse
  (`len>1` exemption) and search (`!_?*`). The read/write `_safe` twin was aligned on `.md`
  handling (a dotted slug like `openssl-3.0.7-advisory` no longer truncates/desyncs read from write).

### Notes
- Remaining audit mediums (30) + lows (33) are tracked in GitLab issues #203 / #202 with per-item
  FIX/WAIVE dispositions; batches land on `main` and roll to the fleet per point release.

## v0.11.0

Runtime release: **Hermes pin bump v0.18.0 (v2026.7.1) → v0.18.2 (v2026.7.7.2)** — pulled for the
upstream MCP stdio stability series (bounded initialize handshake, orphan reaping, idle-server
recycling; our enforced write path and read MCP are stdio MCP servers) and the /stop-signal +
provider-credential-corruption fix. Canaried on two deployments before the fleet roll.

### Changed
- **Carried patches 9 → 8.** Patch 09 (Serper backend recognition) DROPPED — v0.18.2's web-provider
  registry resolves any registered plugin natively, so the `plugins/web/serper/` overlay needs no
  patch. Patch 08 (`web.backend: rotate`) RESHAPED registry-based: plugin providers join the
  rotation automatically; the hardcoded backend list is gone. Patch 02 re-anchored (content
  identical). Full record: `docs/hermes-upgrades/v2026.7.7.2-v0.18.2.md`.

### Added
- **Baked Hermes-pin marker** (`.hermes_pin`) + `check_pins` validation/self-heal — the okengine#192
  second half, found live when the canary's About panel kept claiming the old Hermes after the roll.
- **`int` field-shape class** (base-schema `field_shapes`, pack-extensible): machine-computed counts
  (`recent_reports`, `total_mentions`) REJECT a hand-authored non-int at the enforced write path
  with the field named (digit-strings coerce). Live incident: an agent hand-set `recent_reports:`
  to a list of source paths.

### Fixed
- **Cockpit numeric sorts rank junk last** in both directions — one malformed count value was taking
  the #1 slot of the Most-active table (desc `reverse=True` flipped the numeric/junk key buckets).
- **Pre-release invariant audit — 7 critical/high + 6 folded mediums**, each with a red regression
  test and re-verified by targeted adversarial rounds (two rounds caught 5 further gaps, including a
  read/write ship-blocker, before the tag). Highlights:
  - **Enforced write path.** `_safe` now APPENDS `.md` instead of `with_suffix()` (a dotted slug like
    `openssl-3.0.7-advisory` was truncated to `openssl-3.0.md`, colliding distinct pages and
    dead-linking wikilinks) — fixed in BOTH the write path and its read-MCP twin (`server.py`), which
    had desynced. `converge` merge now runs the machine-owned `int` guard (a dedup-redirected
    `create_entity` bypassed it). `HEALTH.md`/`BUNDLE.md` and the full `INDEX-*`/dotfile family are
    write-refused in lockstep with the validator's conformance exemption (else forgeable-yet-invisible).
  - **Deployment validator.** `check_write_path_libs` no longer vacuously passes when a write-path lib
    (or base-schema) is present on only one of the baked/staged sides; a contract test pins
    `_WRITE_PATH_LIBS` to write_server's actual imports (and reclassifies `converge.py` as image-only,
    so the drift check stops silently skipping it). The api_server auth gate uses a toolset allowlist
    (a composite alias bypassed the old blocklist). The tick-lock post-deploy check measures lock AGE
    against container start (a fossil lock can never signal a dead scheduler by presence alone).
  - **Budget guard / cron dump.** Guard-pauses keyed on guard-owned ids (not job-side `paused_at`,
    which can't discriminate an operator pause), write-ahead intent + cumulative owned-set across retry
    ticks, atomic state; cron dump mints stable ids and un-pauses on truthiness.
  - **Backup / reader / cockpit.** `TarInfo.mtime` preserved (restores no longer date every file to
    1970); reader/cockpit sub-domain (walk-up) namespace resolution; cockpit open-prediction vocabulary
    honors `{open, active}` (pinned to `pred_lib.OPEN_VALUES`); `_shape_conflicts` guards malformed
    scalar `conflicts`/`values`/`sources` shapes (was a 500 in both surfaces).

## v0.10.9

Testing-depth release: the layer that catches **render/integration bugs on real data** — the class
that kept reaching users past clean-fixture unit tests. A seeded **render-surface smoke harness**, two
**vault-wide lints** (render + content) wired as nightly crons, a **write-time degeneration guard**,
plus the second-round invariant-audit mediums and a cockpit cold-load fix.

### Added
- **Render-surface smoke harness** (`tests/e2e/smoke/`, `make smoke-e2e`) — stands up reader + cockpit
  + mcp over a frozen seeded vault (no gateway/model) and asserts on the ACTUAL rendered HTML/PDF +
  rendered DOM (playwright): fact-panel-below-body, wikilink cleanliness, nested-dashboard visibility,
  deck PDF, multi-source embed resolution. A release gate (`docs/release-checklist.md` §2).
- **Vault-wide render lint** (`scripts/cron/render_lint.py`, `make render-lint`) — crawls every page
  through the reader and flags rendered-output defects (leaked builder markup, literal wikilinks,
  broken embeds) on stored content that clean fixtures pass.
- **Content-quality lint** (`scripts/cron/content_lint.py`, `make content-lint`) — flags degenerate
  generations (repetition-loop word-salad); comma/wikilink-aware and validated across five live
  vaults for zero false positives (a CJK-fusion signal was tried and dropped — it can't tell
  code-switching from legitimate Chinese CTI).
- Both lints **registered as nightly `no_agent` engine crons** (write `wiki/operational/{render,content}-lint.md`).
- **Write-time degeneration guard** — `write_server` soft-flags a repetition-loop word-salad
  `needs_review` at the enforced boundary, model-agnostic; mirrors the content lint (cross-surface
  contract test).

### Fixed
- **8 medium invariant-audit findings** (run-2): the `HERMES_CRON_MAX_PARALLEL` no-op under cron-plus,
  `kickstart.sh` exec-uid resolution, baked-vs-staged `base-schema` drift detection, Prometheus
  dead-fleet staleness alerting, the cockpit curated-"Other" nested-dashboard glob, `framework upgrade`
  rollback clobbering concurrently-modified pages, the mcp index-maintainer `last_seen` ordering, and
  `budget_guard` unknown-window fail-loud.
- **Cockpit overview cold-load** — the tab blocked ~8s re-scanning thousands of entity/source pages
  whenever the 120s dir cache expired; now stale-while-revalidate + a startup warm (sub-second).

## v0.10.8

Correctness sweep: a cockpit-rendering fix from an operator report, the lacuna wake-gate
rotation bug, and the **wiki-lane audit** — 100 analysis scripts triaged, 29 confirmed
defects fixed (all 7 HIGH) — plus the reshard/reshelve churn and the ingest duplicate-loop.

### Fixed
- **Cockpit assessments / predictions tables** (operator report) — the predictions scan recurses
  into the dated `predictions/YYYY/qN/` partition (Open-forecasts was silently empty); `table.ledger`
  headers and config-driven `_ds_cell` value cells no longer wrap or break mid-token ("RESOLVES BY",
  "moderate-high", "2026-07-07"); the A−/A+ text-size control scales the whole UI via `zoom` (content
  is px, not rem, so it previously grew only the toolbar). Baked (cockpit image).
- **Lacuna wake-gate rotation** — `_recently_analyzed` read every `[[concepts/…]]` link in a gap page
  instead of the authoritative `field_mapped` frontmatter, so the densest just-analyzed field clogged
  batch slot #1 forever (one gap, then near-zero output). Now keys off `field_mapped`; secondary
  citations no longer over-exclude, and undated pages fall back to file mtime. `frontier-watch`
  (whitespace) carried the identical bug against its `capability` field — fixed too.
- **Wiki-lane audit — 29 confirmed defects across the analysis lanes**, all 7 HIGH, fixed at the
  root in five recurring classes:
  - *Wrong/proxy signal:* prediction status predicates honor the canonical `active` (open) and
    `expired-ungraded` (terminal) — grading / regrade / forecast-review were silently skipping them
    (18 open forecasts on a live vault); `health_export` parses fleet-health's plain-text counts
    (were exporting 0 — a dead-monitor-reads-healthy fail); `daily-brief` reads the `last_updated`
    envelope; glossary counts aliased/anchored wikilinks; falsification uses genuine publication
    recency, not importer-churned `last_updated`.
  - *Sharding:* `source-staleness`, `apply-curated-fields`, `canonical-assemble`, `page-quality-enrich`
    route through the shard-aware `okf_migrate.find_page` / `canonical_key` / `<ns>/<slug>` keys.
  - *Composed schema / walk-up sub-domains:* `wiki-schema-audit` classifies against `merged_schema`
    + `type_aliases` (engine base types were reported as DRIFT); `page-quality-audit`, `tier-refresh`,
    `source-portfolio-watch` read the composed knowledge namespaces + walk-up sub-domain bases.
  - *cwd-vault:* `actor-risk-rank` resolves the vault from `WIKI_PATH`, never `os.getcwd()`.
- **reshard / reshelve churn** — `reshard_oversized` split an oversized leaf one level deeper, but the
  co-scheduled reshelve drain (`okf_migrate.build_map`) reverted it every 2h — the leaf-size invariant
  was never satisfied and each pass rewrote `[[…]]` links vault-wide. `okf_migrate.reshard_seg` is now
  the single shared reshard-key source; `build_map` treats a valid `<canonical>/<reshard-seg>/<slug>`
  bucket as canonical instead of reverting it.
- **Ingest no longer loops on duplicate raw files** — a duplicate raw file (its story already has a
  source) never gets its own page, so it stayed "unprocessed" and was re-offered in every batch: the
  lane drained nothing. An offer-count manifest skips a file after `RAW_STUCK_AFTER` fruitless offers,
  and the agent is guided to append a duplicate's path to the existing source's `raw:` list.

### Hardened (pre-release invariant audit — 23 confirmed cross-surface findings, all fixed + red-tested)
- **Enforced write path — base governance + generated-file exemption** (HIGH/MED/LOW). The write gate
  now applies engine-BASE governance (core-type `required` floors, CLOSED `tlp`/`source_kind`/`severity`
  enums) regardless of whether a composed-schema artifact exists — previously it read types/enums
  straight from the resolved schema, so base rules silently toggled with unrelated extension state. The
  conformance validator exempts the engine-generated structural files (`HOT`/`HEALTH`/`BUNDLE.md`, the
  INDEX tree, `_`-scaffold) that made a live vault permanently sub-100% conformant. `patch_entity` now
  applies the #196 list-shape coercion and both `patch_entity`/`append_to_section` enforce the briefing
  dead-link guard that `create`/`update` already did. The drift check's "always allowed" set now unions
  base `common_optional`, so it no longer flags the engine's own stamped provenance
  (`maintained_by`/`discovered_by`) as domain drift.
- **`install-domain` regenerates the composed-schema artifact** (HIGH) — a taxonomy merge edited
  `schema.yaml` but not `.okengine/composed-schema.yaml` (which the runtime write path prefers), so on
  an extension-enabled host every write to the newly co-installed namespace was silently rejected.
- **Cron sentinel expander tolerates all three documented schedule shapes** (HIGH) — a bare-string
  `schedule` crashed the whole cron deploy; a top-level `expr` sentinel silently never fired.
- **Deploy / rollback / ownership gates**: `framework upgrade` rollback no longer deletes live-vault
  writes made during the apply→validate window; `deploy-cron-scripts` stages the full documented
  `data/*` contract (not a 2-file allowlist); `post_deploy_verify` stats `jobs.json` itself for the
  okengine#193 ownership poison and probes qmd writability with a correct diagnostic; `cron-plus-logs`
  reads the in-container log dir and fails loud; the dead `deploy-cron-plus-plugin.sh` is removed (docs
  corrected to `install-cron-plus.sh`).
- **Fail-closed guards + consumer-shape robustness**: the budget guard fails closed on a malformed
  budget env and re-pauses after a jobs.json redeploy defeats the kill-switch; `deployment_validate`
  no longer false-FAILs a panels-only/schema-only extension; `id_index`/`select_entity_candidates`
  tolerate a non-string `aliases`/`tags` member; the reader/cockpit briefing/download views and embed
  resolver tolerate a non-string `title`/`name` (no 500) and memoize the embed lookup; the secret-scrub
  regex covers base64 tokens.

## v0.10.7

More shift-left + a UI fix from an operator report.

### Fixed
- **Dates never wrap in ledger tables** (operator report) — the "Open predictions" and other
  `.ledger` views (date last, long title first) broke a date mid-token (`2026-09-30`) when the wide
  first column squeezed the table; `.num` cells lacked `white-space:nowrap` and `_html_table` emitted
  plain `<td>`. Now `.num` cells never wrap (cockpit + reader) and `_html_table` tags a bare
  date/number/%/em-dash cell `.num`. Baked (cockpit + reader images).
- **build-engine-image fails loud on an unreadable manifest** (okengine#193 shift-left) — the old
  `${PIN:-v2026.6.19}` / `${RELEASE:-unknown}` fallbacks silently built against a stale pin / an
  "unknown" version; PIN/RELEASE now resolve env → manifest and stop the build if empty.

### Changed
- **Invariant-audit seeded with this session's bug classes** (okengine#193/#195/#196) — two new
  finder dimensions (silent-omission, consumer-shape-assumption) + sharpened mount-ownership /
  two-gate-agreement charters, so the pre-release sweep catches the next one of each class.

## v0.10.6

Shift-left hardening: turn this session's live failure modes into gates + a typed write path.

### Added
- **Schema-declared field shapes** (okengine#196, generalizes the v0.10.5 hotfix) — `field_shapes`
  in base-schema (pack-extensible) declares which fields are lists; the enforced write path coerces
  a scalar written for ANY declared list field to a list at the single chokepoint, driven off the
  composed schema (base ∪ pack) — no more hardcoded field set, no more per-field crash.

### Changed
- **deployment-validate runs DAILY, not weekly** (`10 12 * * 1` → `10 12 * * *`) — a weekly cadence
  let a contract violation (a version desync, a mis-owned jobs.json) sit stale in fleet health for
  up to a week.

### Fixed
- **Mis-owned cron-plus/jobs.json is caught** (okengine#193) — `check_runtime_ownership` now stats
  the jobs.json FILE, not just the runtime dirs: the fleet-stall poison was a root-owned jobs.json
  INSIDE a correctly-owned cron-plus dir, so the dir check passed while the scheduler went dark. A
  mis-owned jobs.json now FAILs with the #193 diagnostic.

## v0.10.5

### Fixed
- **Write path coerces a scalar list-field to a list** (okengine#196) — a list-valued frontmatter
  field authored as a bare string (e.g. `aliases: A, B`) sailed through the open/untyped schema
  unchanged and crashed a list-consuming lane: `normalize-bare-name-links` died with
  `TypeError: list + str` and took the whole lane red (live on a CTI vault whose compile agent wrote
  a scalar `aliases` on three entity pages). `write_server._normalize_refs` now coerces a scalar
  string → list for the known list fields (`aliases`, `tags`, `maintained_by`, `discovered_by`) at
  the single enforced-write chokepoint, so no such page can enter the vault; `normalize_bare_name_links`
  also defends against a scalar before use. Red tests on both surfaces.

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
