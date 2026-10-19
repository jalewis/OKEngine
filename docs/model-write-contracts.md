# Model-write output contracts

Prompts guide a model; they do not enforce what may land in a vault. Every model-driven lane that
mutates canonical pages should declare a versioned `output_contract` beside its cron definition.
The engine validates and composes this domain-neutral shape before deployment; packs supply their
own namespace, type, field, and relationship names.

```json
{
  "output_contract": {
    "api": 1,
    "allowed_namespaces": ["sources"],
    "allowed_types": ["source"],
    "operations": ["create", "update"],
    "required_fields": ["type", "title", "raw"],
    "required_relationships": [],
    "body": {"required": true, "min_non_whitespace": 80, "max_non_whitespace": 8000},
    "unknown_fields": "reject",
    "unresolved_links": "reject",
    "placeholder_links": "reject",
    "completion": "per-selected-item"
  }
}
```

Policy values are `allow`, `review`, or `reject`. Completion is either `run` or
`per-selected-item`. Engine-template prompts retain the legacy string form, or may use an object
with `prompt` and `output_contract`. A pack may narrow allowed values, add requirements, raise body
minimums, lower a declared body maximum, or strengthen `allow → review → reject`; it cannot weaken
an engine floor. `max_non_whitespace` is optional; when present, the governed writer rejects an
oversized body before it reaches the vault.

## Identity changes (`identity_edit`, okengine#1028)

Pack ownership decides who may write a page, not whether a retype is correct. On an EXISTING page
the write path holds every identity change a lane is not granted. The stored value stays, the
change is queued for review (`needs_review` plus the review queue), and the rest of the write
applies. Identity changes are:

- a changed `type` (out of a type as much as into one);
- a changed `title` or `name`. The two are one identity, as the identity index keys a page by
  `name`, else `title`, else its stem: adding the key a page lacks, with a value that is not the
  page's current name under the index's normalization, is a rename too. Naming a page what it is
  already called (a nameless page its own stem) is enrichment;
- an alias dropped from `aliases`. Adding an alias is curation. Dropping one re-routes every later
  identity match, so the hold applies to every type, not only actors;
- a citation dropped from `sources` or `basis` (grant `citations`). Converge
  never drops one: a merge keeps every stored citation and adds the new ones, judged by target,
  for every caller, the same way `raw` is append-only;
- any other curated field removed or emptied, through `remove=` or a null. Engine-managed and
  review-state keys are excepted. Whitespace, and a list or mapping whose every entry is blank,
  count as empty.

Removing a value of a relationship the lane's contract declares is repair, not an identity
change, and applies without a grant (a lacuna lane clearing its own dangling
`prediction_candidate`), only when the check fails closed:
- every value is a wiki path (`namespace/.../stem`). A bare stem, an id (`ns:slug`) or free text
  is never dangling;
- no other page anywhere in the vault carries that stem, compared case-insensitively. A
  sharded, moved, case-variant, duplicated or tombstoned target counts as existing.

The check reads the lane contract's relationship declaration, not a schema field type: the
composed schema has no per-field reference typing to read.

Aliases are judged exactly as the identity index reads them: a scalar `A, B` is two aliases, but a
list entry `"A, B"` is one, so joining two aliases into one list entry drops both.

A field the page did not carry is never an identity change. A held retype or rename also holds a
body replacement made in the same write. Every write path applies the rule: `update_entity`,
`patch_entity`, `converge_entity` and a create that dedup redirects into a converge.

A curation lane may apply identity changes directly with a grant in its contract:

```json
"identity_edit": {"fields": ["type"], "retype_from": ["entity", "@classify_catchall"]}
```

- `fields` lists any of `type`, `title`, `name`, `aliases`, `citations` and `remove`.
- `retype_from` (with `type` only) limits retypes to pages currently of those types.
  `@classify_catchall` stands for the governing schema's `classify_catchall` types.
- A lane whose `operations` include `create` is an ingest or backfill lane and can never hold a
  grant (`framework validate` rejects it).
- A pack may narrow an engine lane's grant, never add or widen one.
- **Trust boundary:** a pack's own domain lane that does not create pages may grant itself
  `identity_edit`. The engine trusts the pack's lane definitions as it trusts the pack's prompts.
  Review a pack lane's grant like any other pack policy.
- `@classify_catchall` follows the governing pack schema's `classify_catchall` list, so the pack
  decides which types its classifier may retype out of.
- A networked extension declares a grant as `capabilities.write_policy.identity_edit`. It is
  validated like a lane contract's, so an extension that can create pages is refused. The write
  path also ignores any grant on a token whose capability includes `create`.
