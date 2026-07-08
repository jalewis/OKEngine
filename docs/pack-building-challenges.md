# Pack-building challenges — what actually goes wrong

Field notes from building and auditing five packs (okpack-cti, okpack-ai-research, okpack-example,
okpack-competitive, okpack-cyber-market) plus porting a production system's lane fleet. This is the
*why-packs-rot* companion to [`authoring-a-pack.md`](authoring-a-pack.md) (the how-to) and
[`common-issues.md`](common-issues.md) (symptom→fix). Every item below was found in a real pack.

## 1. Clone-and-diverge is the default failure mode

Packs start as copies (of `okpack-example`, of a sibling, of an origin system) and the copy carries
things that must not travel:

- **Sibling-domain vocabulary.** A cyber-market pack shipped ingest prompts telling the agent to
  "cross-link model↔lab↔researcher↔technique↔benchmark" — okpack-ai-research's nouns, actively
  misdirecting classification. The engine has a domain-leak grep; packs need the inverse — check
  your prompts for the *donor pack's* nouns.
- **Verbatim template bugs.** okpack-competitive's cron tree was okpack-example byte-for-byte:
  "Daily **example** brief", the template's raw-write bug, even **duplicate cron job ids** across
  packs. Fixing the template after the copy fixes nothing downstream.
- **The template has the highest blast radius.** A bug in okpack-example becomes a bug in every
  pack scaffolded or cargo-culted from it. Review the template with more suspicion, not less.

## 2. Fixes don't propagate across siblings

