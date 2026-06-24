# Technical spec: extension runtime composition

**Status:** draft technical spec
**Builds on:** `plugin-system-prd.md`, `extension-manifest-spec.md`
**Related issues:** #63, #109, #113, #90
**Primary implementation targets:** `scripts/framework.py`,
`scripts/extension_meta.py`, `scripts/extension_registry.py`,
`scripts/cron_pack_split.py`, `scripts/deploy.sh`

## 1. Purpose

Define how OKEngine discovers extensions, tracks enablement state, composes
enabled extension contributions, and integrates them into deploy.

The core invariant:

```text
present on disk != enabled

Only enabled extensions affect runtime.
Only generated files are deployed.
Generated files are reproducible from engine + pack + enabled extension state.
```

## 2. Directory Layout

V1 should standardize on one extension root:

```text
<pack>/.okengine/extensions/
```

Each extension lives in a subdirectory named by extension id:

```text
<pack>/.okengine/extensions/okengine.contradictions/
  extension.yaml
```

Compatibility scan locations:

```text
<pack>/.okengine/plugins/
<pack>/.okengine/capabilities/
```

Rules:

- `.okengine/extensions/` is canonical.
- legacy/compatibility locations may be scanned but should warn.
- duplicate extension ids across directories are `FAIL`.
- a directory without `extension.yaml` is ignored with `WARN` in verbose mode.

## 3. Enabled-State File

Enabled state lives in the deployment/vault, not the extension package.

Path:

```text
<pack>/.okengine/extensions.yaml
```

Shape:

```yaml
extensions:
  okengine.contradictions:
    enabled: true
    config:
      include_resolved: true
  okengine.sec.hunting:
    enabled: false
```

Rules:

- absent state file means all discovered extensions are disabled;
- absent extension entry means disabled;
- unknown extension ids in state are `WARN`;
- malformed state file is `FAIL` for validate/deploy;
- config overrides must validate against the extension manifest config schema;
- state changes are explicit CLI writes.

## 4. Registry

The registry joins discovered manifests with enabled state.

Normalized registry entry:

```json
{
  "id": "okengine.contradictions",
  "kind": "capability",
  "version": "0.1.0",
  "trust": "declarative",
  "path": ".okengine/extensions/okengine.contradictions",
  "enabled": false,
  "valid": true,
  "findings": []
}
```

Registry ordering:

- sort by `id`;
- stable JSON for machine output;
- table for human output.

Registry API target:

```python
discover_extensions(pack_dir: Path) -> list[dict]
load_extension_state(pack_dir: Path) -> dict
build_extension_registry(pack_dir: Path) -> list[dict]
enabled_extensions(pack_dir: Path) -> list[dict]
```

## 5. CLI

Add `extensions` command to `scripts/framework.py`.

Commands:

```bash
python scripts/framework.py extensions list <pack> [--json]
python scripts/framework.py extensions validate <pack>
python scripts/framework.py extensions inspect <pack> <id> [--json]
python scripts/framework.py extensions enable <pack> <id>
python scripts/framework.py extensions disable <pack> <id>
```

Optional aliases can be added later:

```bash
python scripts/framework.py plugins ...
python scripts/framework.py capabilities ...
```

### `list`

Shows discovered extensions and state.

Example:

```text
id                         kind        version  state     valid  trust
okengine.contradictions    capability  0.1.0    disabled  yes    declarative
okengine.sec.hunting       capability  0.1.0    enabled   yes    local-script
```

Exit codes:

- `0`: listed successfully, even if some entries are invalid;
- `2`: invalid invocation.

### `validate`

Validates all discovered extensions and enabled-state.

Exit codes:

- `0`: no `FAIL`;
- `1`: at least one `FAIL`;
- `2`: invalid invocation.

### `inspect`

Shows one extension manifest, validation findings, contributions, permissions,
and enabled config.

Exit codes:

- `0`: extension found;
- `1`: extension not found or invalid when `--strict` is added later;
- `2`: invalid invocation.

### `enable`

Steps:

1. build registry;
2. fail if extension id is not discovered;
3. fail if manifest has `FAIL` findings;
4. resolve dependencies;
5. validate config defaults and overrides;
6. validate trust/permissions;
7. run dry-run composition;
8. print contribution and permission summary;
9. write enabled state;
10. regenerate generated config if supported in current phase.

Exit codes:

- `0`: enabled;
- `1`: validation/composition failed;
- `2`: invalid invocation.

### `disable`

Steps:

1. mark disabled in state;
2. regenerate generated config if supported in current phase;
3. print preserved content note.

Disable does not delete generated wiki pages.

## 6. Dependency Resolution

Dependencies come from manifest `requires`.

Checks:

- engine requirement against `engine-manifest.yaml`;
- pack requirements against installed pack metadata;
- extension requirements against registry.

Rules:

- enable fails if a required pack or extension is absent;
- deploy fails if an enabled extension has an unsatisfied dependency;
- disabling an extension fails if another enabled extension requires it, unless
  `--force` is added later and also disables dependents.

## 7. Composition Pipeline

Deploy-time composition order:

```text
1. load engine definitions
2. load active pack definitions
3. discover extensions
4. load enabled state
5. validate enabled extensions
6. compose schema contributions
7. compose cron contributions
8. compose reader contributions
9. write generated runtime files
10. start/reload services
```

If any enabled extension fails validation, deploy stops before writing generated
runtime files.

## 8. Generated Files

Generated files should be written under a clearly marked generated directory or
existing generated target.

Required v1 generated outputs:

```text
config/cron-plus-jobs.json
```

