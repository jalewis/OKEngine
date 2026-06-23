# PRD: OKEngine plugin system

**Status:** draft PRD
**Related issues:** #63, #109, #113, #90, #112
**Scope:** plugin discovery, manifests, enable/disable state, composition, cron
integration, trust/permissions, and the first vertical slice.

## 1. Summary

OKEngine needs a formal plugin system so deployments can add importers,
validators, enrichment workflows, reader panels, scoring policies, delivery
hooks, and optional feature bundles without copying engine scripts or manually
editing generated runtime files.

The first version should be conservative:

- plugins are discovered automatically but never activated implicitly;
- only enabled plugins affect runtime;
- generated runtime config is reproducible from engine definitions, pack
  definitions, plugin manifests, and enabled state;
- v1 prefers declarative contributions over arbitrary in-process code;
- executable plugins must declare a higher trust level and explicit permissions.

This PRD treats "plugin" as the broad product surface. Internally, plugins have
different kinds: runtime plugins, capability plugins, importers, validators,
reader extensions, and scoring policies.

## 2. Problem

Today OKEngine has several de-facto extension points:

- pack `schema.yaml` and schema-driven validation;
- engine/domain cron split and merge;
- OPML-driven feed import;
- pack-supplied scripts and prompts;
- reader behavior derived from schema and dashboard files;
- Hermes runtime plugins and model-provider plugins.

These are useful but not yet a formal API. Adding new behavior often means
copying scripts, editing pack glue, or relying on conventions that are not
stable enough for third-party or optional feature distribution.

As OKEngine grows, always bundling every workflow into every deployment will
make installs heavier, more expensive, and harder to reason about. A deployment
should be able to start lean and explicitly enable capabilities such as
predictions, contradictions, watchlists, source staleness, threat hunting, or
alerting.

## 3. Goals

1. Make extension points explicit, validated, and documented.
2. Let operators install a plugin without enabling it.
3. Let operators enable/disable plugins safely.
4. Compose plugin cron jobs into the active cron fleet without silent
   collisions.
5. Compose declarative schema, reader, prompt, and validator contributions.
6. Surface permissions and trust before activation.
7. Preserve generated wiki content when a plugin is disabled.
8. Provide a small example plugin that proves the lifecycle end to end.

## 4. Non-goals

- No arbitrary untrusted Python import hooks in v1.
- No marketplace or remote registry in v1.
- No automatic activation when files appear on disk.
- No destructive cleanup on disable.
- No full sandboxing/signing model in v1.
- No cross-plugin conflict resolution beyond fail-loud validation.
- No support for mixing public and private trust boundaries in one composed
  deployment.

## 5. Personas

### Operator

Runs an OKEngine deployment. Wants to see what plugins are available, what they
will do, what permissions they need, what jobs they add, and whether enabling
them affects cost or secrets.

### Pack author

Builds domain packs. Wants reusable optional features without copying scripts
into every pack.

### Plugin author

Builds importers, dashboards, validators, or feature bundles. Wants a stable
manifest and testable contribution model.

### Engine maintainer

Needs extension behavior to remain deterministic, validate before deploy, and
avoid widening the trusted execution surface too quickly.

## 6. Product Principles

### Explicit Activation

Plugins may be copied into a known directory and discovered automatically, but
they must not affect runtime until explicitly enabled.

```text
present on disk != enabled
```

### Generated Runtime State

Runtime config is generated, not hand-edited.

```text
engine definitions
+ active pack definitions
+ enabled plugin contributions
= generated runtime config
```

### Namespaced Contributions

Plugin-contributed cron jobs, reader ids, prompt ids, and other public
contributions must be namespaced by plugin id.

```text
plugin job name: refresh
active job id: okengine.contradictions:refresh
```

### Fail Loud

Conflicts, missing dependencies, invalid manifests, and undeclared dangerous
permissions fail before deploy.

### Preserve Content

Disabling a plugin stops future work and hides UI contributions, but does not
delete generated wiki pages. Cleanup requires a separate explicit purge path.

