# Authoring an extension

How to build an **extension** — an optional, separately-packaged operation over an OKF
vault (predictions, contradictions, a scorer, a competitive-analytics lane, …). For the
design rationale see [`docs/design/extension-system.md`](design/extension-system.md); for
the enable/disable/purge lifecycle see
[`docs/design/extension-lifecycle.md`](design/extension-lifecycle.md). This is the
practical "make one" walkthrough. Before writing a lane that creates/edits entities, read
[`entity-partitioning.md`](entity-partitioning.md) (layout + write/reference contract); for
recurring pitfalls see [`common-issues.md`](common-issues.md). Companion to [`authoring-a-pack.md`](authoring-a-pack.md)
(a *pack* is a domain's schema + persona + data; an *extension* is an operation that runs
over any pack's vault).

## 0. When is it an extension (vs. engine or pack)?

- **Engine** — a core operation every vault needs (ingest, compile, index). Ships in the engine.
- **Pack** — domain schema, persona, feeds, data. One per deployment.
- **Extension** — an *optional* operation, reused across packs, shipped/enabled independently.
  Predictions and contradictions are the first-party examples (they live in `extensions/`).

`present ≠ enabled`: an extension on disk does nothing until an operator enables it (unless
it is `core: true`, §6).

## 1. Anatomy

```
extensions/<id>/                 # tier-1 engine (extensions/), tier-2 pack (<pack>/extensions/),
  extension.yaml                 #   tier-3 operator (<pack>/.okengine/extensions/)
  <selector>.py / run.py         # in-gateway script(s) — staged into the gateway at deploy
  <lib>.py                       # shared code the scripts import (staged alongside)
  prompts/<op>.md                # bundled agent prompts (optional)
  schema/<frag>.schema.yaml       # schema fragment: owns/extends plus optional enums,
                                  # field_enums, field_shapes, and field_items contracts
  README.md
```

`id` is reverse-DNS-ish (`^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$`, no underscores). `okengine.*`
is **reserved for first-party** (engine-tier) extensions.

## 2. The manifest (`extension.yaml`)

The full field reference is [extension-system.md §6](design/extension-system.md). The minimum:

```yaml
id: vendor.scorer
kind: operation                  # the only shippable kind today
version: 0.1.0
name: Scorer
description: Scores entities on <metric>.
trust: in-gateway                # how the code runs — §4
capabilities:                    # what it may touch — operator-granted
  read:  [wiki/**]
  write: [entities/**]           # write-MCP namespaces (must match what it's allowed to own)
requires:
  engine: ">=0.4.0"
operation:
  schedule: {kind: cron, expr: "20 5 * * *"}
  entrypoint: score.py
```

**Required:** `id`, `kind`, `version`, `requires.engine`, `trust`, `capabilities`.

## 3. Operations — the four shapes

An extension contributes cron jobs via `operation:` (one) or `operations:` (a map → one job
each, named `<id>:<op>`). Each operation is one of:

| Shape | How | Example |
|---|---|---|
| **deterministic** | `entrypoint` script, no prompt → `no_agent` job | `okengine.contradictions` (one `select_contradictions.py`) |
| **agent** | add a `prompt`/`prompt_file` → the agent wakes; `entrypoint` (if any) is the wake-gate selector | `okengine.predictions:grade` |
| **multi-lane** | `operations:` map, several jobs | `okengine.predictions` (candidate-watch + grade + regrade) |
| **sidecar** | `entrypoint: {image: {…@sha256}}`, `trust: sidecar` | a containerized op |

Multi-lane agent example (this is what predictions ships):

```yaml
operations:
  candidate-watch:
    schedule: {kind: cron, expr: "17 6 * * *"}
    entrypoint: select_candidates.py        # wake-gate selector
    prompt_file: prompts/candidate-watch.md  # bundled agent prompt
    toolsets: [hermes-cron, okengine-write, okengine]
    tier: predictions                        # optional kickstart-stage hint (#129)
    # model: <a stronger-tier model>         # optional per-lane model — pick by task profile,
                                             #   see docs/model-selection.md (omit to inherit default)
  grade:   {schedule: {kind: cron, expr: "23 6 * * *"}, entrypoint: grade.py, prompt_file: prompts/grade.md}
```

Scripts run from `/opt/data/scripts/<id>/` in the gateway and may `import` a sibling lib in
the same dir (it's staged too).

**Drop-in form (#63 P1, preferred for new extensions).** Instead of an `operations:` map in
the manifest, drop one file per op into `crons/<op>.cron.json` — each file is the same operation
block (`schedule` / `entrypoint` / `prompt_file` / `toolsets`), and the op name is the filename
stem. The composer collects them forward-only (no central block to edit, no merge conflicts), so
adding a lane is adding a file. Drop-ins and a manifest `operations:` block may coexist; a name
in both is a fail-loud collision. (This is the drop-in contribution model from
[`extension-api-design.md`](extension-api-design.md) §3.1; an importer is just a drop-in with
`entrypoint` and no prompt → `no_agent`.)

```
extensions/okengine.ex/
  extension.yaml          # no operations: block needed
  crons/
    watch.cron.json       # {"schedule": {"kind":"cron","expr":"17 6 * * *"}, "entrypoint":"select_watch.py"}
    grade.cron.json       # -> jobs okengine.ex:watch, okengine.ex:grade
```

**Ordering — `tier:` vs `after:` (#129).** `tier:` is an *advisory* kickstart-stage hint (which
stage a lane belongs to). `after: [<job-name>]` is a **hard** cross-job dependency — "this lane
consumes another lane's output, so it must run after it" — naming namespaced job ids
(`okengine.ex:watch`). The deploy validates the fleet's `after:` graph and **fails loud** on a
missing target or a cycle (the `cron_pack_split check` round-trip reports it too). Runtime
ordering enforcement (staggered schedules / wake-gate freshness) is a later phase — see
[`cron-ordering-design.md`](cron-ordering-design.md); today `after:` is the declared, verified
dependency contract.

## 4. Trust & capabilities (two axes)

- **`trust`** = how the code runs: `declarative` (no code), `in-gateway` (a gateway script —
  **first-party only**; operator-tier in-gateway is refused without `--allow-untrusted`,
  okengine#124), `sidecar` (its own container — the isolation boundary).
- **`capabilities`** = what it may touch: `read` scopes, `write` namespaces, `network`,
  `secrets`, `delivery` — granted by the operator at enable, independent of trust.

Path scope can be narrowed with an optional field/body policy for every new writer whose
responsibility is smaller than a namespace:

```yaml
capabilities:
  read: [sources/**]
  write: [sources/**]
  write_policy:
    rule_id: my-extension-quality-fields
    operations: [update]
    paths: [sources/**]
    types: [source]
    update_fields: [my_score, my_score_reason]
    protected_fields: [type, id, publisher, published, url]
    body: deny
```

The token store binds this grant to the authenticated extension. Writes outside its operation,
path, type, field, or body authority fail atomically. See
[`policy-plane.md`](design/policy-plane.md) for rule ownership, composition, tests, diagnosis, and
waivers.

## 5. Schema — own / reuse / extend

If the operation needs a type, add a `schema:` fragment (composed in only when enabled):

- **Own** a new type/namespace it fully controls — `okengine.glossary` *owns* the `term` type
  + `glossary` namespace via `schema/glossary.schema.yaml` (own = new ids only; a collision
  with a pack/engine type fails the compose).
- **Reuse** existing types by typed reference (write into them, don't redefine) — predictions
  *reuses* the pack-owned `prediction` type rather than owning it.
- **Extend** an existing type with additive fields/enum values.

See [extension-system.md §5](design/extension-system.md) for the composition rules and the
`owners:` grammar (`engine | pack:<name> | ext:<id>`).

## 6. Bundled prompts & overrides

Ship generic prompts as files (`prompt_file: prompts/<op>.md`) so they're not crammed into
YAML. A deployment tunes them **without forking** the extension via
`<pack>/.okengine/extension-prompts.json` mapping a job name to a replacement:

```json
{ "okengine.predictions:grade": "…your tuned grading prompt…" }
```

Model choice is a deployment concern the same way: an extension ships model-agnostic, and an
operator routes a lane via `<pack>/.okengine/extension-models.json` (`{job_name: "@profile" |
"literal-model"}`) — a model name, or an `@`-reference to a `model-profiles.yaml` profile to switch
host/ctx (okengine#151; see [model-selection.md](model-selection.md)).

Cadence is a deployment concern the same way: an extension ships a generic default schedule
in its manifest, and an operator retunes a lane via `<pack>/.okengine/extension-schedules.json`
(`{job_name: "0 6 * * *"}`, a 5-field cron expr) — e.g. run a weekly lane daily — without
forking the manifest. Applied by job name at compose, so the change survives a cron regen.

## 7. Core & pack dependencies (okengine#142)

- **`core: true`** (engine-tier only) makes the extension **default-ON** (opt-out): active
  unless explicitly disabled. For operations that are effectively house baseline.
- A **pack** that genuinely needs an extension declares `requires: [ext:<id>@>=ver]` in its
  `pack.yaml` (or annotates an `ext:<id>` schema owner). `framework validate` then **fails
  before deploy** if it isn't enabled — no silent runtime degrade. Only require what the
  pack truly can't function without.

## 8. Enable, deploy, test

```bash
framework extensions list   <pack>                       # discovered (present != enabled)
framework extensions enable <pack> <id>                   # mint scoped token, regen, compose schema
ENGINE_DIR=… CRON_PACK_DIR=<pack> bash scripts/deploy-cron-scripts.sh      # stage *.py into the gateway
ENGINE_DIR=… CRON_PACK_DIR=<pack> bash scripts/deploy-cron-plus-jobs.sh    # fold <id>[:<op>] jobs into the fleet
# trigger a lane on demand (id from jobs.json):
HERMES_UID=… bash scripts/cron-plus.sh run <job-id>
framework extensions disable <pack> <id>                  # opt-out (also turns OFF a core extension)
framework extensions purge   <pack> <id>                  # remove staged scripts + token
```

Enabling folds the synthesized jobs into `cron-plus-jobs.json` (each carries an `extension:`
provenance marker so the cron tooling routes it correctly — okengine#141).

## 9. Validate (before every deploy)

```bash
framework validate <pack>        # includes the extension-requirement / schema-owner checks
```

Add a regression test under `tests/extensions/` for any new behavior (the manifest grammar,
job synthesis, schema fragment). The first-party extensions are the canonical worked references:
- [`okengine.contradictions`](../extensions/okengine.contradictions) — deterministic, single-op,
  `no_agent`, **core** (default-on); writes a schema-excluded dashboard.
- [`okengine.timeline`](../extensions/okengine.timeline) — deterministic, **core**; a read-only
  derived dashboard (reverse-chronological view) — the simplest "derived view" shape.
- [`okengine.predictions`](../extensions/okengine.predictions) — multi-op, agent lanes, bundled
  prompts; **reuses** the pack-owned `prediction` type.
- [`okengine.glossary`](../extensions/okengine.glossary) — agent, **owns** a schema type
  (`term`) + namespace and writes it, plus a `config:` block — the bring-your-own-schema example.
- [`okengine.dedupe`](../extensions/okengine.dedupe) — agent that **mutates** existing entities
  (merges duplicates: `tombstone` + `superseded_by`) behind a deterministic wake-gate selector;
  opt-in — the "agent op that writes through the guard" reference.
- [`okengine.lacuna`](../extensions/okengine.lacuna) — agent that **owns** a low-trust analysis
  type (`lacuna`) and writes it behind a concept-cluster-density wake-gate; opt-in. The
  reference for a **schema-owning analysis extension with a soft cross-extension edge** (emits
  prediction candidates into `predictions/**` when `okengine.predictions` is enabled — no hard
  `requires`).
- [`okengine.embeddings`](../extensions/okengine.embeddings) — the **sidecar** reference:
  digest-pinned, OS-hardened container (#124) reaching the MCP by service name with scoped tokens
  (#132); ships a buildable image template. The "run untrusted code in isolation" shape.

## `reader_panels` (optional) — richer reader views

An extension can give its pages a richer reader view than generic markdown by declaring
`reader_panels` — a list of `{type, kind, …}` bindings that map a page **type** to a built-in
reader **kind** (`fields` | `two-axis` | `timeline`) and its frontmatter fields. The extension
ships **no renderer code**: the reader owns the kinds, so there's no third-party-JS surface (this is
why it sidesteps the `#124` sandbox concern). Example:

```yaml
reader_panels:
  - type: whitespace-thesis
    kind: two-axis
    x: demand_axis
    y: maturity_axis
  - type: prediction
    kind: fields
    fields: [confidence, status, resolves_by]
```

Bindings are validated at `framework extensions validate` time and composed across enabled
extensions (a type may be bound by only one extension). See
[`reader-extension-points.md`](reader-extension-points.md) for the full design + the kind roadmap.