Likely generated outputs:

```text
<pack>/.okengine/generated/extensions-registry.json
<pack>/.okengine/generated/reader-nav.yaml
<pack>/.okengine/generated/extension-permissions.json
```

Rules:

- generated files are reproducible;
- generated files should include a header/comment when format supports it;
- do not require operators to edit generated files;
- generated files may be committed or ignored depending on deployment policy,
  but source of truth remains manifests + enabled state.

## 9. Cron Composition

Input sources:

```text
engine:
  config/engine-crons.json
pack:
  <pack>/crons/domain-crons.json
  <pack>/crons/engine-template-prompts.json
extensions:
  enabled extension contributes.crons files
```

Extension cron file shape:

```json
[
  {
    "name": "refresh",
    "enabled": true,
    "schedule": {"kind": "cron", "expr": "17 5 * * *"},
    "script": "select_contradictions.py",
    "prompt": "(unused - deterministic script)"
  }
]
```

Composition rules:

- local extension job names are scoped to the extension;
- active job id is `<extension-id>:<local-name>`;
- generated jobs are sorted by active job id;
- duplicate active job ids fail;
- extension disabled means no extension jobs are emitted;
- source cron files with disabled jobs remain source state, but disabled jobs
  are not emitted to active scheduler config, matching existing cron behavior;
- extension scripts must resolve to paths declared in `contributes.scripts`.

Script path resolution:

```text
extension local script: scripts/foo.py
runtime script id: <extension-id>/scripts/foo.py
```

Open implementation choice:

1. copy/sync enabled extension scripts into a generated runtime scripts dir; or
2. mount extension directories into the gateway and reference them directly.

V1 recommendation: copy/sync into:

```text
<pack>/.okengine/generated/extension-scripts/<extension-id>/
```

This gives a stable runtime path and prevents cron jobs from depending on
mutable source paths.

## 10. Schema Composition

Schema composition is constrained by the composable okpacks plan (#90).

V1 options:

### Option A: Validate Only

Allow extension schema fragments to be validated and listed, but not merged into
the active schema until base schema merge lands.

### Option B: Additive Merge

Allow additive type declarations when no owner conflict exists.

Rules for additive merge:

- new types only;
- new namespaces only;
- no mutation of existing type required fields;
- no global `okf.required`, `strict_types`, or engine-owned toggles;
- conflicts fail.

Recommendation: implement Option A first if schema merge is not ready; move to
Option B as part of capability packs.

## 11. Reader Composition

Reader contribution files are declarative YAML.

Shape:

```yaml
nav:
  - id: contradictions
    title: Contradictions
    kind: dashboard
    path: wiki/dashboards/contradictions.md
```

Composition rules:

- emitted reader id is `<extension-id>:<local-id>`;
- disabled extensions do not emit reader entries;
- missing dashboard target is allowed and should render empty state;
- duplicate emitted ids fail;
- reader code must not execute extension code in v1.

Generated reader nav target:

```text
<pack>/.okengine/generated/reader-nav.yaml
```

Reader integration options:

1. reader reads generated nav file directly;
2. deploy copies generated nav into reader static config;
3. reader API endpoint exposes generated nav.

V1 recommendation: reader reads generated nav file mounted from the pack.

## 12. Validator Composition

Validator contributions run during:

```bash
python scripts/framework.py validate <pack>
python scripts/framework.py extensions validate <pack>
```

V1 validator types:

- declarative validator YAML;
- local script validator under `local-script` trust.

Rules:

- validators must not mutate files;
- validators run with cwd at pack root;
- validator output must be machine-readable JSON lines or a documented simple
  JSON object;
- validator failure maps into `OK/WARN/FAIL`;
- validators requiring network/secrets must declare permissions.

Recommended v1 output:

```json
{"severity":"FAIL","check":"hunting.hunt.techniques","detail":"hunt missing techniques"}
```

## 13. Permission Summary

Enable and deploy should show effective permissions for enabled extensions.

Example:

```text
Extension okengine.sec.hunting requests:
  reads:
    - wiki/**
  writes:
    - wiki/hunts/**
    - wiki/dashboards/hunting-*.md
  network: false
  secrets: none
  delivery: false
```

Rules:

- permission summary is required for enable;
- deploy should print summary when enabled extensions changed since last
  generated config;
- secrets are named but values are never printed.

## 14. Deploy Integration

`scripts/deploy.sh` should call extension validation/composition before service
startup.

High-level sequence:

```text
ensure runtime
install cron-plus
validate pack
validate enabled extensions
compose cron/schema/reader extension outputs
docker compose up
post deploy verify
```

Failure behavior:

- invalid enabled extension stops deploy;
- invalid disabled extension warns but does not stop deploy;
- generated config write should be atomic: write temp then rename.

## 15. Tests

Required test groups:

- manifest parse/validate;
- state file parse/validate;
- registry discovery;
- duplicate id detection;
- enable/disable state changes;
- dependency checks;
- cron namespace composition;
- disabled plugin exclusion;
- reader nav composition;
- invalid enabled extension blocks deploy composition;
- invalid disabled extension does not block deploy.

## 16. Acceptance Criteria

- copied extension appears in `extensions list` as disabled;
- copied extension does not affect active cron config;
- `extensions enable` validates and records enabled state;
- enabled cron jobs appear in generated cron config with namespaced ids;
- `extensions disable` removes jobs from generated cron config;
- generated wiki pages are preserved on disable;
- invalid enabled extension blocks deploy;
- invalid disabled extension does not block deploy;
- generated outputs are deterministic;
- tests cover the runtime composition pipeline.