## 7. Plugin Taxonomy

| Kind | Purpose | Examples |
|---|---|---|
| `capability` | Optional feature bundle | predictions, contradictions, watchlists |
| `runtime-plugin` | Platform/runtime mechanics | model provider, auth, delivery transport |
| `importer` | Pull or normalize external data | STIX/TAXII, Sigma, GitHub advisories |
| `validator` | Add validation checks | schema policy, content lint |
| `reader-extension` | Add reader nav/panels/views | dashboard tabs, review panels |
| `scoring-policy` | Add ranking/scoring logic | source decay, vuln priority |

This taxonomy is a product/API boundary. Implementation can share manifest
parsing and registry code across all kinds.

## 8. Plugin Anatomy

Recommended layout:

```text
okengine.contradictions/
  extension.yaml
  crons/
    jobs.json
  scripts/
    select_contradictions.py
  reader/
    nav.yaml
  docs/
    README.md
  tests/
    fixtures/
```

Required:

- manifest;
- id;
- kind;
- version;
- engine compatibility;
- trust level;
- contribution declarations;
- permission declarations.

Optional:

- schema fragments;
- cron jobs;
- prompts;
- scripts;
- reader navigation/panels;
- validators;
- fixtures;
- docs.

## 9. Manifest Contract

Initial manifest shape:

```yaml
id: okengine.contradictions
kind: capability
version: 0.1.0
name: Contradictions
description: Refreshes the wiki contradiction dashboard.

requires:
  engine: ">=0.2.0"
  packs: []
  plugins: []

trust: declarative

contributes:
  crons:
    - crons/jobs.json
  scripts:
    - scripts/select_contradictions.py
  schema: []
  prompts: []
  reader:
    - reader/nav.yaml
  validators: []

permissions:
  reads:
    - wiki/**
  writes:
    - wiki/dashboards/contradictions.md
  network: false
  secrets: []
  delivery: false

config:
  include_resolved:
    type: boolean
    default: true
    description: Include resolved contradictions in the dashboard.
```

### Required Manifest Fields

| Field | Requirement |
|---|---|
| `id` | globally unique, stable, reverse-DNS-ish or `okengine.*` |
| `kind` | one known kind |
| `version` | semver-like |
| `requires.engine` | compatibility expression |
| `trust` | one known trust level |
| `contributes` | mapping, empty allowed |
| `permissions` | mapping, explicit even when empty |

### Unknown Fields

Unknown descriptive fields may warn. Unknown fields under `contributes`,
`permissions`, or `lifecycle` should fail until the contract supports them.

## 10. Trust And Permissions

Trust levels:

| Trust | Meaning |
|---|---|
| `declarative` | Metadata/config only; no plugin-owned executable code |
| `local-script` | Runs local scripts under OKEngine cron/script execution |
| `trusted-runtime` | Loaded by runtime/plugin host |
| `network` | May make outbound network calls |
| `delivery` | May send outbound messages/notifications |

Rules:

- `network: true` requires trust `network` or higher.
- `delivery: true` requires trust `delivery`.
- non-empty `secrets` requires trust `network`, `delivery`, or
  `trusted-runtime`, depending on the plugin kind.
- install/enable must print a permission diff.
- deploy must fail if an enabled plugin's effective permissions exceed its trust.

V1 can enforce declarations and validation even if OS-level sandboxing is
deferred.

## 11. Discovery And Registry

Discovery is presence-based and deterministic.

Candidate directories:

```text
<pack>/.okengine/extensions/
<pack>/.okengine/plugins/
<pack>/.okengine/capabilities/
<pack>/.hermes-data/plugins/
```

Open design choice: keep separate `plugins/` and `capabilities/` directories, or
use one `extensions/` tree and distinguish by manifest `kind`. The unified
`extensions/` tree is cleaner long term.

Registry output should be stable sorted JSON:

