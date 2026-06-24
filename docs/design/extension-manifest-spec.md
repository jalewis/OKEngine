# Technical spec: extension manifest

**Status:** draft technical spec
**Builds on:** `plugin-system-prd.md`
**Related issues:** #63, #109, #113
**Primary implementation target:** `scripts/extension_meta.py`

## 1. Purpose

Define the exact `extension.yaml` contract used by OKEngine extensions,
capabilities, importers, validators, reader extensions, scoring policies, and
runtime plugins.

The manifest is the stable input to:

- extension discovery;
- validation;
- enable/disable commands;
- cron/schema/reader composition;
- permission summaries;
- deploy-time safety checks.

## 2. Filename And Location

Every extension package must contain:

```text
extension.yaml
```

Accepted discovery locations are defined in `extension-runtime-composition-spec.md`.

## 3. Manifest Shape

Minimal valid manifest:

```yaml
id: okengine.example
kind: capability
version: 0.1.0
requires:
  engine: ">=0.2.0"
trust: declarative
contributes: {}
permissions:
  reads: []
  writes: []
  network: false
  secrets: []
  delivery: false
```

Full shape:

```yaml
id: okengine.contradictions
kind: capability
version: 0.1.0
name: Contradictions
description: Refreshes the wiki contradiction dashboard.

requires:
  engine: ">=0.2.0"
  packs:
    - okpack-sec@>=0.1.0
  extensions:
    - okengine.base@^0.1.0

trust: local-script

contributes:
  schema:
    - schema/contradictions.schema.yaml
  crons:
    - crons/jobs.json
  scripts:
    - scripts/select_contradictions.py
  prompts:
    - prompts/contradictions.md
  reader:
    - reader/nav.yaml
  validators:
    - validators/check_contradictions.py

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

## 4. Required Fields

| Field | Type | Rule |
|---|---|---|
| `id` | string | Required, globally unique in one deployment |
| `kind` | string | Required, one known kind |
| `version` | string | Required, semver-like |
| `requires` | mapping | Required, must include `engine` |
| `requires.engine` | string | Required version expression |
| `trust` | string | Required, one known trust level |
| `contributes` | mapping | Required, empty mapping allowed |
| `permissions` | mapping | Required |
| `permissions.reads` | list[string] | Required, may be empty |
| `permissions.writes` | list[string] | Required, may be empty |
| `permissions.network` | boolean | Required |
| `permissions.secrets` | list[string] | Required, may be empty |
| `permissions.delivery` | boolean | Required |

Optional descriptive fields:

- `name`
- `description`
- `homepage`
- `author`
- `license`

## 5. IDs

`id` must match:

```text
^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$
```

Rules:

- lower-case only;
- dots separate ownership/product segments;
- no underscores;
- no whitespace;
- max length 128;
- `okengine.*` is reserved for first-party extensions.

Examples:

```text
okengine.contradictions
okengine.sec.hunting
com.example.my-importer
```

Invalid:

```text
OKEngine.Plugin
okengine_plugin
okengine..plugin
.okengine.plugin
okengine.plugin.
```

## 6. Kinds

Known `kind` values:

| Kind | Meaning |
|---|---|
| `capability` | Optional feature bundle |
| `runtime-plugin` | Runtime/platform plugin |
| `importer` | Pulls or normalizes external data |
| `validator` | Adds validation checks |
| `reader-extension` | Adds reader nav/panels/views |
| `scoring-policy` | Adds ranking/scoring logic |

Unknown kinds are `FAIL`.

## 7. Versions And Requirements

Version strings should be semver-like:

```text
MAJOR.MINOR.PATCH
```

The validator may accept extra suffixes later, but v1 should normalize on
numeric triples.

Supported requirement syntax:

| Syntax | Meaning |
|---|---|
| `>=0.2.0` | installed version must be greater than or equal |
| `^0.2.0` | installed version must be same major and greater than or equal |
| `0.2.0` | treated as `>=0.2.0` |

Requirements:

```yaml
requires:
  engine: ">=0.2.0"
  packs:
    - okpack-sec@>=0.1.0
  extensions:
    - okengine.contradictions@^0.1.0
