# OKEngine extension system

**Status:** design — canonical
**Supersedes:** `plugin-system-prd.md`, `extension-manifest-spec.md`,
`extension-runtime-composition-spec.md`, and the `plugin-system-review-plan.md`
draft (all removed; ideas carried forward in §14).
**Related issues:** #63, #90, #109, #112, #113, #121–#130; build items
#132 (scoped MCP), #133 (composed schema), #134 (discovery), #135 (sidecar
contract) under the #131 architecture gate
**Implementation specs:** [`scoped-mcp-spec.md`](scoped-mcp-spec.md) (#132),
[`composed-schema-spec.md`](composed-schema-spec.md) (#133, implemented),
[`discovery-spec.md`](discovery-spec.md) (#134),
[`sidecar-contract.md`](sidecar-contract.md) (#135),
[`extension-lifecycle.md`](extension-lifecycle.md) (#113, implemented).
Cross-cutting: the write-path
**provenance stamp** is built in #132 (per-extension identity arrives there) and consumed
by #133 (orphan detection) and #135 (attribution) — it does not exist today (§4).

## 1. Summary

An **extension** is an *optional operation the engine can perform over a vault's
wiki data* — packaged separately, opted into per deployment, and written so it
never touches engine internals. Predictions is the canonical example: given the
base wiki, it produces forecast pages; it ships and is paid for independently of
the base, and a deployment that doesn't want it never builds it.

The load-bearing design choice: **an extension is an MCP-API client.** It reads
via the query MCP and writes pages via the enforced write MCP (`okengine-write`).
It does not link engine code, read raw vault files, or patch the gateway. That one
choice delivers all three properties we want at once:

- **isolation** — it binds to a stable API contract, so engine internals can change
  underneath it;
- **privacy** — it is a self-contained artifact (its own repo/image), so a paid
  extension stays out of the base and out of public builds;
- **conformance** — writes go through the same schema-validated, guard-enforced
  path as every other vault write, so extension output can't fork or corrupt the
  vault (the split-brain class, okengine#110/#115).

## 2. Terminology

| Term | Meaning |
|---|---|
| **engine** | The core operations (ingest, compile, score, index, …). Always on. Domain-agnostic. |
| **pack** | Schema + persona + domain data + feeds for one deployment. |
| **extension** | An *optional, separately-packaged operation* over wiki data. Opt-in by default. May ship its own schema. May stay private. |
| **core extension** | An engine-tier extension marked `core: true` — default-**on** (opt-*out*): active unless explicitly disabled (okengine#142). For operations effectively part of the house baseline. |
| **pack→extension dependency** | A pack may declare `requires: [ext:<id>@>=ver]` (or annotate an `ext:<id>` schema owner). `framework validate` fails before deploy if that extension isn't enabled at the floor. A "core" extension a pack relies on is a first-class pattern, not a smell (okengine#142). |
| **plugin** | Reserved for the **Hermes** layer (cron-plus, model-providers) — in-process runtime plugins. Not this system. |

We say **extension**, not plugin: an extension *extends what the system does* from
the outside; a plugin plugs *into* a host's internals (which is precisely what we
are avoiding, and which Hermes already owns the word for).

An extension sits conceptually beside the **engine** (it is an operation), not
beside the pack (it is not domain data). Most extensions are domain-agnostic —
predictions works over any pack's wiki.

## 3. What an extension is — and isn't

**Is:** a separately-packaged operation that, on a schedule, reads wiki data via
the query MCP, computes, and writes pages via the enforced write MCP — bringing
its own schema for any new page types it produces.

**Is not:** a code module loaded into the engine; a patch to engine scripts; a
direct filesystem writer; a domain pack; a Hermes plugin.

**Hard rules:**

- An extension MUST NOT modify engine or pack files.
- An extension MUST write through `okengine-write` (no direct `.md` writes — that
  bypasses schema validation and the field-loss/reserved-file guards).
- An extension MUST be removable: disabling it stops its operation and hides its
  contributions without deleting the pages it already produced (§9).
- An extension MUST declare what it reads, writes, and needs (§6, §7).

## 4. Core principle: extensions are MCP-API clients

The engine already exposes two stable surfaces:

- **query MCP** (`okengine`, read) — the conformant way to read vault content.
- **write MCP** (`okengine-write`) — the enforced contract for all vault writes;
  validates every page against the *composed* schema (§5) and applies the guards.

An extension binds to those, nothing else. The vault's on-disk layout, the engine's
internal scripts, the cron machinery — all of it can change without breaking an
extension, as long as the MCP contracts hold. This is the future-proofing: advanced
operations added later bind to the same surfaces (and to new stable surfaces we add
deliberately — e.g. a raw-ingest API for importers), never to internals.

A consequence we exploit: because writes go through `okengine-write`, the engine can
**stamp each written page with its owning extension id** (provenance) at the write
path — which makes disable/purge and orphan detection tractable (§9), closing
okengine#127 for free.

**Surface reality — the isolation is a build target, not a current property.** Today
the read MCP is a single coarse bearer token (full-read or nothing — no per-extension
scope), and `okengine-write` runs as a local **stdio** server the gateway spawns, so a
separate sidecar container cannot reach it. The MCP-client model is the right
*boundary*, but the isolation it can **enforce** requires building a network-reachable
write surface plus per-extension read/write scopes with authorization in both MCPs.
Until that lands, extensions are **trusted first-party** (§7): the contract is honored,
not enforced. Scoped MCP is the hardening milestone that gates untrusted/third-party
extensions — the build item is okengine#132 (sandboxing/signing is the separate
okengine#124), tracked under the #131 architecture gate.

## 5. Schema model — bring-your-own, layered and additive

An extension brings its own schema. The active schema a deployment validates against
is a **composition**:

```
engine OKF base  ⊕  pack schema  ⊕  Σ(enabled extension schemas)
```

The composition is **additive and owner-scoped**: every type, field, and namespace
has exactly one owner (engine / pack / a specific extension), and composition fails
loud on any ownership conflict — at enable and at deploy, before runtime changes.

An extension's schema can operate at three levels, smallest to most powerful. The
goal is that the easy case is one block and the powerful case is still declarative.

### Level 1 — Own (the easy case, fully self-contained)

Declare new namespace(s) and type(s) the extension owns. No interaction with pack
schema; the extension is fully portable across packs.

```yaml
owns:
  namespaces: [predictions]
  types:
    prediction:
      namespace: predictions
      required: [claim, horizon, confidence]
      fields:
        claim:      {type: string}
        horizon:    {type: date}
        confidence: {type: enum, enum: [low, medium, high]}
```

Rule: owned ids must not collide with any engine/pack/other-extension owner.

### Level 2 — Reuse (typed references to existing content)

Reference types the pack or engine already defines, so extension output links into
the existing graph as typed edges rather than loose strings.

```yaml
owns:
  types:
    prediction:
      fields:
        about: {type: ref, to: entity}   # entity is owned by the pack/engine
```

Rules: a `ref.to` target must exist in the composed schema (else `FAIL`); the
extension declares `requires.schema_refs: [entity]` so the dependency is explicit
and checked at enable.

### Level 3 — Extend (additive fields / enum values on existing types)

Add new **optional** fields to an existing type, or new values to an `extensible`
enum. Never change required-ness, never re-type an existing field, never take
ownership.

```yaml
extends:
  entity:
    fields:
      predicted_by: {type: ref, to: prediction, optional: true}
  source_kind:                 # an extensible enum owned elsewhere
    add: [forecast-derived]
```

Rules: additive only; the extended type/enum must be marked `extensible` by its
owner; the field/value owner is recorded as the extension; conflicts fail loud.

### Composition rules (all levels)

- one owner per type/field/namespace; conflicts `FAIL` at enable/deploy;
- `own` = new ids only; `reuse` = target must exist; `extend` = additive + target
  must be `extensible`;
- the **composed** schema is what `okengine-write` validates against — so extension
  pages get the same conformance guarantee as everything else, with no new
  enforcement path;
- disabling an extension removes its schema layer from the composition; pages it
  wrote are preserved but no longer have an active type owner → they are flagged
  *orphaned* by validation, not deleted (§9).

This is the same additive-merge machinery the composable-okpacks work needs
(okengine#90). **Decision: the merge engine is owned by composable-okpacks (#90) and
extensions consume it — schema composition lands *with* #90, not extension-first.**
The extension-facing requirements on that merge engine (artifact path, owner metadata,
conflict + disable/orphan behavior) are specified in okengine#133.
Building it extension-first would mean writing a merge engine and then refactoring it
for multi-pack and multi-vault (§12); one shared engine avoids that. (Operations stay
isolated; only schema composes.)

## 6. Manifest (`extension.yaml`)

> Building one? The step-by-step walkthrough is
> [`docs/authoring-an-extension.md`](../authoring-an-extension.md); this section is the
> field reference.

```yaml
id: okengine.predictions          # reverse-DNS-ish; okengine.* reserved first-party
kind: operation                   # MVP kind; see §8
scope: vault                      # vault (MVP) | workspace (cross-vault); defaults to vault — §12
version: 0.1.0
name: Predictions
description: Generates forecast pages from base wiki entities.
core: false                       # true (engine-tier ONLY) = default-ON / opt-out — §2, #142

requires:
  engine: ">=0.3.0"
  schema_refs: [entity, source]   # types this op reads/links (must exist composed)
  extensions: []                  # other extensions this depends on

trust: sidecar                    # execution model (§7)
capabilities:                     # what it may touch — operator-granted
  read:  [wiki/**]                # query-MCP scopes
  write: [predictions/**]         # write-MCP namespaces (must match owned schema)
  network: false
  secrets: []
  delivery: false

schema:
  - schema/predictions.schema.yaml  # the §5 fragment (own / reuse / extend)

operation:                        # ONE operation; use `operations:` (a map) for several lanes
  schedule: {kind: cron, expr: "17 5 * * *"}
  entrypoint: run.py              # script (in-gateway) | {script: …} | {image: …} (sidecar)
  timeout: 1800                   # bounded; a runaway op cannot run forever
  # --- agent operation (omit all of these -> a deterministic no_agent script) ---
  prompt: "…"                     # inline prompt -> wakes the AGENT (no_agent: false); OR:
  prompt_file: prompts/run.md     # a bundled prompt file (use one, not both). With a prompt,
                                  #   the entrypoint becomes the wake-gate selector (optional).
  toolsets: [okengine, okengine-write]   # agent toolsets (this is the default)
  tier: score                     # optional #129 hint: slot this job into a kickstart stage

# Multi-operation: one job per entry, named `<id>:<op>` (this is what predictions ships):
# operations:
#   candidate-watch: {schedule: {kind: cron, expr: "17 6 * * *"}, entrypoint: cand.py, prompt_file: prompts/cand.md}
#   grade:           {schedule: {kind: cron, expr: "23 6 * * *"}, entrypoint: grade.py, prompt_file: prompts/grade.md}

config:
  horizon_days: {type: integer, default: 90}
```

**Required:** `id`, `kind`, `version`, `requires.engine`, `trust`, `capabilities`.
**ID:** `^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$`, lower-case, no underscores,
`okengine.*` reserved. **Version:** semver triple; `requires` supports `>=x.y.z`,
`^x.y.z`, bare (treated `>=`). **Scope/paths:** `scope` defaults to `vault`; capability
paths are `[<vault>:]<path>` where an unqualified path means `self` — the multi-vault
grammar reserved in §12. Unknown keys under `requires`/`capabilities`/`schema`/
`operation`(`s`)/`config` are `FAIL`; unknown descriptive keys `WARN`.

**Operation(s).** Declare EITHER a singular `operation:` block OR a plural `operations:`
map (not both) — the map gives one namespaced cron job per entry (`<id>:<op>`), for an
extension with several lanes (#multi-op). Per-operation keys:

| Key | Meaning |
|---|---|
| `schedule` | `{kind: cron, expr: "<5-field>"}` (required) |
| `entrypoint` | in-gateway script name / `{script: …}`, or `{image: …}` for a sidecar. Optional for an agent op (then there's no wake-gate). |
| `prompt` / `prompt_file` | presence makes it an **agent** operation (`no_agent: false`): an inline prompt, or a bundled file under the extension dir (use exactly one). With a prompt, `entrypoint` is the wake-gate selector. No prompt ⇒ a deterministic `no_agent` script (entrypoint required). |
| `toolsets` | agent toolsets (default `[okengine, okengine-write]`). |
| `timeout` | seconds; bounds a runaway op. |
| `tier` | optional kickstart-stage hint (#129) — slot the job into that stage's order instead of guessing a clock time. |
| `model` | optional per-operation model id. The cron scheduler honors `job["model"]` over the deployment's `config.yaml` default — so a low-stakes lane (e.g. glossary) can run on a small/free model while reasoning lanes use a stronger one. Pick by task profile, not brand — see [docs/model-selection.md](../model-selection.md). Omit to inherit the default. |
| `cost_bearing` | `true` on a **`no_agent`** op that still SPENDS model budget — a deterministic script that calls `llm_lib` directly (e.g. `concept-enrich`, `scope-classify`). `budget_guard` pauses it with the agent lanes when over budget; without the marker a no_agent lane looks free and burns paid tokens unpausably. Omit for a truly free maintenance script (zero model calls). |

**`core`** (boolean, engine-tier only): `true` makes the extension **default-ON** (active
unless explicitly disabled), for operations that are effectively part of the house baseline
(#142). A pack can require an extension via `pack.yaml` `requires: [ext:<id>@>=ver]`;
`framework validate` fails before deploy if it isn't enabled — see `authoring-a-pack.md`.

**Prompt overrides.** A deployment tunes an extension's bundled prompts without forking it:
`<pack>/.okengine/extension-prompts.json` maps a job name (`<id>:<op>`) to a replacement
prompt (the engine-template pattern, for extensions).

## 7. Trust and capabilities — two axes, not one ladder

Trust describes **how the operation's code runs**; capabilities describe **what it
may touch**. They are independent (this corrects the conflated ladder in the old
drafts, okengine#121).

**Execution model (`trust`):**

| Value | Meaning |
|---|---|
| `declarative` | no extension-owned code (schema + reader nav + config only) |
| `in-gateway` | a local script in the gateway cron runtime. **Trusted first-party only** — it shares the gateway filesystem/process, so the §3 rules are a contract, not enforced. |
| `sidecar` | the extension's own process/container. The *intended* isolation boundary — enforced only once scoped, network-reachable MCP exists (okengine#132); until then it too runs trusted. Its operational contract (image ref, env/MCP injection, trigger, timeout, logs, cleanup) is okengine#135. |

**Capabilities** (granted by the operator at enable, independent of trust):
`read` scopes, `write` namespaces, `network`, `secrets`, `delivery`.

Rules: contributing a script requires `in-gateway` or `sidecar`; `write`
namespaces must be covered by the extension's owned/extended schema; `network`,
`secrets`, `delivery` are explicit grants surfaced in the enable summary; a
broad `write: [wiki/**]` is allowed only with operator override and always warns.

**v1 is trusted-first-party, not a sandbox.** We validate the manifest, schema
composition, capabilities, and paths, and print an operator capability summary at
enable/deploy — but in v1 those declarations are a *contract*, not enforced at runtime.
As the surface stands (§4), neither mode is isolated: `in-gateway` shares the gateway's
filesystem/process, and `sidecar` cannot reach a scoped write path that does not exist
yet. The plan is for **`sidecar` + scoped MCP to become the enforced boundary** — a
private image holding only per-extension read/write scopes, the API as the wall; that
scoped, network-reachable MCP is the build item in okengine#132 (landed).

**Trust gate (okengine#124, enforced).** Until OS sandboxing/signing exist, `enable`
refuses an **operator-tier** (the third-party/paid drop-in home) extension with
`trust: in-gateway` — untrusted code with full gateway access — unless the operator
passes `--allow-untrusted`. Engine- and pack-tier in-gateway code is allowed (the
author already runs in-gateway via the engine/pack crons); any-tier `sidecar` is
allowed (isolated via scoped MCP + its own container); `declarative` runs no code.
**Still deferred (okengine#124):** OS sandboxing and image signing — the preconditions
before running untrusted third-party extensions from *remote* sources unattended.

**Secrets** are injected as subprocess/container-scoped env vars only to the
extension that declared them, never written to registry output, generated files,
logs, or summaries.

## 8. Kinds — operation now, others future-proofed

MVP ships one kind, `operation` (read → compute → write). The manifest + lifecycle
are general enough that later kinds reuse them by binding to a *different stable
contract*, not by changing the model:

| Kind | Stable contract it binds to | Status |
|---|---|---|
| `operation` | query MCP + write MCP | **MVP** |
| `importer` | a raw-ingest write surface + query MCP | future |
| `reader-extension` | richer reader surfaces (panels/views) | future |
| `validator` | the `framework validate` finding contract | future |

We do not ship a kind until its stable contract exists — no kind is "a script with
no interface." (This is why the old `scoring-policy`/`runtime-plugin` kinds are
dropped for now: they implied interfaces the engine doesn't expose yet.)

**Reader nav is a contribution, not the `reader-extension` kind.** Declarative reader
navigation is a `contributes.reader` entry (an id/title/path into the generated nav)
that **any** kind may ship in MVP — including `operation`. That is what the
`okengine.contradictions` first slice (§11) uses. The `reader-extension` *kind* is
reserved for the richer, eventually code-bearing reader surfaces (panels, custom
views) and stays future.

## 9. Lifecycle

**Invariants** (carried from the old drafts):

- **present ≠ enabled** — discovered on disk has zero runtime effect until enabled;
- **generated-from-source** — runtime config (cron, composed schema, reader nav) is
  regenerated from engine + pack + enabled-extension state; never hand-edited;
- **namespaced** — every emitted id is `<extension-id>:<local>`; collisions `FAIL`;
- **fail-before-runtime** — an invalid *enabled* extension stops deploy before any
  generated file is written or service restarted;
- **preserve content** — disable stops the operation and hides contributions but
  does not delete produced pages.

**Discover → enable → disable** via the framework CLI:

```bash
python scripts/framework.py extensions list    <pack>
python scripts/framework.py extensions inspect <pack> <id>
python scripts/framework.py extensions enable  <pack> <id>
python scripts/framework.py extensions disable <pack> <id>
python scripts/framework.py extensions validate <pack>
```

Enabled-state + config live in the **vault** (`<pack>/.okengine/extensions.yaml`),
not in the extension package — so one package runs in many deployments with
different settings, and a private extension's *enablement* is the operator's, not
the author's.

**Disable + provenance + purge (closes okengine#127):** because pages are written
through `okengine-write`, each carries its owning extension id. On disable, its
schema layer leaves the composition and its pages become *orphaned* — surfaced by
validation, hidden from extension-owned reader nav, but kept. A separate explicit
`extensions purge <id>` (**implemented**, okengine#127) removes the owned pages using
that provenance stamp — disabled-required, dry-run unless `--yes`. (Refusing when
another *enabled* extension references the pages is a follow-up — the MVP guard is
disabled-required.)

## 10. Discovery tiers (closes okengine#122)

Three tiers, so "lean engine + opt-in operations" works without copying engine code
into every vault, and private extensions have a clear home:

| Tier | Holds | Example |
|---|---|---|
| **engine** | first-party `okengine.*` optional ops, shipped with the engine, discovered without copying | predictions, contradictions |
| **pack** | ops a pack bundles for its domain | a pack-specific dashboard |
| **operator/vault** | ops dropped into `<pack>/.okengine/extensions/` — the home for **private/paid** extensions | a customer's private market-intel op |

Discovery scans all three; **enabled-state + config are always vault-level.** A
private extension is just a tier-3 artifact (its own image + a scoped token); it
never enters the engine or a public pack.

## 11. MVP shape and first slice

**MVP operation:** cron-triggered; reads via query MCP; computes; writes pages via
`okengine-write`; brings its own schema; runs under a bounded timeout.

**Isolation choice (the one real MVP decision):**

- `in-gateway` — a cron-plus script in the gateway. Simplest; shares the gateway's
  process/filesystem; weaker privacy. Fine for first-party ops.
- `sidecar` — the extension's own container with a scoped MCP token, triggered by
  cron. Strongest isolation; the real "private third-party artifact" story. Aim
  here for paid/external extensions.

**First slice:** `okengine.contradictions` — small, deterministic, dashboard-shaped,
schema-light (Level 1 own, or even reader-only) — proves discover → enable → cron op
writes a page via the write MCP → reader nav appears → disable hides nav and stops
the op while the page is preserved. `okengine.predictions` is the second slice that
exercises Levels 2–3 of the schema model.

## 12. Workspaces & multi-vault (forward-compatibility)

We want to search/analyze/process pages **across** vaults, and synthesize a **new**
vault from that processing. The MCP-API-client model makes this an *extension* of the
same design, not a rework: a cross-vault operation is an extension holding MCP handles
to several vaults instead of one. **MVP is single-vault — a "workspace of one." Nothing
here is built in MVP; these are seams reserved so single-vault is the degenerate case,
not an assumption we later unwind.** Tracked: okengine#130.

### The workspace layer

```text
workspace = a set of vaults + cross-vault operations + a vault registry
vault     = engine + pack(schema+data) + single-vault extensions
```

A vault is a workspace member. A `scope: vault` extension (MVP) acts on its own vault.
A `scope: workspace` extension reads from members and writes to one target — which may
be a **new** vault it provisions.

### Seams reserved now

1. **Explicit vault handle (lands in MVP).** Capability paths are `[<vault>:]<path>`;
   unqualified means `self`. So `read: [wiki/**]` is `read: [self:wiki/**]` today and
   generalizes to `read: [sec:wiki/**, frontier:wiki/**]` later with no manifest break.
2. **`scope` field** — `vault` (MVP) vs `workspace`; present from day one.
3. **Vault registry** — a workspace maps each vault handle → its MCP endpoint + its
   composed schema. MVP's registry has one entry (`self`).
4. **Enabled-state location** — vault-scoped extensions: vault-level (as designed);
   `scope: workspace` extensions: **workspace-level** (a vault can't own an operation
   that spans its siblings).

### Schema across vaults

- Composed schema stays **per-vault** (`engine ⊕ pack ⊕ enabled extensions`).
- Cross-vault **reads** see heterogeneous schemas → the registry exposes each member's
  composed schema; the operation is schema-aware per source.
- Cross-vault **writes** validate against the **target** vault's composed schema, so a
  workspace operation's schema layer is enabled on the target (falls out of write-via-MCP).
- **Creating a new vault** is a heavier capability (`scope: workspace`,
  `creates: vault`): it derives a new pack — schema = carried-forward source types +
  the operation's own synthesis types — touching the `framework init`/scaffold path, not
  just the write MCP. Out of MVP; the slot is reserved.

### Convergence

Carrying types into a new vault is the composable-okpacks (#90) additive merge applied
across vaults — **one shared mechanism, not a third.** Cross-vault entity resolution
generalizes `multi-source-entity-resolution.md` and is the likely first *workspace*
extension (as contradictions is the first *vault* extension).

## 13. Out of scope (v1) / open questions

**Out of scope:** remote marketplace/registry; signing/provenance verification of
third-party packages; OS sandboxing; arbitrary in-process import of untrusted code;
cross-extension data-dependency
ordering beyond declared `requires.extensions`.

**Isolation milestone (okengine#132):** per-extension **scoped MCP** — a
network-reachable `okengine-write` surface plus per-extension read/write scopes with
authorization in both MCPs — is the work that turns the `sidecar` model from a
contract into an enforced boundary (§4, §7). Required before untrusted third-party
extensions. (Schema-composition ownership is now decided — §5: it lands with #90,
specced for extensions in #133.) These build items sit under the #131 architecture gate.

**Open questions:**

1. Raw-ingest stable surface for the future `importer` kind — what does it look like?
2. Sidecar trigger mechanics — does cron-plus invoke the container, or does the
   extension self-schedule against a deploy-provided token? (okengine#135)
3. Provenance stamp location — OKF envelope field vs. a sidecar index.
4. Discovery roots + precedence — exact engine/pack/operator paths, cross-tier
   shadowing, and duplicate-id-across-tiers behavior (okengine#134, sharpening #113).

## 14. Carried forward from the superseded drafts

Nothing below is lost; it lives in this doc or is explicitly deferred:

- present ≠ enabled; generated-from-source; namespaced contributions; fail-loud;
  preserve-content (§9).
- Manifest contract: id rules, semver `requires`, config schema with overrides in
  enabled-state, strict unknown-key handling (§6).
- Capability/permission summary at enable; secrets never printed; **subprocess/
  container-scoped secret injection** (§7).
- **Bounded timeouts** for extension operations (§6).
- The CLI surface and exit-code conventions (§9).
- The `okengine.contradictions` first slice (§11).
- Trust model — **reworked** into two axes (execution vs capability), resolving
  okengine#121 (§7).
- Discovery — **reworked** into three tiers, resolving okengine#122 (§10).
- Schema — the old "validate/list-only, deferred" stance (#123) is **replaced**:
  extensions bring real schema via additive composition (§5).
- The contradictions-example trust bug (#125) and plugin/extension naming (#126)
  are resolved here (§2, §6).

Issue map: this doc resolves the design intent of #121 (§7), #122 (§10), #123 (§5),
#125/#126 (§2/§6), and #127 (§4/§9). #124 (sandboxing posture) and #128/#129
(script delivery / ordering) are narrowed by the MCP-client + sidecar model and
tracked as the §13 open items. Multi-vault forward-compatibility is reserved in §12
(okengine#130). The implementation-spec work the #131 review surfaced is split into
four build items under that gate: #132 scoped MCP (§4/§7), #133 composed schema (§5),
#134 discovery roots (§10/§13), #135 sidecar contract (§7/§13).