```json
[
  {
    "id": "okengine.contradictions",
    "kind": "capability",
    "version": "0.1.0",
    "trust": "declarative",
    "path": ".okengine/extensions/okengine.contradictions",
    "enabled": false,
    "valid": true
  }
]
```

## 12. Enabled State

Enabled state belongs to the deployment/vault, not the plugin package.

Potential state file:

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
  okengine.watchlists:
    enabled: false
```

If a plugin is present on disk but absent from state, it is available and
disabled.

## 13. Lifecycle

### Install

Installer responsibilities:

- copy/fetch plugin files;
- validate manifest shape;
- check engine compatibility;
- record installed plugin if needed;
- optionally enable if the operator asks;
- never hand-edit generated runtime config directly.

### Discover

Discovery responsibilities:

- scan known directories;
- parse manifests;
- join discovered plugins with enabled-state;
- report valid/invalid status.

### Enable

Enable responsibilities:

- validate manifest;
- validate dependencies;
- validate trust and permissions;
- validate contribution conflicts;
- store enabled state;
- regenerate composed config;
- report whether redeploy/restart is needed.

### Disable

Disable responsibilities:

- mark disabled;
- remove plugin jobs from generated cron config;
- hide reader contributions;
- stop future scheduled work;
- preserve generated wiki pages by default.

### Remove

Removal is later than disable. It should refuse if other enabled plugins depend
on the plugin. It should not delete generated wiki content unless the operator
uses a separate purge command.

### Upgrade

Upgrade is out of v1 except for version validation. Later work should add
manifest diff, permission diff, and migration hooks.

## 14. Cron Integration

A plugin contributes local cron definitions:

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

Composition rewrites local names to namespaced active job ids:

```text
okengine.contradictions:refresh
```

Composition input:

```text
config/engine-crons.json
+ <pack>/crons/domain-crons.json
+ <pack>/crons/engine-template-prompts.json
+ enabled plugin cron files
```

Composition output:

```text
config/cron-plus-jobs.json
```

Rules:

- disabled plugin jobs are ignored;
- plugin job names are local to the plugin;
- active job ids are plugin-prefixed;
- duplicate active job ids fail;
- plugin scripts must resolve under the plugin directory or an explicitly copied
  runtime scripts directory;
- generated `cron-plus-jobs.json` remains the only deployed scheduler file.

## 15. Reader Integration

Reader contributions should be declarative in v1:

```yaml
nav:
  - id: contradictions
    title: Contradictions
    kind: dashboard
    path: wiki/dashboards/contradictions.md