```

Rules:

- `requires.engine` is mandatory.
- pack and extension requirements are optional.
- a requirement without `@` is presence-only.
- missing required packs/extensions are `FAIL` for enable/deploy.
- missing required packs/extensions are `WARN` for a bare manifest parse.

## 8. Trust Levels

Known `trust` values:

| Trust | Allows |
|---|---|
| `declarative` | metadata, schema, reader nav, config only |
| `local-script` | local scripts run by OKEngine jobs/validators |
| `trusted-runtime` | runtime/plugin host loading |
| `network` | outbound network use |
| `delivery` | outbound notification/message sending |

Trust levels are ordered:

```text
declarative < local-script < trusted-runtime < network < delivery
```

Permission consistency:

| Permission | Minimum trust |
|---|---|
| contributes `scripts` | `local-script` |
| contributes `validators` script files | `local-script` |
| `network: true` | `network` |
| non-empty `secrets` | `network` or `delivery` |
| `delivery: true` | `delivery` |

V1 validates declarations. OS-level sandboxing/signing is deferred.

## 9. Contributions

`contributes` is a mapping. Known keys:

| Key | Type | Rule |
|---|---|---|
| `schema` | list[path] | YAML schema fragments |
| `crons` | list[path] | JSON cron job arrays |
| `scripts` | list[path] | local scripts referenced by crons |
| `prompts` | list[path] | prompt fragments or prompt maps |
| `reader` | list[path] | reader contribution YAML files |
| `validators` | list[path] | validator scripts or declarative validator YAML |

Rules:

- unknown contribution keys are `FAIL`;
- paths are relative to the extension root;
- paths must not escape the extension root;
- absolute paths are invalid;
- missing contributed files are `FAIL`;
- path traversal such as `../x` is invalid;
- symlink policy is deferred; v1 should reject symlinks that resolve outside the
  extension root.

## 10. Permissions

Permissions shape:

```yaml
permissions:
  reads:
    - wiki/**
  writes:
    - wiki/dashboards/contradictions.md
  network: false
  secrets: []
  delivery: false
```

Path permissions:

- paths are deployment-relative;
- v1 allows glob-like suffixes such as `wiki/**`;
- write permissions should be as narrow as practical;
- `writes: ["wiki/**"]` is allowed only for `trusted-runtime` or higher and
  should warn even when allowed.

Secrets:

```yaml
permissions:
  secrets:
    - TAXII_API_KEY
```

Rules:

- secret names must match `^[A-Z][A-Z0-9_]*$`;
- missing configured secrets are checked at enable/deploy time, not manifest
  parse time;
- secret values must never appear in registry output.

## 11. Config Schema

`config` declares operator-settable options.

Supported config field types:

| Type | Validation |
|---|---|
| `string` | scalar string |
| `integer` | integer |
| `number` | int/float |
| `boolean` | bool |
| `enum` | value in `choices` |
| `list` | list, optional `items` type |

Example:

```yaml
config:
  severity_floor:
    type: enum
    choices: [low, medium, high, critical]
    default: medium
  max_candidates:
    type: integer
    default: 25
```

Rules:

- unknown config field types are `FAIL`;
- defaults must validate against the declared type;
- operator overrides are stored in enabled-state, not in `extension.yaml`;
- config descriptions are optional.

## 12. Unknown Fields

Unknown top-level descriptive fields should be `WARN` unless they collide with a
reserved future key.

Unknown keys under these blocks are `FAIL`:

- `requires`
- `contributes`
- `permissions`
- `config`
- `lifecycle`

## 13. Validator API

Initial implementation target:

```python
load_extension_meta(path: Path) -> dict | None
validate_extension_meta(meta: dict, root: Path) -> list[Finding]
```

Finding shape:

```python
{
    "severity": "OK" | "WARN" | "FAIL",
    "check": "extension.id",
    "detail": "..."
}
```

The `framework` command should render findings in the same spirit as
`framework_validate.py`.

## 14. Normalized Metadata

Parser output should normalize to:

```python
{
    "id": "okengine.contradictions",
    "kind": "capability",
    "version": "0.1.0",
    "name": "Contradictions",
    "description": "...",
    "requires_engine": ">=0.2.0",
    "requires_packs": ["okpack-sec@>=0.1.0"],
    "requires_extensions": [],
    "trust": "local-script",
    "contributes": {
        "schema": [...],
        "crons": [...],
        "scripts": [...],
        "prompts": [...],
        "reader": [...],
        "validators": [...],
    },
    "permissions": {
        "reads": [...],
        "writes": [...],
        "network": False,
        "secrets": [],
        "delivery": False,
    },
    "config_schema": {...},
    "root": "/abs/path/to/extension",
}
```

## 15. Acceptance Criteria

- valid minimal manifest parses and validates;
- valid full manifest parses and validates;
- invalid id fails;
- unknown kind fails;
- missing `requires.engine` fails;
- missing required files fail;
- path traversal fails;
- scripts with `trust: declarative` fail;
- `network: true` below `network` trust fails;
- secret names with lowercase or punctuation fail;
- invalid config defaults fail;
- normalized metadata is deterministic;
- tests cover all cases above.