- Only a first-party (engine-tier, `okengine.*`) extension holds its grant by declaration. A pack-
  or operator-tier extension's grant reaches its token only after the operator accepts it:
  `framework extensions enable <pack> <id> --accept-identity-edit` (okengine#1032). The acceptance
  is bound to a digest of the extension id and its whole `write_policy`, so any later change to the
  policy (a wider grant, more paths or types) needs a new acceptance. Until then the token carries
  the rest of the policy, and the extension's identity changes are held for review.
- The token store (`.okengine/extension-tokens.json`) is a copy of the manifest. Every deploy
  re-derives it (`framework extensions reconcile-tokens <pack>`), so a grant the manifest dropped
  does not stay live; `--check` reports drift and exits 1. An active record for an extension that
  is no longer enabled (its directory or its `extensions.yaml` entry removed without
  `extensions disable`) is revoked, record and plaintext, and `extensions disable <pack> <id>`
  revokes such a leftover token too (okengine#1042 [9] [20]).
- The engine grants three lanes:
  - `schema-classify-drain`, for retypes out of `entity` and the catch-all types only;
  - `entity-link` and `dedup-same-url`, the deterministic citation-repair lanes that re-seat a
    moved source and repoint referrers off a retired duplicate, for `citations` only.
- The operator principal, the actor-less admin caller, holds every grant. That covers an
  in-gateway `write_server` import and the review-write service. A networked extension carries
  the same key in its scoped `write_capability`.

`output_contract_exempt` is a temporary, explicit migration marker for a legacy model-writing lane.
It must contain a reason and must not be used for new lanes. Runtime enforcement and verified
per-item receipts are separate layers that consume this contract.

A lane may also narrow the MCP tools its server-bound writer exposes with `write_tools` beside its cron definition (`"write_tools": ["update_entity"]`). `ensure-runtime.sh` forwards it as `OKENGINE_WRITE_TOOLS` on that lane's `okengine-write-<lane>` server and `write_server.py` prunes to exactly that set. Policy still guards every call server-side; the allowlist only stops a local model from spending turns on tools it cannot use. Engine lanes carry their allowlist in the engine; a pack lane's belongs here, never in engine code (okengine#664). The value must be a non-empty list of tools `write_server.py` registers: a bare string, an empty or blank list, or an unknown tool name fails the fleet gates (`framework validate`, ensure-runtime and the deploy split) instead of silently widening the writer to every tool or narrowing it to none. Omit the key to keep the full tool surface (okengine#1044).

`ensure-runtime.sh` registers one `okengine-write-<lane>` server per lane of **this deployment's own fleet**, which it builds in memory from the pack's crons and enabled extensions (`scripts/lane_writers.py`), plus the `cron:` capabilities of its materialized policy. It never reads the engine checkout's shared `config/cron-plus-jobs.json`, which holds whichever pack was split last there. On each run it also removes an engine-generated lane writer whose lane the deployment no longer runs: a block is engine-generated only when its name is the one the engine derives from its own `cron:` actor and it runs the baked `write_server.py` under the baked interpreter. The baseline writers (`okengine-write`, `okengine-write-chat`, `okengine-write-source-quality`) and any server you add yourself are never removed. `deploy-cron-plus-jobs.sh` FAILs while `config.yaml` still registers a writer for a lane outside the deployment's job set; re-run `ensure-runtime.sh` and force-recreate the gateway (okengine#1029). It also re-derives the two values a generated writer copies from its lane, `OKENGINE_OUTPUT_CONTRACT_MODE` and `OKENGINE_WRITE_TOOLS`, so a pack release that widens, narrows or drops a lane's `write_tools`, or adds or drops its contract, reaches the registered writer; only those lines change, and a generated block is engine-owned, so a value you set on one by hand is overwritten. The same jobs gate FAILs while a writer disagrees with its lane (okengine#1042 [3] [18]). `python scripts/lane_writers.py --pack <deployment> --dry-run` shows what a re-render would add, re-derive and remove, without writing.

That server name is `okengine-write-<lane>` only while it is at most 36 characters. Hermes clamps a
dispatched MCP tool name (`mcp__<server>__<tool>`) to 64 characters. Past 49, the clamp eats the
separator between server and tool, so run telemetry can no longer attribute the lane's writes and
its completion contract fails runs that genuinely wrote (okengine#840); between 37 and 49 writes
survive but long tool names are reduced to a hash. The 36-character target is the longest server
name for which no write tool clamps at all, so every dispatched name is the literal one a prompt
can name. A longer name keeps a readable prefix and ends in an 8-hex digest of the full name, for
example `okengine-write-okengine-pre-1f5dbe84` for `okengine.predictions:candidate-watch`.
`cron_pack_split.contract_writer_name()` is the single derivation: regen stamps it onto the job's
toolset, `ensure-runtime.sh` loads it rather than restating it, and on a rename it retires the
engine's own pre-bound server block. The lane's full identity stays in `OKENGINE_WRITE_ACTOR`.
`framework validate` still fails any writer, derived or named explicitly, over 49.

Generated jobs receive a stable `id` derived from the composed lane name and an
`output_contract_digest` (`sha256:<hex>`) over canonical JSON. Writers and receipts must carry both
values so a write cannot be credited to a different lane or contract revision.

Typical lane profiles are: source compilation (`sources`/`source`, body and `raw` required), entity
synthesis (`entities`/`entity`, body plus pack-defined relationships required), briefing and
prediction (their pack-defined namespace/type with per-item completion), and deterministic jobs
(`no_agent: true`, which do not need a model-write contract). These are profiles, not engine-owned
domain constants; each pack declares the exact vocabulary it supports.

Source ingestion is staged: raw compilation may write only accepted source pages; entity synthesis
runs later over accepted sources and requires a resolving source relationship. A raw dedupe key is
consumed only by a source page that satisfies its acceptance fields and meaningful-body minimum.
Large inputs must declare partial extraction instead of treating a context-limited read as complete.
Packs may declare bidirectional relationship field pairs; deterministic propagation fills the
missing inverse after either side is accepted.
