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

## v0.15.19

PATCH. Public CI and documentation only. No runtime, image or pack change. Deployment scope:
**none**; a host on v0.15.18 runs the same engine code.

### Fixed
- **Public coverage run measured files outside the checkout** (okengine#1067). The public
  workflow's Python 3.12 job read `pyproject.toml`'s relative coverage sources, so tests that run a
  temporary copy of the tree, and an installed package named like a source directory, were
  measured too: 191,799 statements at 33.64%, against the 100% floor. The job now writes an
  absolute coverage configuration with `ci/coverage_rcfile.py` and measures the checkout only.
  Measured that way the public tree stood at 99.74%: `ci/mutation_dispositions.py` shipped while
  its only test was excluded from the snapshot. It is now excluded with its test, and the timeout
  wrapper's stdout sentinel has a public test. The snapshot build now enforces public CI's coverage
  floors on the assembled public tree, so this class fails before publication.
- **Docs still described IWE as the backlink engine** (okengine#1066). IWE was retired in
  v0.14.0 (okengine#651); backlinks come from the engine's own forward-reference scanner and
  `wiki/.backlinks.json`. Ten documents are corrected, and a test fails any new IWE reference
  outside an allowlisted historical note.

## v0.15.18

PATCH. Fixes a defect in v0.15.17's new `scheduler-dst` deployment check, which blocked the
v0.15.17 roll at its first host. Ships everything in v0.15.17 (the cron-plus DST fall-back fold
policy). Deployment scope: **this release, before 2026-11-01**, in place of v0.15.17. No image
change; the gateway must be recreated, which a `--release-digests` roll does (see v0.15.17).

### Fixed
- **Root-owned bytecode in the cron-plus plugin tree** (okengine#1064). Post-deploy verify runs
  the `scheduler-dst` check as root in the gateway. Since v0.15.17 the check imports the deployed
  plugin to judge its fall-back policy, and it wrote `__pycache__/*.pyc` owned by root into
  `/opt/data/plugins/cron-plus`, so the same verify FAILed its ownership check on every run.
  `croniter_dst_selftest.py` now imports and runs the plugin with bytecode writing off, and the
  check also passes `-B`.

### Upgrade notes
- Deploy as for v0.15.17. A host that already ran v0.15.17's verify has root-owned files under
  `.hermes-data/plugins/cron-plus/__pycache__/`; remove them before deploying
  (`docker exec -u root <gateway> rm -rf /opt/data/plugins/cron-plus/__pycache__`), or the
  ownership check stays FAIL.

## v0.15.17

PATCH. Fixes how the cron-plus scheduler handles the repeated hour when clocks fall back
(2026-11-01 in America/New_York). Deployment scope: **this release, before 2026-11-01.**
No image change: `deploy.sh` reapplies the carried cron-plus patches. The scheduler runs inside
the gateway process and loads them only at start, so **the gateway must be recreated** after the
patches land. A `--release-digests` roll does this (the per-release `.tested` gateway tag
changes); a host-built deploy must `--force-recreate` the gateway.

### Fixed
- **DST fall-back fold** (okengine#1055, okengine#1061 [4]). The pinned cron-plus guard compared
  croniter's next slot with the claim time. A daily `30 1 * * *` lane claimed a minute late fired
  again at 01:30 EST, and every hourly fixed-minute lane (`usage-rollup`, `12 * * * *`) lost its
  01:MM EST run. The new carried patch `patches/cron-plus/dst-fold.patch` (overlay
  `dst_fold.py`) decides from the slot: an explicit hour fires once in the repeated hour, an hour
  field starting with `*` fires in both passes (Vixie cron / cronie semantics).

### Added
- `croniter_dst_selftest.py --cron-plus DIR` runs the installed plugin's own `compute_next_run` on
  the repeated hour. The `scheduler-dst` deployment check (post-deploy verify, daily validator)
  passes the gateway's plugin, so it judges the deployed scheduler, not only croniter.

### Upgrade notes
- Deploy as for v0.15.16: `deploy.sh --release-digests <list>` per host from a clone of the tag.
  `install-cron-plus.sh` applies `dst-fold.patch` at step 2.
- Post-deploy verify `[scheduler-dst]` must report `cron-plus compute_next_run passes 3 fall-back
  case(s)`. That check loads the plugin files from disk, so also confirm the gateway started after
  them: `docker inspect -f '{{.State.StartedAt}}' <gateway>` must be newer than
  `.hermes-data/plugins/cron-plus/dst_fold.py` (okengine#1062 tracks a standing check).

## v0.15.16

PATCH. Hardens the CI-tested-digest deploy introduced in v0.15.15 and adds detectors for the
v0.15.15 invariant-audit candidates (okengine#1057). `deploy.sh --release-digests` now checks that
every image it needs can be fetched, by its pinned digest, before it writes any pin, and it resolves
the registry host itself instead of relying on the host's DNS failover (okengine#1058). Deployment
scope: **next release.** Deploy tooling only (`scripts/deploy.sh`, `scripts/release_load.py`); no
baked runtime code changed.

### Upgrade notes
- Deploy as for v0.15.15: fetch the release's `tested-digests.json` and run
  `deploy.sh --release-digests <list>` per host from a clone of the tag (docs/release-digests.md).
- The tested-digest list is now kept at `<pack>/.okengine/generated/tested-digests.json`, written
  only after a verified load; `OKENGINE_TESTED_DIGESTS` points there, so deleting the downloaded
  copy no longer breaks `post_deploy_verify.sh`.
- The gateway's tested tag is now scoped like the engine images
  (`hermes-agent:okengine-<release>-<sha>.<project>.tested`), so two packs on one host no longer
  share it.

### Fixes
- `release_load` preflight: every required image that is not already local is checked with
  `crane digest` against its pinned digest before `.env` or the image override is touched; the
  registry host is resolved once and passed to the loader with `--add-host` (okengine#1058, !1364).
- An engine image with no CI release service mapping is a named failure instead of a `KeyError`;
  the six hand-maintained engine-image tables are held to one agreement test (okengine#1057 [5],
  [9], [10], !1364).
- The durable tested-digest list and the pack-scoped gateway tag (okengine#1057 [6], [11], !1364).

### CI and tooling (no runtime change)
- The release lanes rerun when anything under `okengine-operations/` changes, not only its
  Dockerfile; the input detector also reads the release-build compose file (okengine#1057 [4],
  !1362).
- New detectors: rebound write-service code must resolve every global in the `write_server`
  facade (okengine#1057 [3]); the gateway release lane's `changes:` list must cover the image
  lane's (okengine#1057 [8]) (!1363).
- The prepull tests no longer reach the real resolver (okengine#1060, !1363).
- Test-policy collection reads each function's source once: local collection 32 s → 11.8 s, CI
  well under its 120 s deadline (okengine#1059, !1361).
- A backup test waits out the ctime tick of every file it touches, ending a nightly flake (!1360).
- v0.15.16 invariant-audit candidates (all low, none release-blocking) are tracked in okengine#1061.

## v0.15.15

PATCH. A security fix for the MCP servers, an authentication fix, and the first release the fleet
deploys from the images CI tested (okengine#1048). The pinned `mcp` 1.28.1 never reclaimed stateful
Streamable HTTP sessions, so any client that could reach the read or write MCP endpoint could grow
its memory without bound (CVE-2026-59951); `mcp` 1.30.0 expires sessions idle for 30 minutes, caps
them at 10,000 and limits request bodies to 4 MiB. An `Authorization` header that is not valid
Latin-1 or base64 now gets a 401 instead of a 500 (okengine#966). Deployment scope: **fleet
required.** **Baked: the okengine-mcp, review-write and operations images (`mcp` pin) and the
write path (`okengine-mcp/`, okengine#966).**

### Upgrade notes
- **Deploy the tested images:** fetch the release's `tested-digests.json` from the package
  registry and run `deploy.sh --release-digests <list>` per host from a clone of the tag
  (docs/release-digests.md). Nothing is built on the host; `post_deploy_verify.sh` must PASS
  "every running engine image is one CI tested". Each deploy host needs the read-registry
  credential file (`~/.config/okengine/registry.env`, mode 0600).
- **Rollback** to v0.15.14 is a host-built deploy from a v0.15.14 clone: that release has no
  tested-digest list. Later releases roll back by digest.
- An MCP HTTP client that drops its stream and stays idle for 30 minutes now gets a 404 and must
  initialize a new session.

### Fixes
- `mcp` 1.30.0 for CVE-2026-59951; the CI tooling images were refreshed with it (!1358).
- Undecodable `Authorization` headers get a 401 on the read and write MCP servers (okengine#966,
  !1226).
- `deploy.sh --release-digests` (v0.15.15 invariant audit, !1359): `release_load` no longer deletes
  an image it just loaded by removing crane's placeholder name, its only tag, before the deployment's
  tags hold it; and a failed load restores `.env` and `docker-compose.okengine-image.yml` instead of
  leaving them pinned to images that were never loaded.

### CI and tooling (no runtime change)
- okengine#1048 phase 3: CI builds, boots and records every release image, including the gateway;
  a version tag promotes the tested digests; `deploy.sh --release-digests` deploys them (!1342,
  !1343, !1344).
- okengine#1047: the critical-mutation nightly is green again (mutation-history order, an actor
  grounding survivor, the HTTP client timeout and carried floor waivers: !1354, !1355, !1356).
- Five full-suite shards; heavy work stays on the CI nodes; the runner audit fails a private CI server
  runner that carries heavy or light (okengine#1052, !1348, !1352).
- Tests that read publish-excluded files are caught on the MR (okengine#1054, !1351); the registry
  canary checks the Chrome download hosts (!1353); the security layer's deadlines match its measured
  runtime (!1357).
- v0.15.15 invariant-audit candidates not fixed here are tracked in okengine#1057.

## v0.15.14

PATCH. The v0.15.13 invariant-audit follow-ups (okengine#1044), the v0.15.14 audit fixes, a
gateway scheduler fix for daylight saving time, and CI work that changes no runtime behavior.
The reader no longer stalls on link-heavy pages: dangling and ambiguous wikilinks resolve through
one shared basename index instead of a full vault walk per link (32-73 s renders measured on live
vaults, now under 0.5 s). The gateway image carries croniter 6.2.4, so cron-plus computes the
next fire correctly across DST; with the croniter 6.0.0 that Hermes's lock installs, daily lanes
would have fired an hour late on 2026-11-01 and twice on 2027-03-14. Deployment scope: **fleet
required, full `deploy.sh` per host.** **Baked: rebuild the gateway image (croniter pin, engine
wheel); `deploy.sh` rebuilds the reader and cockpit images (`vault_lookup_cache.py`, reader
rendering and backlinks).**

### Upgrade notes
- **Rebuild the gateway image first** (`build-engine-image.sh` with `OKENGINE_TAG`), then run a
  **full `deploy.sh` per host**. The image build runs a DST self-test and refuses to produce an
  image whose scheduler computes DST wrong.
- **New check `scheduler-dst`** (`deployment_checks`): runs `scripts/cron/croniter_dst_selftest.py`
  under the gateway's own interpreter. Wrong DST math or a missing croniter FAILs; a self-test
  that cannot run is UNDETECTABLE, never a pass.
- **Deploy refuses a host-port conflict** before recreating anything (okengine#1044 [25]), now
  from any working directory.
- **Sidecar wrappers** use the deployment's own compose project and the per-pack read MCP URL
  (okengine#1044 [5]); regenerate them with `framework extensions sidecar-generate`.
- **Restore** reports directory modes it could not apply (including pre-v0.15.13 archives) and
  still rebinds the deployment identity (okengine#1044 [19]/[22]/[23]).

### Fixed
- Reader re-resolution: one basename index shared by a render, stamped when its walk finishes so
  a slow disk cannot turn it back into one walk per link (okengine#1044 [24], v0.15.14 audit).
- Gateway scheduler: croniter 6.2.4 pinned engine-side (Hermes unmodified), recorded in
  `engine-manifest.yaml` `runtime.dependency_overrides`.
- HOT.md and `tier_of` agree on the open set for a pack's own `hot_set` (okengine#1044 [7]/[12]);
  a `hot_set` without sections, a section without `kind`, a non-mapping section and a scalar
  `open_values` no longer crash the lane (v0.15.14 audit).
- `deploy.sh` reads the `.env` uid pin with compose semantics; `check_crons` judges ownership by
  the gateway's `HERMES_UID`, not the deployer's uid (okengine#1044 [1]/[9] and W1).
- Lane `write_tools` declarations are validated; sidecar op names cannot collide with extension
  names (okengine#1044 W2).
- Migration `TO` versions are pinned to the release that shipped them (okengine#1044 [4]).

### CI (no runtime change)
- Jobs start on real `needs:` with a final fan-in gate (okengine#1045).
- Mutation: bare-name exception lint (okengine#1046), main campaigns diff from the last green
  campaign (policy B), automatic diff-scoped mutation on small MRs, a `mutation-dispositions`
  job, and an unblocked nightly critical campaign (okengine#1047).
- CI records the digests of the images it tested; post-deploy verify checks running images
  against that list when one is present (okengine#1048 phases 1-2).
- Unit CPU budgets no longer bill an automatic garbage collection to the running test
  (okengine#1051).

## v0.15.13

PATCH. The v0.15.12 invariant-audit follow-ups (okengine#1042), the backup follow-ups
(okengine#1041) and host-side verify import pinning (okengine#1043). Backup `prune`/`list` no
longer match another pack's archives, a failed SQLite online backup fails closed, and restore
publishes the job store under its lock; the write-path corpus libraries order and cap HOT.md
deterministically, split legacy scalar aliases the way the write path does, re-resolve cached
lookups and date the ledger in the deployment zone; every engine image probes all wheel runtime
members and deploy refuses a compose project another directory owns; generated lane writers are
re-derived from their lane and orphaned extension tokens are revoked; prerelease versions order
before their final; and `post_deploy_verify.sh` step [9] judges with the checkout being deployed.
Deployment scope: **fleet required, full `deploy.sh` per host** (not stage-only). **Baked: rebuild
the gateway image (`okengine-mcp/converge.py`, `write_server.py`, `id_index.py`, the engine
wheel's write services and identity guard); `deploy.sh` rebuilds the reader, cockpit,
review-write and projection images.**

### Upgrade notes
- **Rebuild the gateway image first** (`build-engine-image.sh` with `OKENGINE_TAG`), then run a
  **full `deploy.sh` per host**. A stage-only deploy leaves `id_index.py` drifted and
  `check_write_path_libs` FAILs.
- **Every `okengine-write*` server gains `TZ`** from the deployment's zone when ensure-runtime
  reconciles `config.yaml`; the gateway recreate in `deploy.sh` applies it (okengine#1042 [36]).
- **Generated lane writers are re-derived.** ensure-runtime rewrites a generated writer's
  `OKENGINE_OUTPUT_CONTRACT_MODE` and `OKENGINE_WRITE_TOOLS` when they disagree with its lane,
  and leaves every other line alone (okengine#1042 [3]/[18]).
- **`deploy.sh` refuses a compose project another directory owns** (`engine_images.py claim`);
  docker unavailable is a WARN (okengine#1042 [8]).
- **Engine image drift** reports SHARED for an engine image not on its deployment-scoped tag, and
  post-deploy verify WARNs UNATTRIBUTED on an image built without the provenance label until it is
  rebuilt (okengine#1042 [12], [14]).
- **`patches/apply.sh` enforces the pinned Hermes SHA** and refuses a directory that is not the
  top of the Hermes tree (okengine#1042 [34], [35]).
- **`framework upgrade --apply` exits 3 when validation fails** after the upgrade, and passes
  `--skip-validate` through (okengine#1042 [28]).
- **Backup:** archives are matched by their exact `<pack>-<stamp>.tar.gz` name; `restore` re-creates
  directory modes and publishes `jobs.json` atomically under `jobs.json.lock`. Stop the gateway
  before `restore --force` as before (okengine#1041, okengine#1042 [0] [6] [7] [29]).
- **`framework extensions forget <pack> <id>`** removes a stale `disabled:` entry; deploy revokes
  tokens of extensions that no longer exist (okengine#1042 [9]/[20], [32]).

### Fixed
- Backup prune/list prefix collision, fail-closed SQLite snapshot, locked job-store restore,
  directory modes, cap allocation to settle and in-place writers, direct-invocation import path
  (!1325; okengine#1041, okengine#1042 [0] [6] [7] [29]).
- `post_deploy_verify.sh` step [9] runs from `scripts/post_deploy_selfchecks.py` with imports
  pinned to the deployed checkout; a crashed step is a FAIL; drift peer 3b reports both-sides
  unreadable; remediation hints name the check (!1324; okengine#1043, okengine#1042 [15] [16] [21] [22]).
- Image provenance: every wheel runtime member and copied file probed in all six images,
  compose-project ownership claim, shared-tag drift, provenance labels, unconfigured running
  services, Dockerfile map completeness (!1323; okengine#1042 [2]/[4] [8] [12] [13]/[31] [14] [24] [25]).
- Lane writers re-derived, ownership gate real-invocation tested, orphan tokens revoked,
  `extensions forget`, multi-operation sidecars get their extension's token (!1327; okengine#1042
  [3]/[18] [9]/[20] [23] [32] [33]).
- Prerelease-aware version ordering on the pack-migration path and pins, active patch series in
  release evidence, nested Hermes tree and pinned SHA in `apply.sh`, upgrade validation exit
  (!1322; okengine#1042 [5]/[27] [10] [28] [34] [35]).
- HOT.md cap order and truncation note, open values from the composed tier block, alias
  splitting, wrapped cockpit titles, lookup-cache re-resolution, writer ledger timezone, registry
  census step (!1326, baked; okengine#1042 [11] [17] [19] [26] [30]/[38] [36] [37]).
- CI: 120s/180s timeouts for real-invocation and repo-walking tests, measured durations for the
  shard planner, `.suite-run` 50m (!1327, !1328).
- v0.15.13 invariant audit: the cockpit ref-title reader falls back to the title line when
  frontmatter parses but cannot be constructed (an impossible date raised ValueError and made the
  assessments tab 500), and `post_deploy_verify.sh` grades a step [9] driver failure as a FAIL,
  not a WARN. The remaining audit candidates are tracked as follow-ups.

## v0.15.12

PATCH. Each deployment renders lane writers from its own fleet and prunes foreign ones
(okengine#1029); compose-built engine images get immutable, deployment-scoped tags with a drift
check (okengine#1033); the shared-word actor alias tie no longer accepts organization or software
names, and `framework validate` FAILs a type owner that no lane can act for (okengine#1030);
reader and cockpit lookup memos revalidate (okengine#1024); release lanes run on every baked
source (okengine#1031); cron scripts reject unknown arguments (okengine#1034); and the v0.15.11
invariant-audit follow-ups land, including a short backup fence and gated extension identity
grants (okengine#1032). Deployment scope: **fleet required, full `deploy.sh` per host** (not
stage-only). **Baked: rebuild the gateway image; `deploy.sh` rebuilds the reader, cockpit,
review-write and projection images.**

### Upgrade notes
- **Rebuild the gateway image first** (`build-engine-image.sh` with `OKENGINE_TAG`): the engine
  wheel's `okengine.actor_grounding` (#1030) and `tools/policy_plane.py` (#1032 [6]) are on the
  write path. Then run a **full `deploy.sh` per host**, never a stage-only deploy.
- **A stage-only `deploy-cron-plus-jobs.sh` FAILs until the host has run ensure-runtime.**
  ensure-runtime now renders `okengine-write-<lane>` servers from the deployment's own fleet
  (`scripts/lane_writers.py`) and removes engine-generated writers for lanes the deployment does
  not run. The jobs deploy refuses a `config.yaml` that still registers such a stale foreign
  writer ("write server(s) for lanes this deployment does not run"). `deploy.sh` runs
  ensure-runtime first and force-recreates the gateway, so a normal deploy passes.
- **Every compose-built engine container is recreated once.** `deploy.sh` pins the reader, mcp,
  review-write, operation-runner, cockpit and projection services to
  `<repo>:okengine-<release>-<sha>[-dirty].<compose project>` (`OKENGINE_ENGINE_IMAGE_TAG` in
  `.env`, applied through `docker-compose.okengine-image.yml` for the services the deployment
  defines). A pack's own `image: okengine-reader` lines need no edit. Old per-release tags
  accumulate; prune `okengine-*:okengine-<old release>-*` periodically.
- **`deploy.sh` fails closed** (exit 1, before bring-up) when a `COMPOSE_FILE` entry does not exist,
  when the engine revision cannot be resolved (`git rev-parse` fails), or when the tag cannot be
  derived. A tag is never built from an `unknown` sha.
- **`framework validate` now FAILs** a `types.<t>.owner` that is not the `pack.yaml` name, a
  co-installed pack, `engine` or `ext:<id>`, and an unknown `types.<t>.role` (it was a WARN), on
  pack types and on extension-owned types. A pack whose types still name a former bundle or a
  folded pack as owner must fix that before it rolls.
- **Bundle pulls keep the host's `name`.** `_apply_bundle_identity` re-stamps only `description`
  and `mission`, so the composed name stays the write principal and the declared owner.
- **`deploy.sh` reconciles the extension token store** (`framework extensions reconcile-tokens`)
  from the current manifests. A stale record is rewritten in place with a WARN, without token
  rotation; a discovery failure aborts the deploy. Stores that lag their manifests are brought
  back to the declared scopes.
- **Pack- and operator-tier extension `identity_edit` grants need acceptance.** Run
  `framework extensions enable <pack> <id> --accept-identity-edit`. The acceptance is bound to a
  digest of the extension id and its whole `write_policy`; widening it needs a new acceptance.
  Without one, the token drops the grant and identity changes are held for review. First-party
  `okengine.*` extensions keep their declared grant.
- **Post-deploy verification runs more checks.** Step [9] runs every registered check except
  the in-container ones, so `reserved-basenames` (FAIL on a reserved-basename page with content),
  `source-identity` and `tool-search` now run at deploy time. It also WARNs when a running
  container's image ID differs from what its compose image reference resolves to now.
- **Backups hold the corpus fence briefly.** See Backup and restore below. A restore prints a
  warning naming `consistency.rewritten_after_fence` when the manifest lists files that kept
  changing after the fence.

### Deploy and runtime (host-side)
- **Lane writers from the deployment's own fleet (okengine#1029, !1312).** `lane_writers.py`
  builds the lane set in memory with `cron_pack_split.build_fleet(pack)` plus the `cron:`
  capabilities of `.okengine/effective-policy.json`, never from the shared
  `config/cron-plus-jobs.json`, so it also works on a fresh deploy. A writer is pruned only when
  its name is the one the engine derives from its `cron:` actor and its `command` and `args` are
  the engine's write server; `okengine-write`, `okengine-write-chat`,
  `okengine-write-source-quality` and operator-added servers are never touched. Nothing is pruned
  without a known lane set or from a `config.yaml` that does not parse. `--dry-run` prints the
  plan. A broken fleet now fails ensure-runtime loudly. `deploy.sh` no longer regenerates the
  shared split for ensure-runtime.
- **Deployment-scoped engine image tags (okengine#1033, !1317, !1319).** `scripts/engine_images.py`
  derives the tag, writes the override and runs the drift check (unreadable docker state is
  undetectable, never a pass). `image_tag()` refuses any sha that is not 4 to 40 lowercase hex
  characters, optionally ending in `-dirty`.
- **Reader and cockpit image provenance probes every copied file (#1032 [16], !1317).**
  `image_drift.py` gains a `source_glob` probe; the reader and cockpit entries probe `*.py`,
  `static/` and `requirements.txt`.
- **Partial gateway overlay Dockerfiles removed (#1032 [18], !1317).** They copied write-path code
  to paths nothing imports. Hotfixes go through a patch release and `build-engine-image.sh`.
- **Extension token store (#1032 [12] [17], !1318).** New
  `framework extensions reconcile-tokens <pack> [--check]`; `--check` reports drift and exits 1. A
  record for an extension that is not enabled is reported, never dropped.
- **`framework upgrade` orders prereleases (#1032 [25], !1313).** `release_key()` orders
  dev < a < b < rc < final, so a migration added between an rc and its final is selected.
- **`check_provenance_env` follows `pack.yaml` (#1032 [20], !1313).** It WARNs when `pack.yaml`
  declares no `name`, and when a gateway `OKENGINE_PACK` disagrees with it.

### Write path (baked)
- **Shared-word alias tie (okengine#1030, !1316).** New `actor_grounding.extends_name`: a shared
  distinctive word ties a new name to a kept one only when every other word it adds is a kind word
  (group, APT, team, gang, ransomware, RaaS, ...), a numeral or a tracker designator. Names that
  add an organization or software word ("... Labs", "... Bank", "... Stealer", "Cobalt Strike")
  must ground on their own or be glossed by a cited source. A legacy scalar `aliases` or `sources`
  value splits like the list it stands for (#1032 [23]).
- **Granted extension writes pass policy validation (#1032 [6], !1318).**
  `policy_plane.validate_capability(..., caller=True)` accepts a well-formed `identity_edit` on a
  caller capability; it rejected every write by a granted extension before. A policy document
  still may not carry a grant.

### Reader and cockpit (baked)
- **Revalidating lookup memos (okengine#1024, !1315).** The reader's embed and link-title memos and
  the cockpit's embed and `_ref_title` memos use `okengine.vault_lookup_cache.VaultLookupCache`:
  a hit is served only while its source file's fingerprint is unchanged, a miss expires after
  60 s, and each memo is LRU-bounded at 4096 entries. A retitled, moved or newly created page
  shows without a container recreate. The cockpit's `_ref_title` refuses paths outside the wiki.
- **Per-page governing actor schema in the cockpit (#1032 [10] [19], !1315).** A type declared
  `role: actor` only in a walk-up sub-domain's schema is quarantined like an actor on that
  sub-domain's pages, and does not leak to root pages.

### Backup and restore
- **Short backup fence (#1032 [0], !1314).** `framework backup create` holds `stable_corpus` only
  to walk the corpus and hardlink it into `.okengine/backups/.staging/`; the `.hermes-data` walk,
  gzip, hashing, SQLite online backups and runtime files run after the fence is released. Known
  in-place writers (`log.md`, `*.jsonl`, `.okengine/*.json`), files written within the settle
  window and recent hot files (64 MB budget) are copied on the first attempt. A file rewritten in
  place after the fence triggers a rebuild that copies only the changed files; forced copies under
  one fence are capped at 2,000 files and 128 MB. After 3 attempts the archive is kept and the
  still-changing files are listed in `consistency.rewritten_after_fence`. Without hardlinks, files
  are read after the fence and rechecked (`corpus_snapshot: read-after-fence`). Stale staging
  directories are swept under a lock.
- **Restore re-arms cron-plus privately and under its lock (#1032 [26], !1314).** `jobs.json` keeps
  its mode and owner and is rewritten under `jobs.json.lock`.
- **Projector reconciles a restored vault (#1032 [9], !1314, baked: projection image).** A corpus
  epoch below the checkpoint forces a full reconcile instead of an empty delta. Epoch reuse after
  a restore that advances past the checkpoint is follow-up okengine#1035.
- `framework_deployment.discover_packs` and `framework upgrade` snapshots ignore pack copies under
  `.okengine/backups` and `.okengine/snapshots` (!1314).

### Detection and review
- **Cron scripts reject unknown arguments (okengine#1034, !1313).** `corpus_audit.py` parses argv
  before touching the vault; `--help` exits 0, an unknown argument exits 2, and `--report-only`
  (alias `--dry-run`) prints the metrics without writing the dashboard. A subprocess detector runs
  every converted `scripts/cron/*.py` with an unknown argument; the 53 not yet converted are on a
  shrink-only allowlist.
- `registry_coverage` uses `review_autoverify.refusal()`, the lane's own refusal ladder, so held
  actor pages are counted, not ranked (#1032 [21], !1313).

### CI and tests
- **Release lanes run on every baked release-image source (okengine#1031, !1309).** The MR
  `changes:` list covers `src/`, `okengine-mcp/`, `okengine-reader/`, `okengine-cockpit/`, the
  smoke fixtures and the wheel's members, derived and checked by a test with a negative fixture.
- **Smoke images are scoped (okengine#1033, !1310).** The smoke stack builds
  `okengine-smoke-<svc>:${SMOKE_RUN_ID:-local}`, so a local build never moves a deployment's tag.
- `hermes-target-contracts` selects on every input it consumes (#1032 [13], !1313).
- Full-suite shards are rebalanced from CI-measured durations and the shard timeout is 48 minutes
  (!1308); the shard budget checks weigh an unrecorded test file like the planner (!1311).
- The documented carried-patch count is derived from the manifest (#1032 [28], !1313).

### Issues
- Fixed and closed: okengine#1024, okengine#1029, okengine#1030, okengine#1031, okengine#1032,
  okengine#1033, okengine#1034.
- Follow-ups: okengine#1035 (projection epoch reuse after a restore), okengine#1025 (operator-tier
  extension trust acceptance).

## v0.15.11

PATCH. Write servers act for their pack, identity changes on existing pages are held for review,
actor guards follow the composed schema's declared actor types, and actor renames are judged
(okengine#1014, okengine#1020, okengine#1026, okengine#1027, okengine#1028). The reader's page
revision token is restored for settled pages (okengine#1023). Deployment scope: **fleet
required**. **Baked: rebuild the gateway, reader, cockpit and review-write images.**

### Upgrade notes
- **Rebuild the gateway, reader, cockpit and review-write images first**, then run `deploy.sh`
  per host. The staged crons that import the new `okengine.*` modules (`corpus_audit`,
  `importer_guard`, `review_autoverify`, `active_view_lib`) roll together with the image, never
  ahead of it. `scripts/cron/schema_lib.py` is baked too; a stage-only deploy leaves the write path
  on the old library.
- **Run `ensure-runtime` and recreate the gateway**, so every write server receives
  `OKENGINE_PACK` in its `env:` map.
- **Regenerate the cron fleet.** Output-contract digests changed.
- **A deployment whose `pack.yaml` has no `name` refuses to start its write servers.** Give a
  nameless `pack.yaml` a literal `name` before upgrading. `framework validate` now FAILs a
  `pack.yaml` with no literal (or a blank) `name`, and `deploy.sh` refuses before it recreates the
  gateway when a write server would lack a principal (new step 2b, below).
- **Packs that keep actors under other type names must declare `role: actor`** on those types
  (`types.<t>.role: actor`). Until they do, the actor guards skip those types and the audit reports
  them as undeclared.

### Write path (baked)
- **Write servers act for their pack (okengine#1027, !1299).** Hermes starts stdio MCP servers
  with a filtered environment, so `OKENGINE_PACK` never reached a write server and every agent
  lane wrote with no pack principal. The new `okengine.pack_principal` takes the deployment pack
  from the `name` in `pack.yaml` (`OKENGINE_PACK` only where `pack.yaml` declares none; a
  disagreement is refused). A server-bound cron lane of a co-installed or composed pack acts for
  that pack; a pack the deployment does not hold never becomes a principal. `write_server.py`
  refuses to start without exactly one principal, and `ensure-runtime.sh` renders `OKENGINE_PACK`
  into every `okengine-write*` `env:` map.
- **Identity changes are held for review (okengine#1028, !1299).** On an existing page, a changed
  `type`, `title` or `name`, a dropped alias, or a removed or emptied curated field is held unless
  the caller holds an `identity_edit` grant: the stored value is kept, the change is queued for
  review, and the rest of the write applies. The guard (`okengine.identity_guard`) runs on update,
  patch and converge, including a dedup-redirected create. Grants live in a lane's output
  contract; a lane that creates pages can never hold one, a pack may only narrow an engine lane's
  grant, and malformed grants grant nothing. The operator principal holds every grant.
- **Citation lists are merged, never dropped (!1299).** Converge unions `sources` and `basis` for
  every caller; a dropped citation in update or patch is a curated removal held for review. A
  lane may clear a relationship value of its own contract when every stored target is dangling
  (a wiki path whose stem no page in the vault carries).
- **The composed `pack` owner placeholder resolves to a declared owner (okengine#1026, !1298).**
  Since v0.14.0 every pack was a non-owner of its own types, so `remove=` and every changed field
  on its pages were conflicts. `type_owner` now resolves a `pack` entry to the type's declared
  `types.<t>.owner`; without one it stays `pack`, so nothing is loosened.
- **`title` and `name` are one identity (!1305).** A page's effective name is what the identity
  index keys it by: `name`, else `title`, else its stem. Adding the display key a page lacks, with a
  value that differs from that effective name, is a held rename: the addition is dropped and the
  change is queued for review. Naming a page what it is already called still applies. A held
  field's key-alias spellings are dropped with it, so an alias cannot bring back a held display
  key. Baked: `okengine.identity_guard` and `write_services/review.py`.
- **Actor types come from the composed schema (okengine#1020, !1296).** Every actor guard,
  detector and panel tested the literal `type == "actor"`, so a pack that keeps actors under other
  type names got no grounding and a vacuous audit count of 0. `okengine.actor_types` resolves
  `actor`, every type that declares `role: actor`, and their `type_aliases`. A pack-declared actor
  type is judged on create and on a retype into it, never on stored pages. `framework validate`
  WARNs on an unknown `role`. Actor names fall back to aliases, then the slug.
- **Actor renames are judged (okengine#1014, !1297).** A write that gives a stored actor a new
  title, name or alias must ground that name like a create, unless a cited source ties it to a
  kept name (a vendor gloss, an alias cue, a slash or a shared distinctive word). Respellings and
  dropped names are not renames. A rename of an unsourced actor onto a name the deployment already
  holds as a non-actor is refused. The grounding predicate also refuses absence-of-evidence
  sentences, a third party's claims, software named as the subject when a source defines it as
  software, and a pack's geopolitical titles.

### Detection and review
- `corpus_audit` reads **undetectable**, never 0, when no page carries a declared actor type, and
  WARNs on a type that the corpus's own actor references point at but that is not declared
  (`actor_types_undeclared`) (!1296, !1297).
- `review_autoverify` holds an actor page whose cited sources resolve but do not ground it (!1297).
- **Declared actor types always publish (!1304).** Without an explicit list, every declared actor
  type (`role: actor`, its aliases, any capitalization) gets the #554 always-publish default in
  `review_autoverify`, as `actor` does. A pack's explicit `review_autoverify.always_publish_types`
  is still taken as written, with a WARN for each declared actor type it leaves out. Each page is
  judged against its own governing (sub-domain) schema for actor detection, the grounding hold,
  required fields and always-publish. `registry_coverage` uses the same rule. Staged only.
- New `write-principal` deployment check in the daily validator, `framework doctor` and
  post-deploy verify (!1299).

### Deploy gates
- **`deploy.sh` step 2b (!1305).** After `ensure-runtime` renders the writers and before any
  `compose up` or recreate, `deploy.sh` runs `scripts/check_write_principal.py`. It cannot be
  skipped (`--skip-validate` skips judgment, not an outage), and a WARN also stops the deploy.
- **The baked-engine preflight is derived, not hand-listed (!1305).** `scripts/lib/baked_imports.py`
  scans every `okengine.*` and `tools.*` import in what `deploy-cron-scripts.sh` stages (the cron
  scripts, the runtime compose helpers, `extensions/`, enabled pack and operator extensions, and
  the pack's scripts and connectors) and runs each in the gateway's venv. A stage-only roll onto
  an image that cannot satisfy one is refused, naming every missing import. An import inside a
  `try` that tolerates `ImportError` is optional and is not probed.

### Reader (baked)
- **Page revision token (okengine#1023, !1301).** A settled page's revision token is the legacy
  `mtime_ns:size` again, so render-lint's stored baseline matches without a reset, and a restore,
  move or `chmod` no longer invalidates it. An unsettled page's token still carries a content hash,
  keeping the v0.15.10 same-tick protection.

### CI and tests
- The cockpit datasets service imports `YAMLError` by its bare name, so the mutation gate can
  mutate it (!1300).
- The smoke fixture vault declares its pack, and the smoke stack prints a failing service's last
  log lines (!1302).
- `integration-suite`, `hermes-target-contracts`, `public-snapshot` and `public-compatibility`
  run on the CI nodes' heavy slot, with a routing detector for heavy jobs (!1292, !1295).

### Issues
- Fixed and closed: okengine#1014, okengine#1020, okengine#1023, okengine#1026, okengine#1027,
  okengine#1028.

## v0.15.10

PATCH. Every file-backed cache is keyed on one shared file fingerprint instead of mtime alone
(okengine#1017, !1293). Deployment scope: **fleet required**. **Baked: rebuild the gateway,
reader and cockpit images**, and roll the image before staging the cron scripts.

### Operator actions for the roll
- **Build the gateway, reader and cockpit images first, then run `deploy.sh` per host.** The
  staged `schema_lib.py` and `build_index_tree.py` import the new `okengine.file_fingerprint`
  module from the image. The stage preflight now refuses to stage onto an image that lacks it,
  as it already refused one without `is_reserved_basename`.

### Caches (baked)
- **File-backed caches no longer serve stale data within one timestamp tick (okengine#1017).**
  A cache keyed only on a file's mtime missed a rewrite inside one filesystem timestamp tick, so
  the write path could validate against an outdated schema. One shared key,
  `okengine.file_fingerprint`, replaces it: the file's device, inode, size, `mtime_ns` and
  `ctime_ns`, plus a SHA-256 of the content while the file's newest timestamp is within 2 s of now
  (unsettled). A settled file costs one `stat()`. It covers 13 sites:
  - the write path's schema caches (`schema_lib` base, governing and composed schemas) and the
    validator's schema load;
  - the extension scope token store, so a revoked token stops resolving within the same tick;
  - the authorization effective policy and output-contract enforcement's jobs store;
  - the index builder's retirement cache;
  - the backlink caches in the MCP server, the reader and the cockpit;
  - the reader's root-namespace cache and page revision token, and the cockpit's deck-render
    cache.
- A standing test pins every remaining mtime read in engine code to a reviewed non-cache use
  (an age check, a sort, a window or a tripwire), so the list only shrinks.

### Issues
- okengine#1017 is fixed and closed.
- Follow-up filed: okengine#1019 (same-tick misses in mtime comparisons and watermarks that are
  not caches).

## v0.15.9

PATCH. A new actor page must be grounded in its cited sources (okengine#1012, !1288). Deployment
scope: **fleet required**. **Baked write-path change: gateway image rebuild**
(`src/okengine/write_services/{integrity,convergence,transactions,patching}.py` and the new
`src/okengine/actor_grounding.py`). Staged cron scripts roll too (`importer_guard.py`,
`corpus_audit*.py`).

### Operator actions for the roll
- **Build the image first, then run `deploy.sh` per host.** Existing pages are not changed; the
  new audit metrics list them for review.

### Write path (baked)
- **A new or retyped actor is grounded in its sources (okengine#1012).** A model lane could mint
  a non-actor (a sandbox vendor, a hosting provider, a campaign name, a nation-nexus adjective, a
  name no source contains) as `type: actor` through the enforced write path, because every
  identity check read the profile the model had just written. Now `create`, `update`, `patch`
  and `converge` refuse a create of an actor page, or a retype of any page to `actor`, unless a
  cited source grounds it:
  - The source must name the subject by its title or name. An alias counts only when it is tied
    to the subject: it shares a distinctive word with the title or name, appears in one sentence
    with the name and an alias cue ("also known as", "tracked as" and the like), or is
    slash-joined with it.
  - The source must affirm the subject as an adversary in its own words around the mention: role
    grammar, agent verbs, passive agents ("attributed to X"), a named kind, a list under an
    adversary heading, naming phrases, or an attribution field. A tracker designator named in a
    source body affirms itself. Hedged or negated sentences, headline-only mentions and a name
    that only modifies another noun do not count.
  - A curated record vouches without prose only for its own subject's actor record, and only when
    marked `evidence_role: curated-authority`. That field is operator-owned, so a model write
    cannot set it. Provenance labels are not evidence for a model write.
  - A name equal to its publisher must be affirmed by a source; no curated record or label can
    vouch for it.
  - Refusals carry a reason code: `unsourced`, `no_resolvable_source`, `name_absent_from_sources`,
    `name_equals_publisher`, `source_defines_non_actor` or `no_source_affirms_actor`. An
    unresolvable or unreadable source is not evidence. An update to an existing actor is not
    re-judged, and lane ownership does not bypass the check on a retype.
- The predicate lives in one new library, `okengine.actor_grounding`.

### Deterministic writers and detection
- `importer_guard.guard(fm, vault=..., creating=True)` applies the same predicate to a direct
  writer's create; there, provenance labels vouch. It reports and never fails open. The default
  stays `creating=False`, so no current direct writer changes behavior until it opts in.
- `corpus_audit` adds two WARN metrics: `actor_identity_ungrounded_pages`, a review list with a
  per-reason breakdown, and `actor_identity_unsourced_pages`. Both report **undetectable** when the
  grounding library is missing.

### Issues
- okengine#1012 is fixed and closed.
- Follow-ups filed: okengine#1013 (v0.15.8 invariant-audit candidates waived for the release),
  okengine#1014 (actor grounding follow-ups: renames, the autoverify hold, predicate residuals,
  and pack direct writers opting into `creating=True`).

## v0.15.8

PATCH. cron-plus runners are no longer killed by the gateway's MCP startup cleanup, and an
orphaned run record no longer keeps the healthcheck red (okengine#1010, !1286). Deployment scope:
**fleet required**. **Baked: gateway image rebuild** for carried Hermes patch 25. The cron-plus
change is stage-only.

### Operator actions for the roll
- **Build the image first, then run `deploy.sh` per host.** Nothing to run after the roll.

### Carried Hermes patches (baked)
- **MCP cleanup no longer kills cron-plus runners (okengine#1010).** The stdio MCP transport
  identifies a new server as the change in the gateway's direct children across the spawn, and
  cron-plus runners are direct gateway children too. A runner spawned while MCP discovery was
  connecting was tracked as that server's process and killed by the stale-connection cleanup,
  leaving its run record `running`. The new carried patch
  `25-mcp-cron-plus-runner-child.patch` adds the `cron-plus/runner.py` argv marker to Hermes'
  MCP child-pid exclusion markers. It does this from `tools/mcp_tool_discovery.py`, an
  already-carried surface, so the touched-upstream-file count stays at 35. The carried set is now
  24 patch artifacts. Carried patches are applied at image build, so this needs a gateway image
  rebuild and roll.

### cron-plus (stage-only)
- **Per-tick orphan reconciliation (okengine#1010).** Orphaned run records were closed only when
  the same lane started again, so one killed run kept the gateway healthcheck red until that
  lane's next run. Each scheduler tick now closes the newest dead `running` record of every lane,
  reading only each lane's newest record. A run that closes its own record during the liveness
  probe keeps its outcome, and a sweep failure is logged without stopping the schedule. It is
  applied by `install-cron-plus.sh` during deploy.

### Issues
- okengine#1010 is fixed and closed.
- Follow-up filed: okengine#1011 (add pid-namespace and start-time identity to orphan
  reconciliation).

## v0.15.7

PATCH. The operator-tier trust gate keys on what an extension runs (okengine#1004), and list-field
scalars keep prose whole (okengine#1005). Deployment scope: **fleet required**. **Baked write-path
change: gateway image rebuild** (`src/okengine/list_scalars.py`, `write_services/validation.py`,
`okengine-mcp/write_server.py`, `scripts/cron/schema_lib.py`; !1283). Staged cron scripts roll too.

### Operator actions for the roll
- **Build the image first, then run `deploy.sh` per host.**
- **After the roll, run `framework repair --repairs legacy-scalars`** where its dry run plans
  pages. `--apply` refuses unless the deployment's runtime stamp says v0.15.7 or later.

### Write path (baked)
- **List-field scalars keep prose whole (okengine#1005, !1283).** The write path and the
  legacy-shape repair lane share one rule (`okengine.list_scalars`). A `list` field splits on its
  commas, but never at a comma inside brackets, a date or a number. A new `field_shapes` value,
  `prose-list`, declares a list of sentences, and a scalar written for one is kept as a single
  item. The assessments extension declares `alternatives`, `assumptions`,
  `would_increase_confidence` and `would_decrease_confidence` as `prose-list`. `basis` and
  `adversarial_evidence` stay `list`, because engine consumers read their items as references.
- **`repair_legacy_shapes.py --apply` now requires an engine runtime of v0.15.7 or later**, so the
  gateway that serves the repaired pages applies the same rule. The lane's reshape-only check
  accepts either reading of a scalar (its delimited items or the whole string); a split inside
  brackets is a data change.

### Extensions
- **The operator-tier trust gate keys on what runs (okengine#1004, !1281).** An operator-tier
  extension labelled `trust: declarative` that shipped a script entrypoint (in a manifest block or
  a `crons/*.cron.json` drop-in) passed `enable`, validation and the load-time re-check without a
  recorded acceptance, then was staged and scheduled in the gateway. Now:
  - Acceptance is required when an operator-tier extension is `trust: in-gateway` **or** runs
    gateway code (`extension_discovery.runs_gateway_code`): a non-sidecar operation that names a
    script, an operation shipped with top-level `*.py`, a toolset outside the no-code allowlist
    (`okengine`, `okengine-write*`, `web`, `todo`, `no_mcp`, `file_read*`), or an unreadable
    drop-in.
  - `trust: declarative` declares no operation at all, prompt or script, manifest or drop-in.
    Discovery, both validators and the composer reject it, and `--allow-untrusted` cannot get
    around it. A declarative extension with no operation composes to no job.
  - An empty `trust_accepted: {}` no longer counts as acceptance for a manifest with no `trust` key.

### Crons
- **`build_hot_set` reads its clock once per run (!1284).** A run that straddled midnight UTC could
  cut its `recent` and `open` windows on different days. The tests pin the date, which removes a
  midnight flake in `test_default_hot_set_window_and_cap`.

### CI
- The mutation timeouts of the six targets that run `tests/extensions/test_trust_gate.py` are
  re-measured from CI (82 s JUnit sum) and raised to at least twice that baseline (!1282).

### Issues
- okengine#1004 is fixed and closed.
- Follow-ups filed: okengine#1005 (v0.15.6 invariant-audit candidates waived for the release; the
  comma-split and hot-set clock items above came from it), okengine#1006 (`test_trust_gate.py`
  cost in CI), okengine#1007 (a prose
  `basis`/`adversarial_evidence` scalar with a plain comma is still split), okengine#1008
  (`select_daily_brief` mixes UTC and local clocks).

## v0.15.6

PATCH. Follow-ups from the v0.15.4 and v0.15.5 release audits (okengine#994, okengine#995), the
victim guard's defender post-modifier (okengine#996), and reserved page names (okengine#997).
Deployment scope: **fleet required**. **Baked write-path changes: gateway image rebuild** for
`src/okengine/actor_identity.py` (!1271, !1275), the reserved-name predicate in
`tools/schema_validator.py`, `scripts/cron/okf_migrate.py` and `write_services/policy.py` (!1277),
and the stored-echo repair in `write_services/{authorization,transactions,patching}.py` (!1278).
Staged cron scripts roll too.

### Operator actions for the roll
- **Build the image first, then run `deploy.sh` per host.** `deploy-cron-scripts.sh` now checks
  the gateway's baked `tools.schema_validator.is_reserved_basename` before it stages anything and
  refuses to stage onto an image that predates it. `okf_migrate` refuses to load against such a
  validator (an `ImportError`, not a silent fail-open), so the staged scripts and the image roll
  together (!1277).
- **Operator-tier extensions need a recorded trust acceptance (okengine#994 [14], !1276).** An
  operator-tier `trust: in-gateway` extension with no recorded acceptance is neither staged nor
  scheduled, and `framework validate` (deploy step 1) FAILs naming the command:
  `framework extensions enable <deployment> <id> --allow-untrusted`, run once. The accept also
  reconciles the extension's token to its current manifest scopes, which can widen a token minted
  before the manifest gained scopes: review the manifest's scopes before accepting.
- **Post-roll repairs.** Run them only after the roll:
  - `framework repair --repairs malformed-slugs` (okengine#997). Before the roll, a no_agent
    writer would re-mint the reserved name on its next run. Run the dry run first: a
    tombstone whose `superseded_by` does not resolve is reported as an error for a manual fix, and
    scaffold-shaped pages such as `_template.md` are listed for review, never renamed.
  - `framework repair --repairs legacy-scalars`, then, as a separate decision, `legacy-wikilinks`
    (okengine#994 [19]). Apply refuses unless the deployment's runtime stamp
    (`.hermes-data/engine-runtime.yaml`) says v0.15.6 or later. Run the scalar class first; the
    wikilink class can touch many pages on a large vault. See `docs/audit-and-repair.md` for the
    audit, plan, apply and verify commands.

### Write path (baked)
- **Victim guard recognizes every adversary self-description (okengine#995, !1271).** The source
  role cue list was closed, so an actor described only as a hacktivist, cybercrime, espionage or
  state-sponsored group was rejected as a `victim organization`. The cues now cover those terms
  and the related adversary vocabulary (whole-token, case-folded). Affiliation cues
  (`state-sponsored`, `nation-state`, bare `hacker`) count only before an adversary kind such as
  group, actor or gang, not before broad nouns like organization or team. Defender modifiers
  (ethical, white/grey/gray hat, red/blue/purple team, bug bounty, CTF, student, academic,
  university, pentest and others) break the descriptor. Words that name defenders as often as
  adversaries (`fraud`, `financially motivated`, `cyber`, `state`) are deliberately not cues. The
  !1268 head-noun rule and the "attributed claims still count" decision are unchanged.
- **Victim guard checks the head noun's post-modifier for defender members (okengine#996,
  !1275).** "a hacking group of ethical hackers", "a hacking collective of students" and "a hacker
  group that runs a conference" are rejected as a `victim organization` again. The scan reads a
  membership phrase (`of`, `consisting of`, `comprising`, `including`), a relative clause whose
  verb constitutes, runs or hosts the group, and a copula's noun complement. A clause with any
  other verb ("that targets university researchers") names a target, tool or lure and is not
  scanned. Regular plurals are matched. The scan stays linear.
- **No page is minted under a reserved or dot-leading basename (okengine#997, !1277).**
  `schema_validator.is_reserved_basename` is the one predicate for the OKF reserved names (plus
  the pack's `reserved_files`, case-insensitive), dot-leading names, `_` scaffolding, `INDEX.md`,
  `INDEX-*` and `index-*`. The validator's exemption, the write path's refusal
  (`policy._reserved_refuse`), the `okf_migrate.write_key` minting guard and the detector all call
  it. `write_key` mints a stand-in (`<slug>-<type>` or `<type>-<slug>`) and reuses a live page that
  already holds one. `find_page` never returns an engine-generated file, and a tombstone at a
  reserved name no longer takes the write. The detector FAILs a live page under a reserved or
  dot-leading name and WARNs on tombstones and scaffold-shaped pages. The new
  `repair_malformed_slugs` reserved-name path renames live pages to their stand-in, keeps the
  `id`, rewrites links, sends a tombstone's inbound references to its `superseded_by` target, and
  runs as one corpus transaction.
- **A correct repair of a legacy field shape is stored (okengine#994 [19], !1278).** The #991
  stored-echo guard replaced a sent value with the stored one for judging and for storing, so a
  write that sent the canonical shape of a legacy value (a scalar list field as a list, a
  `[[link]]` as the plain path) was reported `updated` while the legacy shape was put back.
  Judging is unchanged; storage now keeps a JSON date echo's stored date, stores a reshape in its
  canonical shape and stores a real change as sent. Operator-owned attestation fields stay byte
  for byte (#980). `_preserve_immutable` keeps the canonical shape of a stored immutable value
  without flagging it, so a patch to a page with a legacy `discovered_by` no longer sets a
  spurious `needs_review`; a real change is still reverted and flagged, now compared
  type-strictly. Every check in `_update`/`_patch` that compares the write with the stored page
  reads canonical views of both sides, so echoing a legacy multi-value `aliases` or `sources` no
  longer records a removed alias or refuses the write.

### Repair lanes
- **Legacy-shape repair lane (okengine#994 [19], !1278).** `scripts/cron/repair_legacy_shapes.py`
  is registered in `framework repair` as `legacy-scalars` and `legacy-wikilinks`. The canonical
  value is the write path's own `_coerce_fm`. A field changes only when the reshape keeps the same
  items in order; a canonical form that would lose data (`[[x|Label]]`, an anchor, a collapsed
  duplicate) is skipped and reported. Attested pages, operator-owned fields, tombstoned, reserved
  and refused pages are skipped. Only a repaired key's bytes change. `framework repair plan` records
  every selected repair's dry run, and `verify` fails while a planned repair remains.
- **Repair lanes keep CRLF bytes; `malformed-slugs` leaves no dangling links (okengine#994, !1279).**
  - The legacy-shape lane read and wrote pages with newline translation, so a CRLF page came back
    entirely LF. Pages are now read and written untranslated, and a repaired key's new lines take
    that line's own ending. Verification asserts that every byte outside the edited spans is
    unchanged.
  - `malformed-slugs` now plans every move first and refuses two pages claiming the same new name.
    It renames inside one corpus transaction, then rewrites references in one pass with the
    whole-token `okf_migrate` rewriters, keeping CRLF. Before this:
    - pages that linked to each other were left with dangling links;
    - a page's link to itself did not follow its rename;
    - a slug that is a prefix of another corrupted the longer one's links;
    - the dry-run count disagreed with apply.
    Files are now reported by their original path, and a failed write is reported as an error.
  - `okf_migrate.make_path_rewriter` (baked) no longer skips a path right after a single `[`. The
    first item of a YAML flow list (`related: [entities/x]`) and bracketed paths in prose are now
    rewritten by every rename and dedup lane; `[[wikilinks]]` are unchanged.

### Fail-closed (okengine#994, !1276)
- **budget_guard** (#994 [9]): an unreadable `state.db` (missing, unopenable, locked, corrupt, or
  without a token column) is unknown usage, not zero. The guard never resumes, never self-heals
  orphans and never pauses on a guess; a paused guard still re-pauses re-enabled crons, and the
  tick exits 1. With no budget set, as on every deployment today, the guard is a no-op.
- **Operator trust acceptance** (#994 [14]): see Operator actions. `enable --allow-untrusted`
  records `trust_accepted` in the enabled-state entry; disable drops it.
- **entity_link** (#994 [20]): a bare scalar alias or source is a one-item list instead of a
  crash; a YAML boolean is never a name.
- **question_kind** (#994 [21]): an explicit `question_kind` outside the policy vocabulary keeps
  the reference blocking (`unknown-question-kind`) instead of demoting it to contextual.

### Generated pages and deploy checks (okengine#994, !1272, !1274)
- HOT.md drops every tombstoned page, not only retired actors (#994 [11]).
- The INDEX tree uses the shared `generated_page.is_exempt` predicate, so a paginated index no
  longer lists its own pagination pages, and namespace, BUNDLE and HEALTH counts exclude
  INDEX/INDEX-pNN/_about/log files (#994 [23]). Content totals fall accordingly, by about 2-3%
  on a large vault.
- `deployment_checks.run()` FAILs an unknown check name instead of dropping it, and post-deploy
  verify reports a staged `deployment_checks.py` that predates `chat-capability` as stale (#994
  [15]).
- `deploy-cron-scripts.sh` reports staged extension directories that the stage plan no longer
  contains, and removes them only with `--prune-stale-extensions` (#994 [27]).
- `framework extensions disable <core id>` opts a default-on core extension out instead of
  printing "not enabled" and leaving it active (#994 [26]).

### Environment and backup (okengine#994, !1273)
- Host `doctor`, `status` and post-deploy verify judge the gateway environment that compose builds
  from the pack `.env`, not the operator's shell (#994 [16]).
- `ensure-runtime.sh` reads `OKENGINE_MCP_TOKEN` with `dotenv_lib` (compose grammar), rewrites
  every spelling on regeneration, and FAILs the deploy if compose would not hand the containers
  the new token (#994 [17]). Appended pins no longer glue onto an unterminated last line.
- `hermes_uid.sh` `_okengine_env_file_val` is last-wins with interpolation, like compose (#994 A).
- A no-secrets backup records the names (never the values) of `.hermes-data/.env` keys it does not
  carry, and restore lists them (#994 B).

### CI and tests
- New `dotenv-compose-parity` job runs the compose cases with compose required, so a missing
  compose FAILs instead of skipping (#994 [18], !1273).
- `hermes-target-contracts` watches `patches/apply.sh`, `patches/cron-plus/**` and
  `scripts/install-cron-plus.sh`, and runs the shipped `apply.sh` twice to prove the series is
  idempotent (#994 [22], !1272).
- The test session refuses to start when `okengine` resolves from outside this checkout's `src/`
  or an installed wheel (#994 [24], !1274). The `test_dump_from_live` round trip seeds the
  generating pack's prompt files (#994 D, !1274).
- `CHANGELOG.md` records #973 (!1266) and its victim-guard fix (!1268) under v0.15.5 (!1270).

### Issues
- okengine#996 is fixed and closed. okengine#997 stays open until the live `malformed-slugs` repair
  runs. okengine#994 and okengine#995 are umbrella audit issues: the items above are fixed, and
  both stay open for their remaining items.
- Follow-ups filed: okengine#998 (victim guard: appositive and unlisted post-modifier defender
  forms), okengine#999 (read-side `INDEX`-prefix and `.bak` skips hide writable pages),
  okengine#1000 (`field_values.same_value` misjudges YAML-anchored values as changed),
  okengine#1002 (write-path reshape rule and the legacy-shapes lane disagree; `_coerce_fm` is not
  idempotent), okengine#1003 (`malformed-slugs`: bare references to a collision-refused slug with a
  space can be corrupted).

## v0.15.5

PATCH. Follow-ups from v0.15.4 (okengine#993). Deployment scope: **fleet required**. Baked write
path: gateway image rebuild (chat status rule); the reader image rebuilds (whole-envelope tombstone
read); staged cron scripts roll (resharder and frontmatter readers).

- **Chat cannot retire a page (okengine#993).** A new capability key, `forbidden_values`, holds a
  field to a value deny-list. The chat floor forbids moving `status` to a retirement value
  (`tombstoned`, `superseded`, `retired`), case- and whitespace-insensitively, after field and value
  aliasing, on every frontmatter-writing path. A legacy stored value that an alias maps to a
  retirement status cannot be normalized into retirement by an unrelated chat edit. A pack's chat
  replacement may widen the list but never drop or narrow it. Converging a new id still requires
  `create`, now stated in the refusal; chat stays refused under walk-up sub-domains.
- **Partition loop fixed (okengine#993).** `reshard-oversized` read only the first 4000 characters
  of frontmatter, so a long-frontmatter source got a different shard than the reshelve drain gave
  it and moved back and forth every day. It now uses the drain's own reader. Views, the daily
  brief, the Wardley map and the conformance and grounding audits share one streaming, uncapped
  frontmatter reader; the daily brief no longer lists a tombstoned page as fresh or updated, and
  `rebuild-index` treats an impossible date as unparseable instead of crashing.
- **Reader sees long-frontmatter tombstones (okengine#993).** The reader's head read extends until
  the frontmatter closes, so a retired actor with tens of kilobytes of frontmatter is hidden like
  any other.
- **Deploy gates match Hermes (okengine#993).** The actor-less-writer gate follows Hermes' full
  platform-config precedence (`gateway.platforms`, `platforms`, `gateway.<x>`, top-level blocks,
  relay URL), sees photon credentials stored in `auth.json`, and treats a default-bound chat writer
  as bound. The shared `.env` reader resolves `${VAR}` in compose's order. `deploy.sh` pins the
  reader's `OKENGINE_EDITING` to the `.env` value so the reader and gateway agree.
- **Smaller fixes (okengine#993).** Whole-value regex validators across the engine anchor with `\Z`
  (the detector now scans every source tree). `patches/apply.sh` reports a failed checkout
  snapshot. `install-domain` records a guest pack's chat declaration even on a re-install without
  `--refresh`.
- **Descriptive threat-actor identities (okengine#973, !1266) with a hardened victim guard (!1268).**
  Original sources may now establish a dual-role actor through a descriptive identity ("X is a
  cybercriminal ransomware group"). The descriptor must head the noun phrase: a cue word must
  modify the actor noun directly, and prepositions, victim/target/defender nouns, possessives and
  "hit by …" end it, so a victim ("X is a victim of ransomware group Akira", "an APT group's
  target") stays rejected as a victim organization. The role scan is linear (a 200 KB source page
  went from 36 s to 0.03 s on the write path).

## v0.15.4

PATCH. Security and reliability follow-ups from the v0.15.3 release audits (okengine#991), plus
question-scoped country evidence (okengine#992). Deployment scope: **fleet required** (security).
Baked write path: gateway image rebuild (the chat write policy). The read-MCP image also rebuilds
(`tier_lib`), and the reader needs `OKENGINE_EDITING` passed through in hand-maintained pack
composes (the pack skeleton now does this).

- **Chat write policy (okengine#991).** A chat write under a walk-up sub-domain is held to the
  same namespace ceiling as a top-level one, judged by the page's namespace position (shard
  directories such as `entities/t/_/` are ordinary storage). The chat floor also protects
  `redirect_to`, `link_stub`, `tombstoned` and `canonical`. A read-modify-write that echoes a
  stored date or legacy value back is no longer read as a protected-field change, for any writer.
  A chat create that dedups into an existing page is refused with a message naming that page.
  `framework validate` and `install-domain` warn when an installed guest pack ships a chat
  capability that is never composed.
- **Deploy and runtime gates (okengine#991).** One shared `.env` reader (`scripts/cron/dotenv_lib.py`)
  matches `docker compose` for comments, `export`, quotes, CRLF and duplicates; ensure-runtime,
  `framework validate` and the in-gateway check all use it, so `OKENGINE_EDITING=0` in `.env`
  reliably unbinds the chat writer. The actor-less-writer gate now sees messaging platforms that
  pinned Hermes enables from credentials alone, and reads `.hermes-data/.env` as Hermes does. A
  new in-gateway check FAILs when a bound chat writer has no capability in the baked catalog.
- **Engine upgrades fail closed (okengine#991).** Migration state and pin files are written
  atomically; an unreadable state file stops the upgrade instead of replaying every migration.
  `deploy.sh` stops when an engine migration fails or rolls back (an ahead pin still falls
  through to validate). Rollback snapshots cover `.hermes-data/.env`. A crashing validator rolls
  the upgrade back.
- **Backup restore and extension checks (okengine#991).** Restore refuses to run as root and
  never pins uid 0; non-secret `.env` settings (timezone, schedule, tuning) are carried by an
  explicit allowlist, values must be printable, and restore lists the keys it did not carry. A
  crashing post-restore validator fails the restore. `framework validate` reports extension
  discovery faults even without an enabled-extensions map. Whole-value regex validators in
  `scripts/` anchor with `\Z`, guarded by a detector test.
- **Generated views and tiering (okengine#991).** Retired actors with long frontmatter are hidden
  from the index tree and hot set, which now share one uncapped frontmatter reader. Any
  tombstoned page tiers cold (`tier_lib`); on large vaults this moves hundreds of tombstoned
  sources and entities to cold and lets long-frontmatter pages leave cold.
- **Carried patches (okengine#991).** `patches/apply.sh` is idempotent on re-runs and names the
  patch a drift actually sits in (exit 2 on every failure path); the patch-integrity tests cover
  the live target series.
- **Release-audit fixes.** A secrets-excluded backup no longer archives `.hermes-data/.env`
  (Hermes' credential file). Backup now carries `OKENGINE_EDITING`, `API_SERVER_ENABLED` and
  `COMPOSE_PROFILES`, so a restored read-only deployment stays read-only, and a duplicated `.env`
  key resolves last-wins as compose does. Cockpit HTML escaping covers quotes, so vault-derived
  text in `title`/`aria-label` attributes cannot inject markup.
- **Question-scoped country evidence (okengine#992).** Country evidence references are scoped to
  the question, collection blockers are explained, and canonical operator provenance is preserved
  in scoped evidence.

## v0.15.3

PATCH. Write-path identity and authority hardening. Deployment scope: **fleet required**
(security). Baked write path: gateway image rebuild; the image and staged cron scripts must roll
together (`corpus_audit` imports the new `okengine.attestation` module), then `ensure-runtime` and
a gateway recreate (the Agent Chat writer registration and `platform_toolsets.api_server`
migration, okengine#983).

- **The Agent Chat writes through its own actor-bound writer (okengine#983).** On every
  deployment `platform_toolsets.api_server` bound the generic `okengine-write`, which carries no
  `OKENGINE_WRITE_ACTOR`, so the write path treated the reader/cockpit Chat agent as admin: no
  capability, no namespace limit, no review. Chat now writes only through `okengine-write-chat`
  (actor `chat:api_server`, tools `create_entity`/`update_entity`/`append_to_section`), held to a
  catalog floor: create/update/append in `entities/` and `concepts/`, frontmatter-only updates,
  no provenance, handling, review or attestation fields, and every chat write queued for human
  review. Capabilities gain per-operation `body` modes (a single string still applies to every
  operation). A pack may replace an engine-declared `chat:*` capability, and nothing else, inside
  an engine ceiling that composition and `framework validate` enforce. `ensure-runtime` registers
  the chat writer and migrates existing configs (honoring `OKENGINE_EDITING`); `framework
  validate` and the in-gateway deployment check FAIL any model-facing platform that can reach the
  actor-less writer, with the CLI exempt by a named operator-surface rule. A `chat:` actor no
  longer resolves as a cron lane in the output contract. Field-change checks (the update
  capability check and the #980 attestation guard) compare values type-strictly via canonical YAML,
  so `0` stored over `false` is a change, and a pack `field_aliases` key is judged under its
  canonical name. Because `deploy.sh` validates before ensure-runtime migrates, `framework
  validate` reports a pre-#983 `api_server` list as a pending migration (WARN) rather than
  failing the upgrade that removes it; any binding the migration does not remove still FAILs.
- **An alias-matched create no longer recurses.** A create whose alias matches a live entity
  the id index does not hold yet (a page a lane wrote straight to disk) looped converge → create
  → dedup until the interpreter's recursion limit, and was rejected as "not valid YAML" (or
  crashed on Python 3.11). It is now refused by name and flagged for review.
- **Retired actors leave generated navigation (okengine#975).** `build-index-tree`,
  `build-hot-set` and `rebuild-index` omit an explicitly tombstoned actor (its type resolved
  through the governing schema's aliases) from listings, counts and pagination, while keeping
  every other page and the generator's own pagination ownership checks. The index tree judges
  each page once per run (cached by path and mtime, with a `tombstoned` pre-filter before any
  YAML parse), so its counts stay a directory walk on large vaults.
- **Composed `@<pack>` write actors keep their engine lane's checks (okengine#961).** On a
  composed host a lane's writer is bound as `cron:<lane>@<pack>`. Every lane-keyed write-path check
  compared the raw actor, so a composed lane got the full write-tool surface and a contract-derived
  `update_fields: ["*"]` capability instead of its lane's. One normalizer (`state.lane_actor`) now
  feeds the tool fence, the lane-specific checks, capability lookup and the converge rule.
- **The N-way compose path produces a safe job store (okengine#968).** `compose` and
  `regen_composed` now go through the same finishing step and gate list as `regen` (extension
  pass, actor-bound writers, every validator). One driver per engine-template lane: two packs
  driving the same lane is a hard error. A composed instance keeps the engine job id.
- **A pack cannot take another lane's write identity (okengine#969).** `regen`, `compose`,
  `framework validate`, `framework extensions validate` and a new deploy-time ownership check
  refuse a pack-authored job or pack/operator-tier extension operation that would bind another
  lane's write server: an `@` in a domain job name, a name equal to or folding to an engine lane
  (`Trends-Refresh`), an explicit foreign `okengine-write-*` (including the baseline
  source-quality server and every tier-1 extension operation's server, enabled or not), a stale
  server another pack left registered, or the generic actor-less `okengine-write` on a model lane.
- **`CRON_PACK_DIR` is required (okengine#970).** `regen`, `regen_composed`, `compose` and `dump`
  refuse an unset or missing pack directory instead of silently using the `/path/to/pack`
  placeholder (which produced an engine-only fleet). `cron_pack_split check` runs the shared
  `fleet_errors()` gate list.
- **Vulnerability subjects are not admitted as threat actors (okengine#973, !1232, !1236).**
  Actor admission checks the effective normalized actor on create, update, converge and patch,
  and rejects vulnerability-led prose, while keeping a named actor whose next sentence describes
  its activity. Baked write path.
- **Main's nightly critical mutation campaign is green again (!1237).** Line-keyed survivor
  dispositions re-keyed after code moved; tests that only killed their mutants on Python 3.13+ or
  as a non-root user fixed; `mcp/projection.py` and `write_services/convergence.py` back above
  their 90% floors. A new test runs the public-snapshot boundary in the normal suite.
- Design: source-backed actor identity qualification contracts (okengine#972; docs only).

- **Curated-authority attestation is operator-owned (okengine#980).** The local-evidence
  resolver treats a source as an attested authority record when an actor lists it in
  `operator_evidence_refs` and it carries `local_only`/`export_policy`/`retrieved_via`/`dataset`/
  `record_checksum`; a pack policy may then auto-accept a bounded claim from it with no URL
  check. Every model lane with a `sources` or `entities` namespace could write those fields:
  `page-quality-enrich` turned an ordinary blog post into an attested record and linked it from
  the actor in two `update_entity` calls. Every MCP write (job lane, extension, or the unbound
  stdio writer the chat agent uses) must now preserve `operator_evidence_refs`, `local_only`,
  `export_policy`, `record_checksum`, `bounded_auto_accept`, `retrieved_via`, `dataset` and
  `evidence_role` exactly, and may not change an attested record's `publisher` or body. The
  deterministic importer that writes records directly is unaffected; so are scoring and other
  enrichment of an attested record. `corpus_audit` reports attestation on a non-source page, on a
  page a model lane created, `operator_evidence_refs` naming an unattested page, and `operator_evidence_refs` naming no page by exact path. The resolver now resolves `operator_evidence_refs` by exact page path only (no id/URL fallback), and an attested record's `vendor`, `id`, `url` and authority-id lookup keys are frozen with its `publisher`; the check runs after pack field aliases resolve. Baked write
  path: gateway image rebuild (`src/okengine/attestation.py`, `write_services`).

## v0.15.2

PATCH. Closes the remaining paths that could replace an existing page's curated content.
Deployment scope: **fleet required** (data integrity). Baked write path: gateway image rebuild;
`raw_compile.py` is stage-only.

- A write that asked to CREATE and was redirected by dedup (alias, URL, authority id) onto an
  existing page, or a raw capture of an already-compiled URL, now merges additively, whoever the
  caller is. Before, `broken-wikilinks-drain` could replace a canonical page through its alias, and
  a snippet re-capture could replace a full extract. An explicit converge that falls through
  `_create` keeps replace semantics. On a source page the FULLER capture's body wins: a snippet
  never overwrites a full extract, and a fuller re-extraction can still correct one. This never
  crosses an identity conflict. Alias accrual on a merge is kept,
  except for an alias another live page already holds as a name or alias. Dedup-verified alias
  spellings count as agreement (`normalize_key`). List-valued identity fields are compared, not
  unioned (okengine#962). Baked write path: gateway image rebuild.

## v0.15.1

PATCH. Prevents incomplete PostgreSQL projections and conflicting projector mutations.
Deployment scope: **fleet required** for projection data integrity. Gateway and sidecar
images must be rebuilt; existing operator owner overrides remain visible.

- The projector uses the vault owner's UID/GID in new scaffolds and generated overlays for
  existing deployments, retaining non-root execution and a read-only vault mount (#950).
- Unreadable pages or directories fail reconciliation and health before rows can be deleted
  or an incomplete epoch marked successful. Typed reads reject legacy incomplete epochs (#950).
- PostgreSQL transaction locks serialize bootstrap, reconciliation, incremental refresh, and
  scratch verification. Rollback/cancellation release the lock; a five-minute lock timeout
  bounds acquisition. Bootstrap commits before scanning so its view DDL does not pause readers
  for the duration of a rebuild (#951).
- Deploy persists its verified host `ENGINE_DIR` and exact `OKENGINE_SOURCE_SHA` in the
  owner-only deployment `.env`, preserving unrelated operator settings and overlays (#952).
- `entity-backfill` may only ADD to an existing page when identity resolution lands its converge
  on one: the existing body is kept (an empty page is still filled), list fields such as `sources`
  are unioned, existing values are kept, and `remove` is ignored. It had been replacing canonical
  pages wholesale -- 69 pages across four deployments in four days: an actor's MITRE description
  replaced by a one-line summary (which withdrew a downstream ATT&CK attribution), and on 11 pages
  `sources` entries dropped. Scoped by the server-bound write actor; lanes that
  recompile whole pages through converge keep replace semantics. Baked write path: gateway image
  rebuild. The rule also covers `concept-backfill` (same create-missing role), matches a composed
  lane's `@<pack>` actor name, never retypes a page, and treats a value held under a drift alias as
  present -- each found by the adversarial re-verify of the first fix.

## v0.15.0

MINOR. Source identity no longer fuses different records that share a service endpoint URL, and a converge that disagrees on an identity field keeps the existing body. Packs can declare their own identity fields (a new schema key), which is why this is a MINOR release. **Baked write-path change** (`src/okengine/write_services`, #901): rolling it needs a gateway image rebuild.

### Write path
- A bare service endpoint URL (ArcGIS `.../query`, `.../MapServer/<n>`, `.../FeatureServer/<n>`, Socrata `.../resource/<id>.json`, with no query string) identifies a service, not a document. It no longer mints a source identity, so records read from one endpoint stop converging into whichever page claimed the URL first. A record-specific URL (`.../query?where=...`) keeps its identity. The rule lives in one shared place used by the write path, `deployment_checks`, and the `reid_sources_by_url` / `dedup_sources_by_url` operator scripts (okengine#901).
- When a converge's frontmatter merge conflicts on an identity field, the existing body is kept byte-identical and the page is flagged for review, instead of being overwritten. The engine's identity fields are `title`, `name`, `subject` and `url`; a pack adds its own with the new optional schema key `types.<type>.identity_fields` (see `docs/authoring-a-pack.md`) (okengine#901).
- raw-compile rejects a capture whose URL is a bare service endpoint, as it rejects a capture with no URL (okengine#901; the re-offer of such a capture is okengine#942).

### Reader
- The reader hides tombstoned pages wherever it finds pages: namespace counts, directory listings, search results, backlinks, and links (a link to a tombstone resolves to a live same-name copy, or 404). Before this, only display groups and same-name fallback filtered them, so a retired page stayed one click away. `/api/page-revisions` still lists everything, because standing detectors read it. Rolling this release rebuilds the reader image.

### Crons
- Single-pack cron regeneration resolves a domain lane's `prompt_file`. Before this, such a lane deployed with an **empty prompt** and ran on the cron wrapper plus its selector output alone. On one deployment a pack's four model lanes ran that way for a week. Regeneration now also refuses an enabled model lane with an empty prompt before writing anything. This lands at deploy time, so rolling this release regenerates the affected lanes with their prompts.

### Repair and detection
- Slug attribution treats unreadable run records within one second of the snapshot as uncertain, accounting for coarse file timestamps. Exact nanosecond boundaries keep older records excluded, and readable run timestamps and declared-artifact attribution retain their existing contracts (okengine#946).
- `dedup_partition_collisions` also repairs case-only duplicate pages (`CVE-x.md` beside `cve-x.md`). The survivor is the spelling with the fewest capitals; separators are never folded. It keeps the lane's union merge, link rewriting and `needs_review` semantics, and never removes generated `INDEX`/`log`/`README` files in any case (okengine#883).
- The cron-plus slug guard fails a job only when it can attribute the collision to that job: the job declared the file as its own artifact, or no other lane ran in its window. Otherwise it records the collision as `unattributed` with the overlapping lanes, and the bystander job is not failed. Stage-only, applied by `install-cron-plus.sh`. Run records are judged with a 2 s allowance for coarse file timestamps, so a record written just after the window opened is not mistaken for one written before it (okengine#883).
- `deployment_checks.check_source_identity` WARNs on source pages that carry a bare service-endpoint URL, and no longer suggests merging them as same-URL duplicates (okengine#901).

### CI
- The diff-only docs gates no longer run in scheduled or manual pipelines. They failed closed there for lack of a diff base, and that lint-stage failure skipped every later job: from 2026-09-19 to 2026-09-29, no nightly critical-mutation campaign ran (okengine#783). The survivors that built up meanwhile in `corpus_transaction` and `output_contract_enforce` are killed or dispositioned.
- The mutation budget pre-check prices a target from its coverage-guided selection when a fresh map covers it, so whole-range release replays are no longer refused on full-command estimates (okengine#941).
- Release-test images are built once per candidate, and consumers verify them by digest (okengine#902). Verified tooling environments are prebuilt, and one source-attributed engine wheel is shared across jobs (okengine#905).

## v0.14.9

PATCH. Restores tag-to-code agreement again: the fleet was rolled from post-v0.14.8 `main` (462245f2) under the v0.14.8 marker, which the release checklist forbids. Also keeps generated-page identity, removes snippet evidence from inquiry analysis, and repairs source hydration and feed-capture recovery. **Baked write-path change** (`okf_migrate`, #915): rolling it needs a gateway image rebuild.

**Behavior change (#923):** inquiry analysis no longer counts discovery snippets, failed captures or unhydrated sources as article evidence. Deployments whose inquiry evidence is snippet-only will hold that analysis until real content is backfilled (`sources hydrate --missing-body`).

### Collection and sources
- Inquiry analysis derives acquisition state from raw provenance; a writer label cannot promote a snippet, and raw-compile records acquisition state on new pages (okengine#923).
- `sources hydrate --missing-body` inventories existing sources, previews by default and repairs with bounded `--apply` through the raw-compile write contract, preserving identity, content and raw references (okengine#923). It runs capture as the deployment's worker uid, never root (okengine#934).
- A feed item whose first capture failed is recovered on its first later success, without churning revisions when a publisher rotates GUIDs (okengine#928).
- `dedup-same-url` skips generated pages and pages without frontmatter when counting inbound links (okengine#886).

### Generated pages
- Engine-generated pages keep their `id` across whole-file rewrites and carry exactly the id `backfill_ids` would stamp, so the id backfill converges (okengine#933). Engine reports are typed `dashboard`, a base-schema type (okengine#920).
- `schema_drift_lint` buckets a typed page that lacks only OKF-required fields (such as `id`) as "missing OKF-required field(s)", not "typeless".
- `year_derivation_audit` walks `raw/` in sorted order.

### Packs and upgrades
- `install-domain --refresh` applies an owned namespace's partitioning change after proving no page moves; co-install preflight WARNs instead of failing for an owned namespace (okengine#915).
- Engine upgrades confirm their pre-upgrade snapshot once the vault validates, so `--keep-snapshots` prunes them; before, every roll kept a full vault copy forever (okengine#914).
- `prompt_lint` derives a lane's receipt requirement from its contract and selector (okengine#916).
- The pack skeleton no longer ships prompt keys that match no engine lane (okengine#929).

### CI
- `full-suite` runs as four shards combined into one coverage and JUnit verdict (okengine#898).
- Mutation testing selects, per mutant, the tests that cover its lines from a per-test coverage map built by the same suite run; every selection survivor is re-checked against the full test command, and a missing or stale map falls back to the full command (okengine#900). Target timeouts are held to at least twice the measured baseline (okengine#895).
- Coverage measures scripts that tests launch as subprocesses from another directory; five previously invisible cron scripts are now covered to 100%.

## v0.14.8

PATCH. Restores tag-to-code agreement: the fleet had been rolled from post-v0.14.7 `main` under the v0.14.7 marker, which the release checklist forbids. Also adds unencodable-text guards, a pack-currency check, same-URL source merging, entity-to-source linking, and declared lane expectations. **Baked write-path changes** (#894, #886 raw-source convergence): rolling it needs a gateway image rebuild.

### Write path and projection
- Frontmatter with a UTF-16 surrogate pair is rejoined into its character. An unpaired half is refused, naming the field; it has no UTF-8 encoding (okengine#894).
- The projection rebuild isolates a page with unencodable text: it projects the page with U+FFFD and flags `fm_error`, instead of aborting the whole rebuild (okengine#894).
- A second capture of a compiled URL appends its raw backlink instead of minting a second page, and raw
  backlinks are append-only in converge whoever writes (okengine#886).

### Collection and raw sources
- Feed titles keep astral characters (emoji) instead of JSON surrogate escapes, and raw-compile repairs legacy captures (okengine#894).
- raw-compile carries a capture's provenance verbatim, fills stub pages, and treats captured brackets as text (okengine#886).
- Capture extracts the document, not the page chrome (okengine#886).
- New `dedup-same-url` lane merges sources that share a URL and refuses placeholder URLs. Its weekly scheduled run is a dry-run report; merging needs an operator's `--apply` (okengine#886).
- raw-backfill is never told to use a tool it lacks. Deployments run with Hermes' tool-search bridge off, and `deployment_checks` WARNs when it is on (okengine#886).

### Lanes
- New `entity-link` lane links new sources to the existing entities they name, so trends and lacuna have sources to work from (okengine#888).
- Every lane declares `expect:`, what "working" means, validated for engine, extension and pack lanes (okengine#890, okengine#893).
- forecast-review may write where its selector sends it. trends-refresh nominates rising topics when no entity moves. lacuna ignores tombstoned pages and checks a concept's seats in a deterministic order.

### Deployment verification
- `framework validate` compares every installed pack, host and composed, with its configured source (`OKENGINE_PACK_SOURCES`, read at each checkout's `origin/main`). It reports BEHIND with the pending `Migration impact:` lines, AHEAD, or UNDETECTABLE, never a vacuous pass. The release checklist runs it before a roll (okengine#896).
- The post-deploy ownership sweep re-stats each stray, so a temp file renamed away mid-sweep is transient, not a FAIL.

### CI
- `full-suite`'s timeout is 45m, a stopgap: the single-job suite measured 1,619–1,800 s against 30m and timed
  out green changes. Sharding is okengine#898.

## v0.14.7

PATCH — capability-policy and write-contract repairs that had left several lanes red on every
gateway, per-item receipts for batch-triage lanes, and the invariant-audit findings from the
v0.14.6 cycle. **Baked write-path changes** (#863, #864, #875, #876, #879, #883, #884, #885,
#887): rolling this release needs a gateway image rebuild, not a stage-only deploy.

### Write path
- Only `tombstone_entity` may retire a page; update, patch and converge refuse a live→tombstoned
  transition (okengine#863).
- Converge keeps existing aliases and records the ones it removes as `rejected_aliases`, so an
  importer cannot restore them (okengine#864).
- A page path in any namespace is refused if it carries a control character (okengine#884).
- Governed write tools refuse an undeclared argument by name instead of silently dropping it —
  `content` for `body` no longer fails as an empty body (okengine#879).
- `find_page` matches case-insensitively, so an importer stops re-creating a case-variant
  duplicate daily (okengine#883).
- A negated distinctness phrase ("not a separate entity") reads as a duplicate verdict
  (okengine#887).

### Capability policy
- `allowed_types: ["*"]` means any type; engine lanes' writes to typed pages were refused
  fleet-wide (okengine#876).
- Policy audit discovers first-party extensions from the runtime engine root, not the baked one
  (okengine#875).
- Flag and review capability checks see the page's stored type (okengine#885).

### Lanes and receipts
- Batch-triage prediction and completeness lanes report per-item dispositions instead of being
  graded "must write" (okengine#878).
- messaging-synthesis contracts allow `briefing`, and the battle-cards selector tells the model
  which cards already exist (okengine#877).
- The tool-call probe reads MCP 2's `input_schema` (okengine#874).
- Raw-backfill recovers URLs from JSON connector records, feeds declare their publisher, and
  raw-compile attaches a capture to the page already holding its URL (okengine#862).

### Review
- The review queue lists every page `requires_review()` flags (okengine#867); every reconcile
  reason code has a glossary entry (okengine#866); the review-record fallback no longer
  collapses subjects across sub-domains (okengine#868).
- The inquiry-analyze prompt asks for the absence-evidence fields its consumer requires
  (okengine#865).

### Operations and CI
- Corpus audit parses only raw captures' frontmatter, with the C YAML loader where it agrees
  with the pure one (okengine#880).
- The runtime-ownership check judges a symlink by its own owner, not its target's (okengine#881).
- Feed fetch requests a non-ASCII (IRI) link as a URI, and one bad link no longer fails the run
  (okengine#882).
- `cron_pack_split` surfaces `pack.yaml` load errors instead of falling back to the directory
  name (okengine#857); the post-deploy ownership sweep lists exactly what it counted
  (okengine#858); the contract layer has its own measured deadline (okengine#859).

## v0.14.6

PATCH — write-path identity invariants, cron job-store concurrency, inquiry-driven
analysis, and a batch of review, projection and deploy-verification fixes. **Baked
write-path changes** (#845, #846, library#101, the shared alias rule): rolling this
release needs a gateway image rebuild, not a stage-only deploy.

### Write path and identity
- An alias must be a single name; an actor page must name its actor; a tombstone
  retires a page onto the same identity (okengine#845, okengine#846, library#101).
- One alias rule for every writer, deterministic lanes included, and names a review
  removed stay out on re-import.
- Corpus audit treats names that differ only in spacing as one identity (library#99).
- Autoverify never leaves a page whose stamped basis contradicts its own
  `reliability`/`credibility` grade.
- Generated structural artifacts are exempt from the collision guard (okengine#836).
- A pack lane that seats pages by the drain's base key fails (okengine#818).

### Cron fleet
- Deploy and the cron-plus scheduler share one job-store lock; post-deploy drift
  compares only the fields the deploy wrote (okengine#850).
- Every prompt's write-tool name derives from the lane binding; regen fails on a
  stale hardcoded writer name (okengine#852).
- Jobs compare by source, not by a prompt resolved from a file (okengine#814).
- A `wakeAgent=false` run owes no write receipt; a run record whose worker died is
  closed out; the stale-run probe is scoped to receipts (okengine#817, okengine#835).
- A lane that stays red re-alerts, and the cockpit shows how long the oldest lane
  has been red (okengine#817).
- Clamped MCP tool names are recognized in write telemetry, and derived write-server
  names are bounded so none clamp (okengine#840).

### Inquiry and lacuna
- `inquiry-analyze` answers standing questions and reevaluation indexes assessments;
  collected evidence is attributed to the inquiry term that found it; an inquiry's
  declared fields steer lacuna; inquiry stays opt-in and gets a step budget that
  covers its evidence (okengine#854).
- Lacuna field density counts evidence, not derived views (okengine#855).

### Projection, review and deploy
- The projection re-projects when the corpus changes, not only hourly
  (okengine#851); paginated INDEX pages are structure, not content (okengine#856).
- A pre-verify reconcile that loses a write race is a WARN, not a failed bring-up
  (okengine#853); the host-side policy digest ignores the container `.env`, and the
  projection timeout is configurable.
- Review records join by a reshard-proof, vault-relative subject key; unreachable
  review records are reconciled; the review vocabulary and registry coverage are
  explained where the decision is made (okengine#822, okengine#832, okengine#842,
  okengine#823).
- Stamp-key stripping consumes continuation lines and unindented block-sequence items
  (okengine#833); a curated-guard restore no longer duplicates its comment.
- Rollback never runs against a snapshot that can restore nothing (okengine#819).
- `install-domain` adopts a pack's own undeclared namespace with a WARN instead of
  refusing it, and counts a co-installed guest's ownership (okengine#812, okengine#816).
- `install-domain --accept-trust-exposure --reason "<why>"` records a reviewable
  acceptance of serving a more-private guest at the host's trust, keyed to both
  trust values and reported by `framework validate` (okengine#813).
- A vault's runtime state is no longer tracked in the engine repo (okengine#847).

### Merged after this section was drafted, included in the tag (okengine#861)
- The daily-brief lane's step budget covers every source its digest lists.
- Private pack names are gone from engine comments and test fixtures; the public-snapshot guard
  had rejected four of them.
- The mutation debt accumulated since v0.14.5 is paid down: 19 modules re-measured over the
  release range, each surviving mutant killed by a test or recorded as equivalent. The cron-plus
  run-record sweep now skips an undecodable record (`ValueError`, which covers `JSONDecodeError`
  and `UnicodeDecodeError`) instead of crashing.

### Release process
- private CI-only qualification with explicit publication approval; the public snapshot
  no longer leaks control-plane files; the public GitHub CI workflow and badge are
  restored.
- A test catches a method that has drifted out of its class.

## v0.14.5

- Preserve co-installed namespace tier contracts on minimal hosts, keep re-apply
  idempotent, and retain complete failed co-install diagnostics in runner logs.
- Strengthen output-size boundary tests for the DeepSeek writer repair (#803).
- Preserve existing partitioning, permission, and tier contracts when co-installing
  into inline or block YAML mappings. DeepSeek prevention shares the migration's
  active-file inventory and checks environment and extension model selections.

PATCH — preserve the file-requirement tool surface across Hermes startup import
ordering, distinguish ordinary unavailable prerequisites from broken check
contracts, and publish profile-scoped, size-bounded named diagnostics that
expire when stale and clear when the same scoped check recovers. Unscoped checks
emit the named signal without sharing retained state. Unexpected terminal and
code-execution backend probe exceptions now reach that classifier; malformed
operator configuration, fail-closed secret scoping, policy refusal, and
documented missing dependencies remain ordinary unavailable prerequisites.

DeepSeek V4.1 Flash scheduled writers now keep selected local and MCP tools,
including shortened governed-writer names, directly visible during cron runs
instead of routing them through the model's unreliable multi-local bridge.
The compatibility scope is provider-, model-, and conversation-qualified so
other models and sessions retain progressive tool disclosure. Malformed bridge
calls remain fail-closed, acquirer-signal writes have a bounded body, and the
release procedure now qualifies model-writing changes with two distinct
disposable inputs and client-visible readback (okengine#803).

## v0.14.4

PATCH — restore governed-writer startup with Hermes MCP 2.x while retaining the
legacy FastMCP import path, and make the packaged-dependency system-Python test
import OKEngine from the source layout under editable installs so the gated public
snapshot and GitHub CI exercise PyYAML bootstrapping without depending on ambient
interpreter state.

## v0.14.3

PATCH — repair installed policy discovery and host-side runtime-path verification,
and reconcile the PostgreSQL projection before immediate post-deploy verification.

## v0.14.2

PATCH — make deploy-time schema recomposition import the source-layout OKEngine
package on application hosts, preventing a post-migration abort before rebuild.

## v0.14.1

PATCH — repair the v0.14.0 deployment upgrade gate for installed first-party
schema extensions. Legacy inquiry partition/tier declarations now compose with
owner scoping, auxiliary JSON result schemas are not mistaken for OKF ownership
fragments, and CI verifies every shipped first-party schema fragment composes.

## v0.14.0

MINOR — upgrade Hermes Agent from v0.18.2 (`v2026.7.7.2`) to v0.21.3
(`v2026.9.14`) and include the prepared, unreleased v0.13.9 corrections. These
ship together to avoid a redundant intermediate fleet rebuild. The release
preserves current Qwen Coder Responses routing, the affected domain-pack
delegation and curator limits, every deployment's current SOUL voice, and the
cron-plus pin.

This is also the first tagged release of the v0.13.8-era changes below, plus corrections
merged after four deployments were built from the untagged `c13f13ad` revision.
The fleet's existing `v0.13.8` image labels are not a Git tag or proof of a
uniform release. Deployment requires a gateway
rebuild/recreate for baked reader/MCP/write-path fixes, in addition to staging
the cron scripts. See #758 and #759 for qualification and per-deployment verification.

- **Hermes v0.21.3 port (okengine#758, okengine#759).** The exact upstream tag
  and peeled SHA are pinned atomically. Twenty-two original patch IDs are ported
  in 21 ordered production artifacts; patch 05 is retired in favor of native
  delegated-session finalization with a retained negative contract. Custom and
  Serper overlays follow the v0.21.3 contracts, while OpenRouter remains native.
  Patch 17 deliberately preserves the existing forced `/v1/responses` route for
  custom Qwen Coder models, including when an explicit Chat mode is present.

- **`observability-validation` can pass on a live deployment (okengine#757).** The lane had never
  passed anywhere: it required fleet-health's search snapshot to equal a qmd artifact the read-MCP
  republishes on every call, rejected the artifact's real no-samples shape (percentile keys absent),
  and did not know fleet-health's `undetectable` bucket. Fleet-health also filed every passing
  `deployment-validate` as UNDETECTABLE — a silent `no_agent` run persists no stdout — and read the
  artifact twice, so its dashboard and sidecar could describe different versions. Fleet-health now
  reads the telemetry once and takes a silent run's verdict from the report that run wrote; the
  validator compares the dashboard to its own snapshot, requires exact agreement with the artifact
  only while the artifact is unchanged since the run (counters may only grow after), accepts
  unsampled buckets, and shares one bucket list with fleet-health. A producer→consumer contract test
  drives the real writers. The two scripts can be staged, but this release also
  includes baked-surface changes and therefore requires an image rebuild.
- **Lacuna soft-prediction links require an accepted prediction write (okengine#760).** The
  first-party Lacuna workflow now creates a standalone inference before an optional prediction,
  then adds `prediction_candidate` only after the prediction create is accepted. The write
  contract rejects links to absent targets, and separate receipts distinguish accepted Lacuna
  and accepted or rejected prediction artifacts. Real writer-boundary tests cover both accepted
  and rejected prediction paths. This release does not by itself remediate the already-dangling
  Strat Intel page; that data repair needs its own authorized write workflow.
- **Invariant-audit corrections landed after the first four image builds.** The
  `d40ecf32` merge contains fixes and regression tests for reader paths,
  write-policy enforcement, cron/index/backup/upgrade behavior, and version
  contracts; these bits were absent from the untagged `c13f13ad` images.
- **Exact-SHA adversarial reverify corrections.** Converge now authorizes the resolved target type
  and actual changed/removed fields; pack migration checkpoints cannot skip same-release siblings;
  review-write image provenance covers the complete baked package and wheel runtime members;
  taxonomy preflight checks pack-owned namespaces even when host additions contain only types;
  reader namespace exclusions use the composed schema without confusing nested folder names;
  producer-lane attribution survives later narrow job edits; and mixed out-of-vocabulary reliability
  evidence remains visible. Sidecar overrides are attached only by their scheduled `run --rm`
  wrappers, never the default deploy project. Negative fixtures cover each repaired boundary.
- **CI reliability gates.** Registry reachability is checked independently of
  earlier stages (#751); the security suite has its own deadline and the
  Makefile gates have explicit budgets. CI evidence remains private CI pipeline
  jobs, not local deployment evidence.
- **Retained deadline limitation (invariant-audit waiver, #779).** Cron-plus publishes and
  enforces each run deadline, but model-producing lanes do not yet use that signal to stop early
  with a partial-success receipt. A hard timeout can therefore still discard an over-budget run.
  Wiring this safely requires cross-lane selection, iteration, and receipt changes plus scheduled
  runtime evidence; it is explicitly deferred from this integrity patch and remains a gate for the
  Hermes v0.14.0 rollout. This release does not claim graceful deadline degradation.
- **Low-severity Wardley vocabulary residual (#778).** Invalid evolution values are still grouped
  with unenriched concepts by the optional Wardley-map extension. The follow-up will establish one
  producer/schema/consumer vocabulary and separate missing from invalid values; no v0.13.9 core
  write or repair path relies on that visualization.

## v0.13.8

PATCH — security, integrity, runtime-performance, and analysis-routing fixes accumulated since
v0.13.7. No schema-breaking change. Packs on v0.13.7 re-stamp after validation.

- **Read-MCP graph tools are vault-contained (okengine#660).** `_resolve_key()` fell back to a bare `(WIKI / target).is_file()`, so `retrieve_context("../CLAUDE")` served the vault-root persona file and a symlink inside `wiki/` read anything on the host; `find_references` took the same route. The on-disk fallback and the page read now go through `_safe()` exactly as `get_page` does, and the key handed back is the canonical wiki-relative path. Read-MCP image rebuild.
- **Cockpit markdown is sanitized (okengine#659).** The cockpit rendered agent- and feed-derived page bodies straight into `innerHTML` with no `nh3` pass (it shipped the dependency and never imported it), so a `<script>` tag, an `onerror=` handler, or a `javascript:` href in any ingested page executed in every viewer's browser on a `trust:public` deployment. The nh3 allowlist and the panel-svg stash now live in the wheel (`okengine.html_sanitize`) and BOTH read surfaces call it, so the reader and the cockpit cannot drift apart again. Cockpit and reader image rebuild.
- **Human review decisions are no longer MCP tools (okengine#661).** `resolve_review` and `assign_review` sat on the default `okengine-write` server that 12 of 14 agent crons and api_server chat carry, with a free-text `reviewer` and an `admin` stdio caller, so a lane could clear its own `needs_review` flags with human-looking approvals. Humans reach the same functions through the review-only HTTP sidecar and `framework review`; the MCP surface keeps only `record_machine_review`, which cannot clear human-required state. Gateway image rebuild (baked write path).
- **Converge ownership keys on the deployment's pack identity (okengine#662).** `_converge` did `pack = pack or _prov_pack()`, so the tool argument won over the deployment-pinned `OKENGINE_PACK` and any lane with `converge_entity` could overwrite another pack's owned fields with "0 conflicts" by naming the owner. The deployment identity now governs whenever it is set; a differing argument is refused (a legacy deploy without the env keeps honouring the argument). Gateway image rebuild (baked write path).
- **The dedup drain is tombstone-aware (okengine#663).** `dedup_partition_collisions` grouped every same-slug copy with no `status` filter and let the deepest path win every scalar, so a live page plus a deeper tombstone merged into a *tombstoned* survivor with the tombstone's `id` and no review flag, and two live copies disagreeing on `status: live` vs `retracted` were resolved by depth silently. Now only live copies merge; a tombstone whose `superseded_by` names the survivor is retired with its links rewritten (the call `build_map` defers to this pass), a tombstone pointing elsewhere is left in place and reported, and live copies that disagree on any non-volatile scalar are merged with `needs_review: true` and the fields named. `okf_migrate.find_page`/`write_key` prefer a live copy over a tombstone at any depth (`desired_key` exposes the strategy's seat). Stage-only (`deploy-cron-scripts.sh`); `okf_migrate.py` is also baked for the write path, so a gateway rebuild picks up the `find_page` change there.
- **Per-lane MCP tool allowlists are pack config (okengine#664).** `write_server` carried a private deployment's cron name in its actor→tools table, baked into the write path and caught by the publish guard. A pack lane now declares `write_tools: [...]` on its cron def; ensure-runtime forwards it as `OKENGINE_WRITE_TOOLS` on that lane's server-bound writer and the server prunes to exactly that set. The engine table keeps engine lanes only. Bare private product names were scrubbed from engine comments and the publish guard now matches the bare form. Gateway image rebuild (baked write path); a pack that relied on the removed entry must add `write_tools` to its lane before its next roll.
- **Secret files are owner-only; `--fix-perms` no longer opens them (okengine#665).** `.env` is created under umask 077 and tightened to 0600 by both `deploy.sh` and `ensure-runtime.sh` (INSTALL promised 600; nothing set it). `ensure-runtime.sh --fix-perms` used to `chmod -R a+rwX` the whole pack, making `.env` and the Bearer token in `.hermes-data/config.yaml` world-readable while reporting only world-writable; it now opens only the trees the container writes (`.hermes-data`, `wiki`, `raw`, `.okengine`), keeps `.env` 0600 and names `config.yaml` as world-readable in its output. `post_deploy_verify.sh` FAILs a group/other-readable `.env` and WARNs on a world-readable `config.yaml`.
- **The per-tool MCP fence costs O(pages touched), not O(vault) (okengine#666).** `corpus_transaction.mutation` SHA-256'd every wiki page before and after each write-tool call, under the exclusive corpus lock: 1.3 s per no-op call at 20k pages, 3.5 s at 60k, on rejected calls too — the id-index first-write timeout and the #650 lock window re-created at the fence. `mutation()` gains `tracking="touched"`: the write services declare each page they mutate with `corpus_transaction.touch()`, which records that one page's before-digest into the active marker, and the receipt is built from the touched set; a killed writer's recovery replays the same set. Batch writers (operations, reconciliation) keep the default full snapshot. Gateway image rebuild (baked write path).
- **Retire request-triggered IWE graph rebuilds (okengine#651).** The bounded backlink scanner and
  `wiki/.backlinks.json` are now the authoritative graph implementation. MCP graph calls return
  explicit unavailable/not-found-or-ambiguous results when evidence cannot answer a query; typos,
  new slugs, stale artifacts, and corrupt artifacts can no longer launch a multi-gigabyte
  whole-corpus subprocess. The IWE binary and wrapper are removed from runtime images and staging.
- **`deploy.sh` reports whether the deployment verified (okengine#597).** It exited `0` whether or
  not `post_deploy_verify.sh` passed, so a fleet roll, a CI job, or `deploy.sh && <next step>` could
  not distinguish a working deployment from one that came up with failing checks — both print
  `==> done` and differ only in the sentence after it. Now `0` = up and verified, **`3` = up but
  checks reported issues**, `1` = bring-up failed, `2` = usage error. Behaviour is otherwise
  unchanged: the stack still comes up and the remediation still prints, because aborting a bring-up
  over a gateway that has not finished binding would be worse. `OKENGINE_VERIFY_DELAY` (default 5)
  now controls the settle before probing.
- **Behavioural tests for the two scripts that gate deployment (okengine#602).** `deploy.sh` and
  `post_deploy_verify.sh` were guarded almost entirely by grepping their own source, which cannot
  tell "the check is gone" from "the check moved" and cannot see whether a branch runs at all. Both
  now execute for real against stub `docker`/`git` and a scratch tree. The verifier's tally is
  asserted to count exactly the verdicts it printed — an operator reads the verdicts, a script reads
  the exit status, and nothing checked that those two agree.
- PostgreSQL projection is now part of the standard deployment lifecycle: `deploy.sh` generates
  distinct per-deployment credentials, upgrades older deployments through a generated Compose
  overlay, starts the projector by default, and verifies health, file-count conformance, and a
  typed MCP query before reporting the deployment healthy.

- **Optional PostgreSQL structured-query projection (#566, #569–#574).** Large deployments can
  compile schema-selected Markdown pages and wikilinks into a disposable epoch/digest read model,
  query it through freshness-enforced typed MCP tools with complete coverage receipts, and prove
  recovery through health and scratch-schema reproducibility checks. The projector and PostgreSQL
  services are opt-in; Markdown remains canonical, qmd remains lexical discovery, and the database
  reader is structurally read-only. The implementation incorporates lessons from the measured reference deployment
  deployment lessons: UTF-8 initdb enforcement, frontmatter links, resolution provenance,
  explicit unreadable/skip accounting, dual deletion guards, real-PostgreSQL CI, typed date binds,
  loop-aware pools, alarms that are fault-tested, and refusal to serve stale results.

## v0.13.7

PATCH — rollout follow-up for v0.13.6. No schema or runtime-image behavior changes outside the
upgrade and repair tools.

- **Symlink-safe upgrade snapshots (#441).** `framework upgrade` preserves file symlinks without
  following dangling or external targets, so ephemeral browser singleton links cannot disable the
  rollback-protected upgrade path.
- **Syntax-only repair classification (#442).** Valid typeless frontmatter remains the schema
  classifier's responsibility and is no longer mislabeled as broken YAML; transformed repair
  candidates still require a valid `type` before they can be written.

## v0.13.6

PATCH — receipted operations framework, operator diagnostics, and a broad data-integrity hardening
pass. No schema-breaking change. Packs on v0.13.5 re-stamp after validation.

- **Receipted operations and operator CLI (#401–#409).** Adds validated operation manifests,
  run IDs, locks, input digests, durable receipts, runner integration, stable job management,
  status/doctor diagnostics, guarded audit/repair, ingest/derived rebuild families, explainability,
  and evidence exports.
- **Repair and reconciliation convergence (#412, #413, #416, #423–#427, #431–#438).** Repaired
  sources re-enter downstream synthesis; entity reconciliation is receipt-backed; dedupe backlogs
  converge; raw/source-quality/model-write receipts are complete and attributable; fleet health
  recognizes terminal outcomes; reference repairs are bounded and audited; malformed source
  shapes, glued delimiters, receipt identity inversion, and repeated RSS-suffix corruption are
  detected or deterministically repaired. Closing fences now match complete lines so suffix
  fragments cannot masquerade as valid delimiters.
- **Runtime and UI hardening (#410, #411, #414, #418–#421, #428–#430).** Bounds and cancels
  saturated qmd work; preserves runner PID ownership and host provenance; reconciles model-write
  receipts; preserves extension namespace partitioning; improves Cockpit layout; ships the branded
  favicon; includes the review write runtime in images; and embeds the source-quality rubric in
  rolled packs.
- **Release safety and documentation.** Merge-request gates are blocking, carried cron patches are
  validated, operator boundaries are documented, and the executive architecture guide and rollout
  policy define release pins as the compatibility contract.

## v0.13.5

PATCH — cron-plus scheduler pin bump (DST fall-back double-fire fix) + DST-window detector. Packs on
v0.13.4 re-stamp. No engine code changed; the image is re-baked to capture the new pinned dependency +
version marker (no baked-code delta from v0.13.4).

- **cron-plus DST fall-back double-fire (#326 [28]).** The pinned scheduler dependency is bumped
  `ee6d9f18 → bdb6cf5f` (jalewis/hermes-cron-plus#5): on a DST fall-back a repeated wall-clock hour
  made `croniter` return the same local time one UTC-hour later, re-firing a slot whose set includes
  the repeated hour (e.g. `0 1-23/2 * * *` fires at 01:00 EDT then again at 01:00 EST). The scheduler
  now skips the fold. All 4 carried cron-plus patches verified against the new pin.
- **DST-window detector.** `_fixed_hours` now expands ranges + steps (`1-23/2`, `*/2`), and the engine
  DST-window guard is refined to flag only low-frequency (≤4×/day) crons — the every-2h maintenance
  lanes structurally span the window and are now scheduler-protected.

## v0.13.4

PATCH — #326 invariant-audit burn-down + #349 parser fixtures + MCP/cron contract clarifications. No
breaking changes; packs on v0.13.3 re-stamp. **BAKED** (gateway + read-MCP image rebuild + roll): the
qmd change-detector fix ([30]).

- **#326 invariant-audit burn-down (17 findings):** [1] hardened-posture host-side peer in
  `post_deploy_verify.sh` (reuses `hardening_lib.hardened_posture_violations`); [9] `resolve_hermes_uid/gid`
  rejects `HERMES_UID=0` from env + .env (root short-circuit, #185); [11] atomic `install-domain` with
  transaction rollback; [14] `check_ownership` scans `.okengine`; [15] baked-vs-staged drift check for
  `tools/schema_validator.py`; [16] positioning-battle-cards same-day mtime staleness tiebreak; [24]
  prediction-schema-drain reuses the deterministic `_horizon_for`; [26] `framework backup` excludes
  cron-plus `pids/`+`.tick.lock`; [29] `check_timezone` detects a `next_run_at` computed under a stale
  TZ **value**; **[30] (BAKED)** the qmd change-detector (`_vault_max_mtime`) now includes directory
  mtimes so a reshelve/reshard `os.rename` (which preserves the file mtime) is caught instead of going
  stale until the 6h full refresh; plus the earlier merged [7]/[10]/[17]/[19]/[21]/[23]/[27]/[31].
- **#349** — line-anchored front-matter parser regression fixtures (URLs with `---`, YAML comments,
  body rules) pinning the write path against inline-`---` truncation.
- **#398/#399** — MCP resource + cron trigger contract clarifications.

Every finding ships a red-proven regression test or a standing detector.

## v0.13.3

PATCH — model-write concurrency/retry hardening + repair-receipt drains + detector burndown. No
breaking changes; packs on v0.13.2 re-stamp. **BAKED** (gateway image rebuild + roll): `write_server`
MCP-scoping + model-writer isolation from native file mutation.

- **Model-write concurrency & retry hardening** (#397 and siblings): review flags made replay-safe;
  model slot ownership reconciled; endpoint HTTP retry contracts enforced; shared model inference
  serialized; model writers isolated from native file mutation.
- **Repair-receipt drains**: unterminated receipt fences recovered; identity-matching receipts
  recovered; exact repair-receipt template provided; historical repairs bounded to explicit raw
  evidence and checkpointed; receipt-drain / repair-executor / MCP scoping fixes.
- **cron-plus managed patches** refreshed from a clean pin; whole-number patch-count test.
- **#326 / #352 detector burndown**: audit findings persisted run-over-run to measure recurrence
  (#352); dead-field registry + cockpit sharded-scan coverage + default-token sync + un-ignored
  post-deploy verify; hot-set / scrub / coinstall-preflight / predictions-vocab now surface
  silently-dropped values instead of no-op (#326 [7],[10],[17],[19],[23],[27],[31]).

## v0.13.2

PATCH — model-write output contracts + deploy self-healing. No breaking changes; packs on v0.13.1
re-stamp. The write-path enforcement is BAKED (gateway image rebuild + roll).

- **Model-write output contracts (BAKED, report mode by default).** Each cron lane can declare an
  `output_contract` (allowed namespaces/types/operations, required fields, body minimums, resolved
  wikilinks/relationships, unknown-field/placeholder policy), stamped into `cron-plus-jobs.json` with
  a digest. `okengine-mcp/output_contract_enforce.py` evaluates each server-authenticated lane write
  against its contract in the write path. `OKENGINE_OUTPUT_CONTRACT_MODE` gates the action: default
  `report` LOGS violations (write proceeds); `enforce` REJECTS. Lane prompts were aligned to declare
  and conform to their contracts (okengine#354/#355/#362/#365).
- **Model-write audit + run receipts.** A `model_write_audit` lane governs model-write violations;
  a cron-plus patch (`run-receipts`) exposes runner-owned receipt identity so a write's authoring
  lane is attributable (#357/#358/#360/#361).
- **Staged ingestion before entity synthesis** — source ingest is staged/contracted ahead of entity
  synthesis, and generated packs align to the staged contracted shape (#356/#364).
- **Deploy self-healing (#359).** `deploy.sh` reconciles a lagging engine pin via `framework upgrade`
  (roll-forward-gated) BEFORE validating, so a bump no longer dead-ends a lagging pack at
  `--skip-validate`; the `check_engine_version` FAIL points at `framework upgrade`.
- **owns-covers-schema is a WARN and honors `schema.exclude`** — an incomplete `owns` no longer hard-
  FAILs `framework validate` (the compose gate is the real collision enforcement), and a namespace
  clears by owning OR excluding it (the shared `dashboards`/`operational` render-tree convention).

## v0.13.1

PATCH — the pre-release invariant-audit remediation (okengine#351) plus the walk-up multipack
write-path correctness pass. No breaking changes; packs on v0.13.0 re-stamp.

- **Walk-up sub-domain write path (BAKED — requires a gateway image rebuild + roll).** A page at
  `wiki/<subdomain>/entities/<slug>` was written FLAT (the create path read the sub-domain *container*
  as the namespace) while the reshelve drain sharded it — re-opening the okengine#54 flat-vs-sharded
  duplicate-canonical ping-pong for every co-installed vault. `_partitioned_create_path` now keeps the
  sub-domain-qualified namespace so a sub-domain entity shards within its sub-domain, and the
  create-time alias/name dedup (`id_index` scan + `_alias_hits`) is sub-domain-aware AND scoped to a
  single sub-domain so two same-named entities in different sub-domains never cross-merge. A flat
  single-pack vault is byte-identical to v0.13.0.
- **Fail-loud hardening across the cron deploy gate** — `validate_ordering` gates the deployed
  (enabled) shape and rejects a non-string `after:`; `@morning:MM` out-of-range and an offpeak `a==b`
  window no longer silently misbehave.
- **Framework/extension validation gaps closed** — cross-tier duplicate-extension errors surface;
  non-rendering `two-axis` reader bindings are rejected; the vendored-`llm_lib` gate discovers copies
  dynamically; a `owns` ⊇ schema check; an unconditional engine-crons classification test; a
  `cost_bearing` discipline gate; and the write-path-lib baked-vs-staged drift check now runs at the
  deploy gate.
- **Robustness** — migration state is recorded incrementally (a hard-kill mid-batch no longer replays
  it); `resolve_hermes_gid` warns before its fallback and ownership scripts resolve gid independently.
- **Scoring signals** — source reliability/credibility grading is driven by the pack schema enum
  (out-of-vocab grades surfaced, not laundered to neutral); and an independent-corroboration producer
  replaces a never-written field that pinned 25% of source-evidence signal to a constant.

## v0.13.0

MINOR — a rapid corrective release folding the fixes and features that landed on `main` after the
v0.12.0 tag (both devs), plus a **version-integrity correction**: the `okengine-v0.12.0` gateway
image had been rebuilt with post-tag code, so the tag no longer identified its bits; v0.13.0
re-establishes tag ↔ code ↔ image agreement. No breaking engine changes; packs on v0.12.x re-stamp.

- **Write-path hardening (#348):** the enforced write path now rejects an entity write whose
  `sources:` uses the invalid singular `source/` namespace — the entity-backfill fabrication
  signature (hallucinated actor pages cited `source/<vendor>/<slug>` pages that never existed). Legacy
  refs already on a page are grandfathered on update.
- **Reshard correctness (#336):** `okf_migrate.make_path_rewriter` repoints BARE frontmatter path
  references (e.g. an assessment's `subject:`) on a reshard, not only `[[wikilinks]]` — fixes
  assessment subjects stranded when `entities/` buckets split a level deeper. New `corpus_audit`
  dangling-path-reference detector makes the class a standing dashboard row.
- **entity-resolve robustness (#348):** `normalize()` coerces non-string aliases — a numeric alias
  (e.g. a mis-imported `aliases: [10768]`) no longer crashes the whole canonical-assemble lane.
- **Assessments (#343–349):** attested operator-evidence resolver, qualification-result contract,
  candidate-lead-vs-evidence cockpit distinction; hide superseded assessments, exclude tombstoned
  subjects from rollups, surface stale references, and leaked-frontmatter detection with line-aware
  review parsing.
- **Publish / security:** scrub the internal org-name abbreviation on content (not only via
  file-exclusion); keep PRIVATE pack names and the private repo's private CI project ids out of the public
  snapshot, enforced by a dedicated private-pack publish guard.
- Pre-release invariant-audit backlog captured in #351 (candidates; no v0.13.0 regressions).

## v0.12.0

MINOR — the largest feature fold since the multipack/extension arc. No breaking engine changes;
packs on v0.11.x keep validating. Headline capabilities:

- **Threat-informed detection (TID) application (#286–#299):** the detection-engineering epic —
  versioned application profiles with safe inheritance and a `threat-informed-detection` profile,
  application role binding, atomic candidate evidence bundles, and cockpit surfaces for the
  threat-to-defense trace, coverage facets, detection dossier, validation queue, and gap workbench.
- **Review auto-verification (#313):** deterministic, Admiralty-graded clearing of `needs_review`
  (1×A or 2×distinct-publisher-B), with judgment types (assessment/proposition/prediction/hypothesis/
  forecast) never evidence-cleared. The MCP write path still forbids an agent clearing quarantine.
- **Collection fidelity + identity (#272–#275, #312, #314):** `web_capture` (content-addressed,
  append-only full-text capture), the declarative `source_connector` runtime + `collection_ledger`,
  identity-authority enrichment (`authority_enrich`, additive authority-ID stamping), and pack-version
  migrations auto-applied on `framework pull --update` / `install-domain`.
- **Composable operations layer (#325)** + the assessments local-evidence resolver.
- **CI test-category program (#59, #279–#282):** ruff/yamllint/shellcheck linters, Bandit/pip-audit
  SAST + secret-scan parity, the engine-wired library gate, composed-vault integration, and E2E render
  smoke on a dedicated dind runner.
- **Security & write-path hardening:** the `OKENGINE_HARDENED` fail-closed deployment profile (#78);
  `id`/`created`/provenance immutability on `update`/`patch`; the reserved-file and type-namespace
  guards extended to every mutating lane; converge's networked-write default-token fail-closed; and an
  indexed alias-dedup replacing the O(vault) per-create scan.
- **Deployment-timezone correctness (#301)**, the render-lint scaling series (#305–#311), predictions/
  corpus fixes, and ecosystem production-readiness (#300).
- **Internal tooling:** the invariant-audit redesign — a no-LLM deterministic pass plus Sonnet
  semantic finders (verify phase dropped for main-loop triage) with incremental `since`/`suppress`
  knobs (#334).

Every fix in the hardening waves followed the standing pattern: root cause + a validation at the
earliest gate + a red-proven regression test.

## v0.11.5

Correctness patch — the seven-issue backlog triaged after v0.11.4, in three fix waves. Every fix:
root cause + a validation at the earliest gate + a red-proven regression test.

- **feat(applications #247):** ship the first versioned application profile,
  `continuous-hypothesis`. Packs bind domain proposition classes, lifecycle fields, operations,
  work queues, views, and success measures through `.okengine/application.yaml`; `framework
  validate` rejects incoherent composition before runtime. A two-class synthetic fixture proves
  exact changed-evidence selection and preserves a reviewed reassessment-to-learning receipt.
- **Cockpit work surface (#187, #230–#233):** responsive compact navigation and accessible tabs/
  drilldowns; attention-first pack landing pages via reusable `doc-summary` cards; declared aliases
  merge legacy aggregate buckets while invalid numeric metrics degrade visibly; metric metadata now
  distinguishes shown records/groups from corpus totals and table columns support declared value
  labels; trend cards declare full-period, same-cutoff YTD, or partial/no-direction clocks; Ops now
  leads with evidence-linked backlog/ingest/validation metrics and sorted artifact freshness states.

- **fix(mcp #198):** `_run` starts helpers in their own process group and `killpg`s the tree on
  timeout — the orphaned-`iwe` leak (internal timeouts were cosmetic; grandchildren piled up) is
  red-proven dead.
- **fix(mcp #199):** `graph_stats` serves from the `wiki/.backlinks.json` artifact (meta + hubs +
  no-inbound count); live whole-graph IWE rebuilds remain only as the no-artifact fallback.
- **fix(pull #183):** a composed bundle keeps the BUNDLE's About identity (name/description/
  mission re-stamped after the host fetch; owns/ports stay the host's).
- **fix(schema #195):** the composed-schema artifact records its fragment INPUTS (`_fragments`)
  and `schema_lib.compose_schema` auto-loads them — the deploy-side and in-gateway composers of
  one contract now agree (kills the un-clearable "composed-schema STALE" false positive;
  conformance now sees extension-owned types).
- **fix(ingest #194):** the raw→wiki compile CARRIES ingest provenance (`source_feed` & co.) as a
  two-surface contract keyed off one constant (wake prompt + base `common_optional`,
  cross-checked in CI). Fleet data repaired: 5,156 source pages re-attached to their provenance.
- **fix(crons #197):** cron-plus (pin `6b230dc`→`ee6d9f1`) gains ownership-preserving writes (a
  root `docker exec` can no longer silently kill every lane) + a loud `.scheduler-stalled`
  sentinel; `deployment_validate` FAILs foreign-owned stores and the sentinel.
- **fix(skeleton #208):** the documented bare-compose sequence seeds the MCP token
  (ensure-runtime) before `up` — live-confirmed up-in-1s with a token, crash-loop control intact.

Deploy surfaces: gateway image (schema_lib — a baked write-path lib; watch the drift check) and
MCP image (server.py) both rebuild; cron-plus plugin updates to the new pin per deployment.

### Invariant-audit remediation (all 65 confirmed findings + doc audit okengine#209)

The pre-release invariant audit (checklist step 2b) surfaced **65 confirmed cross-surface findings**
(5 high · 36 medium · 24 low) — non-obvious traps where a value/invariant must agree across surfaces
and nothing enforced it. **All 65 were fixed in this release** (no deferral, no waiver), each with the
standing pattern: root cause + a validation at the earliest gate + a red-proven regression test.
Issue **okengine#209**'s documentation-drift audit was folded in. By subsystem:

- **Write path** (`okengine-mcp/write_server.py`): `extension_id` provenance made unforgeable across
  update/patch/converge; converge refuses an on-disk-tombstoned page; patch/append/converge now run
  the same drift + degeneration + dead-link review gate as create/update; flat `entities/<slug>`
  paths normalize to the shard canonical; malformed-YAML frontmatter is refused (not wiped);
  namespace permission keys fail closed; the `@mcp.tool` wrappers gained coverage.
- **Deploy/staging**: dirty-tree image staleness; config.yaml force-recreate; always-regenerate crons
  (never a stale/wrong-pack artifact) + pack-provenance guard; deterministic jitter seed; validate the
  staged-script set before overwriting the live store; reconcile staged fossils; no vacuous ownership PASS.
- **Framework CLI**: consistent SQLite backups (online-backup API) + symlink warnings; upgrade snapshot
  disk-check + partial cleanup; rollback quarantines instead of deleting; yaml-free manifest pin read;
  enabled-extension resolve gate; disable/enable compose the effective set + fail loud; port-collision
  preflight.
- **Cron scripts**: dir-ownership + fossil detection; offpeak/okf/tier silent-degradation guards; list/
  semver crash guards; schema-driven namespaces; `budget_guard` pauses `cost_bearing` no_agent lanes;
  `cron_pack_split` routes pack-marked dotted names to domain.
- **Extensions + reader/cockpit**: mtime-based delta detection; non-string alias tolerance; unimplemented
  panel kind dropped; reader chat honors the budget trip (shared-vault marker); cockpit `today_prefix`
  in UTC; MCP index-refresh env forwarded; manual bring-up docs use `--build`.
- **Constants/pins**: `new-pack.sh`/`apply.sh` read the pin from the manifest; patch-count + pyproject
  version bound by test; `page-quality-enrich` moved off the morning stagger; `health_export` gained an
  external dead-man's-switch heartbeat.
- **CI/publish/scrub gates**: mcp install hard-fails + verifies import; the pre-commit scrub scans the
  whole tracked tree; publish/CI test parity; Makefile target guards the excluded script; the three
  vendored `llm_lib.py` copies are pinned byte-identical.

## v0.11.4

Patch: the fixes + install-experience work accumulated after the v0.11.3 tag, re-aligning the
version with what actually ships (the public tag had been re-pointed across three snapshot
publishes; this names that content).

- **fix(cockpit):** `sort.then:` secondary key for dataset tables — coarse-date ties (50–70 actors
  on one annual-report date) rank by activity instead of glob order (!423).
- **fix(cockpit):** exported chat reports strip the agent's progress-narration ("Checking the
  vault…", "Found the page…", "Based on the vault…") — live chat keeps it, md/docx/pdf don't (!424).
- **fix(install-domain):** a cockpit-bearing guest onto a host with no `cockpit:` block SEEDS the
  block instead of asserting — un-crashes every coinstall of a cockpit guest onto a bare scaffold
  host (5 deploy-matrix cells) (!426).
- **fix(deploy):** an .env with no pinned HERMES_UID made deploy.sh die instantly and silently
  (`set -euo pipefail` + no-match `$(grep …)`, 5th instance of the class) — the documented
  from-scratch flow was broken for exactly its target users. Guarded, plus a class-gate test
  scanning every set -e script (!430).
- **docs:** INSTALL.md is a paste-able five-step quickstart ending in a POPULATED system (feeds
  activation — packs ship inert; model key before deploy; `deploy.sh --kickstart`; the reader/
  cockpit URLs + LAN exposure); release-checklist gains the cross-repo library pack-pin re-stamp
  step (!425, !427–!429).

**Invariant-audit waiver (checklist 2b):** scoped waiver for this patch — every code fix above
shipped through its own adversarial re-verify (2c pattern) this cycle and the deploy fix was
caught/validated by a live clean-host kickstart install; no new cross-surface features landed.

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
- **Fresh-host cache init (reader + cockpit).** Five `monotonic()`-TTL caches across
  `okengine-reader/app.py` (`_OBS_INDEX_CACHE`, `_SRC_REL_CACHE`) and `okengine-cockpit/app.py`
  (`_SRC_REL_CACHE`, `_OBS_INDEX_CACHE`, `_TYPE_REQ_CACHE`) initialized to `(0.0, {})` instead of
  `-inf`. `monotonic()` is seconds-since-boot, so on a freshly-started container (or a CI runner)
  `now - 0.0 < _DIR_TTL` read the EMPTY initial entry as "fresh" and served a blank canonical→source
  drill-down, blank Admiralty reliability grades, and blank page-quality badges for up to 15 minutes
  after every start (the sibling caches already used `-inf` per the documented note; these regressed).
  Caught by GitHub CI's low-uptime runners after the release. A cross-app guard test now forbids any
  finite-timestamp / empty-payload TTL cache in either app.
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
- Remaining audit mediums (30) + lows (33) are tracked in private CI issues #203 / #202 with per-item
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
  twice; one domain pack had a pack-level workaround, now retired). Freshness of an engine-generated
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
  set; the internal-tracker (`private CI #NN`) reference class added; a contract test pins the
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