```

Rules:

- reader ids are plugin-prefixed internally;
- disabled plugin reader entries are hidden;
- missing generated dashboard pages should show an empty state, not crash;
- reader contributions must not execute plugin code in v1.

## 16. Schema Integration

Schema contributions should be additive and fail-loud:

- adding a new type is allowed if no owner conflict exists;
- adding optional fields is allowed where schema merge supports it;
- changing another owner's required fields is not allowed;
- global toggles remain engine-owned;
- conflicts fail during validation before deploy.

This builds on the composable okpacks direction. If schema composition is not
ready, v1 plugins may restrict schema contribution to validation-only metadata
or capability docs until the underlying merge lands.

## 17. Validator Integration

Validator contributions run during `framework validate`.

V1 validator options:

1. declarative checks only; or
2. local script validators under trust `local-script`.

Validator output should map into the existing `OK/WARN/FAIL` report style.

Rules:

- validators must not mutate files;
- validators must declare required reads/secrets/network;
- validator failures should block deploy only when severity is `FAIL`.

## 18. CLI

Extend `scripts/framework.py` with an `extensions` or `plugins` command.

Preferred command name: `extensions`, because it covers capabilities and runtime
plugins without forcing every contribution into one mental model.

```bash
python scripts/framework.py extensions list <pack>
python scripts/framework.py extensions validate <pack>
python scripts/framework.py extensions inspect <pack> okengine.contradictions
python scripts/framework.py extensions enable <pack> okengine.contradictions
python scripts/framework.py extensions disable <pack> okengine.contradictions
```

Possible aliases:

```bash
python scripts/framework.py plugins list <pack>
python scripts/framework.py capability enable <pack> okengine.contradictions
```

Example list output:

```text
id                         kind        version  state     valid  trust
okengine.contradictions    capability  0.1.0    disabled  yes    declarative
okengine.watchlists        capability  0.1.0    enabled   yes    local-script
```

## 19. Deploy Behavior

Deploy should:

```text
discover extensions
validate enabled extensions
compose engine + pack + enabled extension contributions
write generated runtime config
start/reload services
run post-deploy verification
```

If enabled extension validation fails, deploy fails before runtime change.

## 20. First Vertical Slice

Recommended first plugin: `okengine.contradictions`.

Why:

- small;
- already exists as deterministic script behavior;
- dashboard-shaped;
- useful proof that features can be optional;
- exercises cron + dashboard + reader nav without arbitrary code loading.

Acceptance:

- a clean deployment runs without contradictions enabled;
- copied plugin appears as available/disabled;
- enabling it composes a namespaced cron job;
- disabling removes its active job and reader nav;
- generated `wiki/dashboards/contradictions.md` is preserved;
- validation fails on duplicate id, invalid manifest, missing dependency, or job
  collision.

## 21. Phased Plan

### P0 - Taxonomy And PRD

- finalize this PRD;
- decide `extensions` vs `plugins` CLI naming;
- decide unified `extensions/` directory vs separate directories.

### P1 - Manifest Parser And Validator

- add `scripts/extension_meta.py`;
- parse `extension.yaml`;
- validate id/kind/version/requires/trust/permissions/contributions;
- add focused tests.

### P2 - Discovery Registry

- discover extensions under known directories;
- join with enabled-state;
- add `framework extensions list/inspect/validate`;
- produce stable registry output.

### P3 - Enable/Disable State

- add `.okengine/extensions.yaml`;
- implement enable/disable commands;
- preserve content on disable;
- print permission and runtime-change summary.

### P4 - Cron Composition

- extend cron composition to include enabled extension cron files;
- namespace job ids;
- fail on conflicts;
- add tests for disabled/invalid/colliding plugins.

### P5 - Reader And Schema Contributions

- compose declarative reader nav/dashboard entries;
- add schema contribution validation when schema merge supports it;
- document deferred parts if schema merge is not ready.

### P6 - Example Plugin

- extract contradictions into `okengine.contradictions`;
- add docs and fixture;
- prove enable/disable/deploy lifecycle.

### P7 - Trust/Permission Hardening

- enforce trust/permission consistency;
- add deploy-time permission summary;
- prepare future sandbox/signing hooks.

## 22. Metrics

Useful success measures:

- time to install and enable a simple plugin;
- number of built-in features extracted into optional capabilities;
- validation failures caught before deploy;
- zero silent cron job collisions;
- zero runtime changes from merely copying plugin files;
- operator-visible permission summary for every enabled plugin.

## 23. Open Questions

1. Should the CLI be `extensions`, `plugins`, or both?
2. Should all plugin kinds live under `.okengine/extensions/`?
3. Where should generated reader contribution config live?
4. Where should plugin scripts be mounted inside the gateway container?
5. Should extension enabled-state be committed by default?
6. How should plugin config be overridden per environment without editing plugin
   manifests?
7. Which schema contribution features must wait for multi-pack schema merge?
8. What is the minimum trust enforcement acceptable before public plugin
   distribution?

## 24. Issue Split

Suggested implementation issues:

1. Add extension manifest parser and validator.
2. Add extension discovery registry.
3. Add framework extension list/inspect/validate commands.
4. Add extension enabled-state file and enable/disable commands.
5. Compose enabled extension cron jobs into generated cron config.
6. Add declarative reader contribution support.
7. Add trust/permission validation and deploy-time permission summary.
8. Extract contradictions as the first optional capability plugin.
9. Write extension authoring docs and skeleton.
