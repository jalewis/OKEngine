# Extension lifecycle — enable / disable / compose

**Issue:** okengine#113 (+ script staging okengine#128) · **Gate:** okengine#131 ·
**Builds on:** okengine#134 (discovery) · **Parent design:**
[`extension-system.md`](extension-system.md) §9
**Status:** design — implemented (MVP)

How an extension goes from *present on disk* to *running*, and back. #134 delivered
discovery (the scanner + the three roots + the no-shadow rule); this is the mutating
half — enable/disable state and the **composer** that folds enabled extensions into
the generated cron fleet.

Several #113-era open questions are settled by #134 and recorded here so they aren't
re-litigated: there is **one `extensions/` tree** (not separate `plugins/`+
`capabilities/`), keyed by manifest `id`; enabled-state lives at
`<pack>/.okengine/extensions.yaml`; the verbs are `framework extensions
{list,inspect,validate,enable,disable}`.

## Invariants (§9)

- **present ≠ enabled** — a discovered extension has zero runtime effect until
  enabled. Enabled-state is vault-level, not in the package, so one package runs in
  many deployments.
- **generated-from-source** — the deployed `cron-plus-jobs.json` is regenerated from
  engine crons + pack crons + enabled-extension manifests. Never hand-edited.
- **namespaced** — a synthesized job's name is the extension `id` (globally unique by
  construction); a collision with an engine/pack/other-extension job is a hard FAIL.
- **fail-before-runtime** — an invalid *enabled* extension stops the regen/deploy
  before any generated file is written or service restarted.
- **preserve-content** — disable stops the operation and drops its job, but never
  deletes the pages it produced (those become orphaned — see #133; removal is the
  explicit `purge`, #127).

## Installer vs composer (the #113 split)

| Role | Responsibility | Where |
|---|---|---|
| **Installer** | put an extension on disk in a tier root; it then *appears* in `list` as present/disabled. Never edits generated config. | drop a dir under one of the three roots (#134 §3.1) |
| **Enable/disable** | validate manifest + dependencies, *dry-run* the composition, then write the enable bit to `extensions.yaml`. Reports whether a redeploy is needed. No content mutation. | `framework_extensions.py` → `extension_discovery.set_enabled` |
| **Composer** | read engine + pack + enabled extensions, synthesize + namespace + conflict-check extension jobs, and write the generated runtime config. | `extension_compose.py`, folded into `cron_pack_split.regen()` |

Enable/disable only manage *state*; the composer is the single generation path, so
active runtime state stays deterministic and regenerable.

## Flow

```
copy extension into a tier root        # installer; present ≠ enabled
  → framework extensions list          # appears: present
  → framework extensions enable <pack> <id>
        validate manifest + deps + dry-run compose   # fail-before-runtime
        write <pack>/.okengine/extensions.yaml
        "redeploy to apply"
  → deploy / regen                     # composer: engine + pack + enabled exts → cron-plus-jobs.json
  → framework extensions disable <pack> <id>
        remove the enable bit; redeploy drops its job; pages preserved
```

## Cron synthesis (MVP)

An enabled `operation` extension with a script entrypoint (`trust: in-gateway`)
synthesizes **one deterministic `no_agent` job** from its `operation` block:

```yaml
operation:
  schedule: {kind: cron, expr: "17 5 * * *"}
  entrypoint: {script: run.py}        # or a bare string: run.py
```

→

```json
{ "name": "<id>", "enabled": true, "schedule": {"kind":"cron","expr":"17 5 * * *"},
  "workdir": "/opt/vault", "script": "run.py", "no_agent": true, "deliver": "local",
  "enabled_toolsets": ["okengine", "okengine-write"] }
```

The job id is a deterministic hash of the extension id, so regeneration is stable
(reproducible-from-manifest). The job reads via the query MCP and writes via the
enforced `okengine-write` path (the §4 MCP-client contract).

## Script staging (#128)

The synthesized job's `script` is the **absolute, namespaced** path
`/opt/data/scripts/<id>/<basename>` (`extension_compose.SCRIPTS_ROOT`). Staging puts
the file there:

- **Copy, not mount.** `deploy-cron-scripts.sh` streams each enabled in-gateway
  extension's `*.py` **through the running gateway** (`docker exec … tar`, as
  `HERMES_UID`) into `/opt/data/scripts/<id>/` — the same mechanism, ownership, and
  container-only `/opt/data` constraints as the engine/pack cron scripts. No bind
  mount, no compose change. (OQ#4 closed: copy.)
- **Namespaced by subdir**, so two extensions' `run.py` can't collide and an
  extension's code stays isolated from the flat engine/pack scripts dir —
  reinforcing the §4 boundary (an extension is an MCP client, not an importer of
  engine cron libs). The absolute `script:` path matches the existing absolute-path
  precedent in `engine-crons.json`, so there is no ambiguity about how cron-plus
  resolves it.
- **Fail-loud before staging.** The deploy reads the plan from
  `framework extensions stage-plan <pack>` (one `<id>\t<dir>` line per enabled
  in-gateway op); a broken enabled set (dup id, missing dep) fails the plan and aborts
  the deploy before any file is staged. sidecar/non-operation extensions emit no plan
  line (they don't run from the gateway scripts dir).
- **Scope:** MVP stages each extension's top-level `*.py`. Non-`.py` runtime data and
  a `scripts/` subdir convention are a later enhancement.

## Scope boundaries (deferred, by dependency)

- **Sidecar extensions** (`trust: sidecar` / image entrypoint) now synthesize a
  **trigger cron job** + a generated compose service via `framework extensions
  sidecar-generate` (#135). Live container launch is operator-opt-in (needs a real
  sidecar image + a docker socket reachable from the gateway) — see
  [`sidecar-contract.md`](sidecar-contract.md).
- **N-way / multi-pack** extension composition is single-pack in MVP (the deployed
  `regen()` path); the N-way `compose()` path generalizes later alongside #90.
- **Agent-driven operations** — MVP operations are deterministic scripts; a
  prompt-bearing agent operation is a later extension of the manifest.

## Surfaces

- `scripts/extension_compose.py` — `synthesize_job` / `synthesize_jobs` / `compose` /
  `extension_jobs(pack_dir, existing_names)` (deploy entry point) / `staging_targets`
  (the #128 plan).
- `scripts/extension_discovery.py` — `load_enabled_state` / `set_enabled` /
  `resolve_enabled` (state) on top of the #134 scanner.
- `scripts/framework_extensions.py` — the `enable` / `disable` / `stage-plan` verbs.
- `scripts/cron_pack_split.py` — `regen()` folds `_extension_pass(...)` in (a no-op
  when no extension is enabled, so it's zero-impact on existing deployments).
- `scripts/deploy-cron-scripts.sh` — stages enabled in-gateway extension scripts into
  `/opt/data/scripts/<id>/` (#128).