Every lesson landed in exactly one pack and stopped: one pack got `type: briefing` on its daily
brief, the others kept `dashboard`; two got MCP-write prompts, three kept raw `file_write`; one got
a TZ note; none documented model pinning. **When you fix a pack bug, grep the other packs for it in
the same change** — the daily-brief lane is functionally identical in all five, and a lane that
identical arguably belongs in the `engine-template` tier so the fix lands once (okengine#169).

## 3. The write path is easy to silently bypass

The enforced MCP write path only governs agents that (a) have the `okengine-write` toolset and
(b) are told to use it. Three packs shipped brief lanes with **no write toolset** and prompts
saying "write the brief to wiki/briefings/…" — raw `file_write` was the only way to comply, so the
schema/field-loss/review guards never ran. Checklist: every agent lane that writes wiki pages needs
`okengine-write` in `enabled_toolsets` **and** a prompt naming the `mcp_okengine_write_*` tool.
Watch for the half-fixed variant too: a "use the MCP path" addendum bolted onto a legacy prompt
whose step-by-step body still instructs `file_write`.

## 4. Layout/reference assumptions travel badly

- **Flat globs on sharded vaults.** A ported selector used `sources/*.md` — correct on the origin
  vault, **silently zero matches** on a by-date-sharded vault (`sources/YYYY/MM/…`). Every
  namespace scan should `rglob` (see [`sharded-scan-discipline.md`](sharded-scan-discipline.md)).
- **Constructed entity paths.** Prompts that teach the agent a path formula
  (`[[entities/<first-letter>/<slug>]]`, `[[entities/<type>/<slug>]]`) produce broken links and
  duplicate canonicals the moment the vault's layout differs. One pack ran a dedicated repair cron
  as a *permanent band-aid* for links its own brief prompt kept breaking. Reference by **bare
  slug**; scripts that edit existing pages emit the real rglob'd path
  ([`entity-partitioning.md`](entity-partitioning.md)).
- **Field assumptions.** `ingested:` existed on the origin vault, not the target — date lookups
  need a fallback chain (`ingested → created → published`).

## 5. Metadata (tags/class) is not relevance

A trigger filter of `signal_class: current-market-signal` + an event tag looked precise and matched
a flood of Show-HN launch noise — the tags were *true* but the sources were off-thesis. Broad
producers (feeds, HN, an upstream service) need a consumer-side relevance boundary: a
`pack_config.scope` the persona applies, relevance-ranking in selectors so digest budgets aren't
consumed by junk, and a materiality-skim step in the prompt (okengine#167 is the durable
mechanism). Corollary: **filter at the consumer, not the producer** — but *invariants* (a
reasoning model's thinking wasting tokens for every caller) belong at the source.

## 6. Prompt discipline decays without a checklist

The recurring prompt gaps, all found live: missing "**first response MUST be a tool call**" (the
agent burns its first turn on prose); missing "**trust the digest — don't re-fetch**" (the agent
re-reads every source and never writes); an open toolset contradicting the prompt (a lane told to
use only the digest but shipped with the fetch toolset enabled — **boundary beats convention**:
remove the toolset); unbounded drains (no `BATCH_SIZE`); agent-narrative output aimed at
`dashboards/` instead of `briefings/`.

## 7. Ops config rots fastest

- **Compose drift vs the skeleton.** The engine skeleton grew the cockpit service and the
  auth/trust fail-safe env (okengine#90 P4a); every pack compose was a hand-copied snapshot that
  drifted differently — the worst shipped a reader with **zero auth env**, so widening the bind
  exposed an unauthenticated private vault with no fail-safe. Until compose is stamped from the
  skeleton (okengine#169), diff your pack's compose against
  `templates/pack/skeleton/docker-compose.yml` on every engine upgrade.
- **Declared-but-not-applied config.** `pack.yaml` said `port_offset: 300`; the compose bound the
  un-offset ports. Nothing checked the two agree.
- **Stale claims.** READMEs asserting engine pin v0.2.0 against an `engine.version` of v0.7.0,
  "both crons" against a ten-job fleet, validator counts two revisions old. If a README states a
  number, it will eventually be wrong — prefer "run `validate_merged.py`" over quoting its output.
- **Validator fragmentation.** Three vintages of `validate.py` across five packs gave three
  different verdicts on the same contract (one hard-FAILed on inherited base types; another
  silently skipped the same check). The vendored validator is a copy too — see §1.
- **Undocumented operational knowledge.** Per-lane model pinning (`.okengine/cron-models.json` —
  agent lanes otherwise default to the slowest model) and TZ semantics of cron hours were tribal
  knowledge in one deployment, absent from every pack README.

## 8. Secrets and inert-ship confusion

- `.okengine/` (extension enablement, model routing, tokens) is **deploy-time runtime** — gitignore
  it and say in the README that the pack ships inert and `framework validate` on the bare pack
  fails *by design* until extensions are enabled at deploy.
- A gitignored `.env` is still a live secret **on disk** — if the pack directory lives in a
  cloud-synced tree, the token syncs with it. Keep deployment `.env`s outside synced paths or
  accept that the sync provider holds the secret.

## 9. Mode-lock: packs that accidentally forbid co-installation

Packs built standalone-first quietly assume they own the whole vault: bare shared-surface
paths (`raw/market/` two packs would both claim), unprefixed dashboard names, one-blob
config files that cannot merge, and entity taxonomies redeclared instead of marked
host-reusable. None of it breaks standalone — all of it breaks the walk-up co-install
that is the point of building packs (deployment-topology model A). The fix is three
hygiene rules + shipping a `subdomain/` install form; see `authoring-a-pack.md` §8.
Found the hard way: every pack built before okpack-doctrine failed at least one rule.

## 10. A checker nobody tests is a placebo

The deployment-validate pin check shipped reading stamp keys (`engine:`) that the stamp
writer (`ensure-runtime.sh`, which prints `engine_release:`) never emitted. Both sides
were individually reasonable; the CONTRACT between them existed only in two heads. Result:
the check compared empty strings, passed everything, and a deployment pinned two releases
back sailed through weekly validation for its whole life — discovered only when a human
read the stamp file during a release sweep.

The class: any checker whose inputs are produced by a *different* file (a stamp, a
manifest, a generated artifact, an env contract) silently rots when either side moves.
Rules:

- **Every checker gets a red test** — one fixture that MUST fail. A checker born without
  a failing test has never been observed working (this one had zero tests).
- **Cross-file contracts get a contract test** — assert the writer's output keys against
  the reader's expectations in one test that breaks when either file drifts
  (`tests/cron/test_deployment_validate.py::test_stamp_format_matches_ensure_runtime`).
- **Never compare optional-empty against optional-empty.** A missing key must WARN
  "undetectable", not vacuously pass ("" == "" is the placebo's favorite shape).

Sibling of class 2 (fixes don't propagate) in reverse: **deployment→repo backflow**.
Changes made live on a deployment (extensions enabled, rules tuned, aliases retired) rot
the PACK REPO copy unless synced back — the repo copy then fails validation or, worse,
reinstalls stale config on the next pull. After operating on a deployment, diff the
pack-owned files (schema.yaml, config/, crons/, .okengine/extensions.yaml) against the
pack repo and commit the deltas.

## The 60-second pre-ship audit

```
□ prompts: first-response-tool-call · trust-the-digest · MCP write tools named · batch caps
□ toolsets: okengine-write on every wiki-writing lane · fetch toolsets OFF where the digest is the source
□ paths/refs: rglob every namespace scan · bare-slug refs · no path formulas in prompts
□ no donor-pack nouns in prompts · no copied cron job ids
□ compose diffed against the skeleton (auth/trust env, cockpit, offsets APPLIED, limits, healthchecks)
□ README: no hardcoded validator/version numbers · inert-ship note · model-pinning + TZ note
□ .gitignore: .env, .hermes-data/, .okengine/
□ validate.py green — and re-run validate_merged.py, not just the pack-local one
□ deployment-side changes since last ship synced BACK to the pack repo (schema/config/crons/extensions.yaml)
□ deploy-matrix green (offline tier runs in the publish gate; run --live <pack> for packs you touched)
```
