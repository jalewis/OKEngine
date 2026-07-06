# okengine.actor-risk-ranking — design (okengine#170)

**Status:** design, census-grounded — v1 not yet built
**Relates to:** [`../application-catalog.md`](../application-catalog.md) (the risk-ranking gap
family: four verticals want this shape), okengine#112 (hunt-hypothesis handoff, soft edge),
okengine#167/relevance-gate (scope-config pattern), okengine#168 (the backlink artifact this
design scores over).

## 1. What it is

Target-relative, evidence-backed, explainable ranking of threat actors against
operator-configured targets (an organization, sector, technology stack). Output is an
operator-facing dashboard that says **why** each actor ranks where it does, what evidence
supports each driver, what is missing, and what should follow (hunts, predictions). It is
not a "threat score": scores are horizon-bound sort keys; bands + drivers + unknowns are
the product.

Generic by construction: the extension ships repeatable scoring over whatever
actor/malware/technique/vulnerability types the enabled pack's schema declares
(`requires.schema_refs`, discovered — never hardcoded); every target, sector priority, and
exclusion is operator config. No shipped seeds.

## 2. The census that fixes the v1 design

Measured on a live security-domain deployment (~51k pages) before designing:

| Signal | Count |
|---|---|
| threat-actor pages | 1,290 |
| vulnerability pages | 4,298 |
| malware / tool pages | 2,049 / 370 |
| attack-pattern pages | 682 |
| campaign pages | 199 |
| actors with `sectors:`/`techniques:`/`cves:` frontmatter | **0** |
| actors with any structured targeting field | ~15% (`aliases:` 191, `motivation:` 32, `targeting:` 9) |
| actors with ≥1 inbound wikilink edge | 1,122 (87%) |
| actors with ≥5 / ≥20 edges | 367 / 68 |
| actors with a current-year source mention | 319 |

**Consequence:** frontmatter-driven capability/intent scoring is impossible on a real vault
today — the structured fields simply are not populated. The **wikilink graph is the
evidence layer**: dense, source-anchored, and (since #168) precomputed nightly into
`wiki/.backlinks.json` (79k inverted edges on this vault). v1 scores over that artifact
plus page frontmatter and source dates. No model, no network, no agent.

The census also surfaced `lazarus` and `lazarus-group` as *separate top-10 actors* — the
alias problem is real data, not a hypothetical. v1 folds aliases from the `aliases:`
frontmatter that exists (191 pages) and reports unresolved near-duplicates as unknowns;
full identity convergence stays with the entity-resolution gap family (catalog).

## 3. v1 — deterministic, dashboard-only, no_agent

One weekly op (`tier: analyze` is wrong — this is arithmetic; `no_agent: true`), scheduled
**after** `backlinks-refresh` so the artifact is fresh. Reads:

- `wiki/.backlinks.json` — the evidence edges (falls back to skipping the run LOUDLY if
  absent/stale; it never rebuilds the graph itself);
- actor/malware/tool/attack-pattern/vulnerability/campaign pages' frontmatter (types
  resolved via schema_refs against the composed schema);
- source pages' `published`/`publisher` for recency + origin-domain independence;
- `config/actor-risk-targets.yaml` (operator-owned; example below). **No config = loud
  no-op** (one log line, no dashboard, exit 0).

### Score decomposition (per actor × target)

All drivers are edge-counts the operator can click through — every driver line on the
dashboard links the pages that produced it:

| Driver | v1 evidence |
|---|---|
| direct | actor page ↔ target entity page edges (co-mention; the strongest signal) |
| opportunity | actor ↔ vulnerability edges where the vulnerability also links a target technology/product |
| capability | breadth of actor ↔ malware/tool/attack-pattern edges |
| intent | actor ↔ sector/segment-concept edges intersecting the target's `sectors:` |
| recency | share of the actor's source referrers inside the horizon (default 180d) |
| confidence | **distinct origin domains** of referring sources — never raw item counts (syndication ≠ independence) |
| unknowns | drivers with zero evidence, unresolved alias candidates, stale artifact age — listed, and they *cap* the band |

