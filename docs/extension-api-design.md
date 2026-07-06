# OKEngine extension API — the formal contribution model (#63)

> **Status: design proposal.** Defines the stable plugin/extension API. The *current*
> (operations + schema + prompts) system is documented in
> [`authoring-an-extension.md`](authoring-an-extension.md); this doc formalizes it and adds the
> missing contribution points (#63: custom importers, validators, reader panels, scoring
> policies), built on a single principle: **drop-in contributions**.

## 0. Where we are, and the two structural problems

Extensions today are mature for **agent operations** (wake-gated cron lanes), **schema
fragments** (own/extend types + namespaces), and **prompts** (operator-overridable). Enable
mints a scoped token and recomposes; lifecycle state already lives under a vault's `.okengine/`
(`extensions.yaml` enabled-state, `extension-tokens.json`, `migrations-state.json`,
`extension-prompts.json`, `extension-models.json`).

But the model has two structural limits:

1. **Each contribution type has a bespoke fold.** Cron ops are folded by
   `extension_compose._extension_pass`; schema by `_fragments_from_resolved`; prompts/models by
   per-file override maps; `about.md` by a seeding step. Every new contribution type means new
   bespoke fold code — and the cron fold needed a *reverse-split* that just produced a lossy
   round-trip bug (#152).
2. **The reader cannot be extended at all.** It is domain-agnostic and engine-owned, so an
   extension that owns a type can't ship a panel for it (lacuna can't surface its
   confidence/density read; predictions can't ship a scoreboard).

And three of #63's named hooks simply don't exist: **importers** (today they're ad-hoc pack
scripts, e.g. a pack's `*_import.py`), **validators** (only the engine validates), and
**scoring policies** (engine/pack config, not an extension point).

## 1. Principle: drop-in contributions

An extension **contributes files into well-known, typed locations**; the engine **collects**
them deterministically (gather → sort → concat/merge). No bespoke per-type fold logic; no
reverse-split. This is the conf.d / systemd-drop-in model.

- **Forward-only.** Contributions are *collected* into generated deployables (`jobs.json`,
  `composed-schema.yaml`, a reader-panel manifest). The decomposed form (the extension dirs +
  the pack sources) **is** the source of truth — nothing is ever reverse-split out of a merged
  artifact, so the whole `split`/`merge`/round-trip class of bugs (#152) dissolves for
  extensions.
- **Uniform shape.** Every contribution point has: a manifest declaration, an on-disk location
  in the extension dir, an engine **collector**, and a generated deployable. Adding a hook =
  adding one collector, not threading a new type through split/merge/deploy.

## 2. The manifest — one `contributes:` block

```yaml
id: okengine.example
kind: operation
version: 0.1.0
requires: { engine: ">=0.6.0", schema_refs: [entity], extensions: [] }
trust: in-gateway                      # vs `sidecar` (networked, token-scoped)
capabilities: { read: ["wiki/**"], write: ["example/**"] }
contributes:
  crons:         [crons/*.cron.json]   # agent lanes AND deterministic importers (no_agent)
  schema:        [schema/*.schema.yaml]
  prompts:       [prompts/*.md]
  validators:    [validators/*.py]
  reader_panels: [reader/*.panel.yaml]
  scoring:       [scoring/*.policy.yaml]
  about:         [about.md]
```

Back-compat: the current `operations:` / `schema:` / `config:` keys keep working, mapped onto
`contributes.crons` / `.schema` / `.config` through a deprecation window.

## 3. The contribution points

### 3.1 Crons (incl. importers) — the keystone change
An extension ships **one cron file per job** (`crons/<op>.cron.json`), each a full cron def
(schedule, script, prompt ref, toolsets). **Importers are just crons with `no_agent: true`** —
this formalizes today's ad-hoc pack importers into the extension model.

Collector: gather every enabled extension's cron files → append to the deployable `jobs.json`
(name-sorted). This **replaces `_extension_pass`** and removes the reason `split`/`merge` ever
had to round-trip an EXTENSIONS partition — the directory *is* the decomposed form. (See the
"cron-as-directory" discussion that motivated this: the same move lets the engine source side
move from `engine-crons.json` (one array) toward `crons/engine/*.cron.json` drop-ins later.)

### 3.2 Schema — own / reuse / extend (already drop-in)
`schema/*.schema.yaml` fragments, composed by `schema_lib.compose_schema` with an `owners` map
and fail-loud conflict detection. This is already the right shape; formalize it as
`contributes.schema`. (`reference_types`/`reference_fields`, added for KB-health, is a worked
example of a pack/extension-declared **policy** in schema — see §3.6.)

### 3.3 Prompts
`prompts/*.md` for agent crons, operator-overridable via `.okengine/extension-prompts.json`
keyed by job name. Already exists.

### 3.4 Validators — NEW
Today only the engine validates (`tools/schema_validator`). An extension that owns a type can't
enforce type-specific invariants. Hook:

```python
# validators/<name>.py
def validate(path: str, frontmatter: dict, body: str, schema: dict) -> list[str]:
    """Return a list of issue strings (empty = ok). Pure, bounded, no side effects."""
```

Registered at enable; called by (a) the enforced write path (`write_server`) — advisory or
blocking per trust — and (b) `lint_watcher` as a queue. In-gateway trust only until sandboxed
(#124).

### 3.5 Reader panels — NEW (the reader's first extension point)
A **declarative** panel descriptor (no arbitrary JS — safe + needs no reader redeploy per
extension):

```yaml
# reader/<name>.panel.yaml
for_type: lacuna                 # or: for_namespace: predictions
title: "Structural inference"
show: [confidence, surround_density, force]   # frontmatter fields, rendered as a card
style: warn                     # visual treatment (info | warn | success)
```

Collector: enabled extensions' descriptors → a generated `.okengine/reader-panels.json` the
reader reads at runtime (it already reads the vault, read-only). The reader renders declarative
panels generically by extending its existing fact-sheet/meta-panel renderer. A richer
server-rendered panel (from a trust-in-gateway extension service) is a possible Phase-2, only if
declarative proves insufficient.

### 3.6 Scoring policies — NEW
Scoring/classification (source quality, page tiering, reference classification) is engine/pack
config today. Hook: `scoring/<name>.policy.yaml` — a declarative policy (weights, thresholds,
field maps) the relevant engine lane reads. The `reference_types`/`reference_fields` KB-health
knob is the proto-pattern; this generalizes it to an extension-contributable policy.

## 4. Lifecycle & config

- **enable**: validate manifest + every contribution → collect into deployables → mint scoped
  token → recompose. **disable**: drop from `extensions.yaml` → recollect (contributions vanish
  from the deployables — forward-only makes this exact). **upgrade**: version bump; run
  `migrations/*.py` keyed by version against `.okengine/migrations-state.json` (infra exists).
- **config**: unify the per-type override maps (`extension-models.json`, `extension-prompts.json`)
  into one `.okengine/extension-config.json` keyed by `<extension-id>.<contribution>`.

## 5. Validation
`framework validate` and `enable` run: manifest-shape check, schema-fragment compose dry-run,
cron tier/scope check, validator import + signature check, panel-descriptor schema, scoring-policy
schema. Fail-loud **before** deploy — nothing half-composed reaches a vault.

## 6. Security (refs #124)
Two axes, unchanged: **trust** (`in-gateway` = full, trusted first-party; `sidecar` = networked,
token-scoped) and **capabilities** (`read`/`write` scopes enforced at the MCP write path).
Validators/importers run in-gateway only until OS-sandboxed/signed (#124). Reader panels are
declarative-only (no code execution) regardless of trust.

## 7. Migration phases
- **P1 — cron drop-in (do first; retires #152's machinery).** `operations:` → `crons/*.cron.json`
  drop-ins; a collector replaces `_extension_pass`; extensions become a pure forward-collect.
  Lowest risk, highest immediate cleanup, and it validates the whole drop-in principle on the one
  contribution type that already exists end-to-end.
- **P2 — validators** (write-path + lint hook).
- **P3 — reader declarative panels** (the reader's first extension point).
- **P4 — scoring policies + unified `extension-config.json`.**
- Throughout: the current `operations:`/`schema:` manifest keys keep working (mapped onto
  `contributes.*`) until the deprecation window closes.

## 8. Example plugin
`extensions/okengine.example/` — a minimal worked example exercising every hook: one importer
cron (`no_agent`), one schema fragment (owns a type), one declarative reader panel, one
validator. Doubles as the conformance fixture for the API.