`risk_score` (0–100) exists to sort; the dashboard leads with `risk_band`
(low/moderate/elevated/high) + drivers. A band above *moderate* requires ≥2 independent
drivers AND ≥2 distinct origin domains — no single syndicated report can produce a high
band.

### Outputs

- `dashboards/actor-risk/rankings.md` — all targets, top-N actors each, drivers inline.
- `dashboards/actor-risk/<target>.md` — full decomposition per target: every scored actor,
  evidence links, missing-evidence list, config echo (horizon, priorities), artifact
  build-stamp.

Dashboards are generated derived surfaces (same class as kb-health/completeness) —
script-written, freshness-stamped. v1 writes **nothing else**: no actor-profile
enrichment, no predictions, no hunt pages.

### Person-target rule (hard)

`type: person` targets are **refused at config parse** in v1 (error, run aborts — loud,
not silent). Person-relative output is deferred to a possible later *agent* lane gated on
`require_human_review_for_people` + public-role framing; the deterministic ranker never
scores people.

### Config (operator-owned, pack-side)

```yaml
# config/actor-risk-targets.yaml
targets:
  our-org:
    type: company
    entity: entities/o/our-org          # optional — direct-edge driver needs it
    sectors: [cloud-infrastructure]      # concept/segment slugs in THIS vault
    technologies: [entities/p/okta, entities/p/kubernetes]
    priority: high
scoring:
  horizon_days: 180
  min_origin_domains: 2
  top_n: 25
  exclude_actors: []                     # slugs the operator rules out-of-scope
```

## 4. Extension shape

```
extensions/okengine.actor-risk-ranking/
  extension.yaml        kind: operation · core: false · requires.engine >=0.8.0
                        requires.schema_refs: [<actor-like>, vulnerability, malware]
                        one op: weekly, no_agent, after backlinks-refresh
  actor_risk_rank.py    the scorer (stdlib+yaml only — self-containment guard)
  README.md             config reference, no-op semantics, the person-target rule
  tests/                fixture vault + artifact → deterministic expected rankings;
                        no-config no-op; person-target refusal; stale-artifact skip;
                        single-syndication cannot reach a high band
```

Capabilities: `write: [dashboards/actor-risk/**]` only. No agent prompt ships in v1.

## 5. Deferred (explicitly, with their triggers)

- **Prediction candidates** (`predictions/**` soft edge, the lacuna pattern) — when a
  ranking movement is falsifiable and dated; needs an agent lane. Trigger: v1 dashboards
  prove drivers stable enough to forecast on.
- **Hunt-hypothesis handoff** (#112) — blocked on the hunting design itself.
- **Actor-profile enrichment** (backfilling the empty `sectors:`/`techniques:` frontmatter
  from the same edges) — arguably the highest-value follow-up: it converts link evidence
  into queryable structure for *every* consumer, not just this ranker. Separate lane,
  MCP-write path, needs_review.
- **Cross-feed entity resolution** (lazarus vs lazarus-group) — catalog gap family; v1
  only folds declared `aliases:` and reports the rest.
- **Vendor/supply-chain variant** (#174) — same scorer, organization ontology; keep the
  scoring core ontology-free so #174 is config, not a fork.

## 6. Risks carried from the issue, and where the design answers them

- *Evidence laundering / false precision* → deterministic v1, drivers are clickable edges,
  bands gated on independent origin domains.
- *Recency bias* → recency is one driver, not a multiplier over the rest.
- *Source duplication* → origin-domain dedupe in the confidence driver.
- *Naming/entity resolution* → aliases folded, near-duplicates surfaced as unknowns.
- *Schema variability* → schema_refs + config; no hardcoded type names or paths.
- *Person abuse* → refused in v1, human-review-gated framing if ever built.
