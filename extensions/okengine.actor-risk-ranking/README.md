# okengine.actor-risk-ranking

Target-relative, evidence-backed, explainable ranking of actor pages against
operator-configured targets. Deterministic v1 (okengine#170;
design: `docs/design/actor-risk-ranking.md`): no model, no network, no agent —
one weekly `no_agent` op that scores the precomputed backlink graph
(`wiki/.backlinks.json`, okengine#168) plus source dates, and writes
dashboards under `dashboards/actor-risk/`.

## Why the link graph, not frontmatter

The design census (1,290 actors on a real vault) found structured targeting
frontmatter essentially empty (`sectors:`/`techniques:`/`cves:`: zero pages)
while 87% of actors carry inbound wikilink edges. v1 therefore treats **edges
as evidence**: every driver on the dashboard is a set of clickable pages.

## Drivers

| driver | evidence |
|---|---|
| direct | edges between the actor and the target's own entity page |
| opportunity | actor↔vulnerability edges where the vulnerability also touches a target technology |
| capability | breadth of actor↔malware/tool/attack-pattern edges |
| intent | actor↔sector/segment edges intersecting the target's sectors |
| recency | actor's source referrers inside the horizon (default 180d) |

**Confidence counts distinct origin domains, never items** — five syndications
of one report are one domain. A band above `moderate` requires ≥2 non-zero
drivers AND ≥ `min_origin_domains` (default 2); zero-evidence drivers are
listed as unknowns and cap the band at `elevated`. `risk_score` exists to sort;
read the band + drivers.

## Config (operator-owned; the extension ships no seeds)

```yaml
# <vault>/config/actor-risk-targets.yaml
targets:
  our-org:
    type: company
    entity: entities/o/our-org            # optional — enables the direct driver
    sectors: [cloud-infrastructure]        # matched against segment/concept slugs
    technologies: [entities/p/okta]        # full page keys (edge intersection)
scoring:
  horizon_days: 180
  min_origin_domains: 2
  top_n: 25
  exclude_actors: []
  # actor_types: [threat-actor, intrusion-set]   # override when the pack names differ
  # type_map: {adversary: threat-actor}          # fold pack-specific type names
  # --- ontology overrides (okengine#174): the scorer is noun-free arithmetic ---
  # capability_types: [malware, tool, attack-pattern, software]
  # vulnerability_types: [vulnerability]
  # sector_types: [segment, sector, concept]
```

The **vendor variant** (okpack-vendor-risk, okengine#174) is pure config:
`actor_types: [vendor]`, `capability_types: [product, component]` — the "actors"
being ranked are vendor pages and capability is their supply footprint. Same
drivers, same gates, no fork.

No config → the lane no-ops loudly. **`type: person` targets are refused at
parse** (exit 2): the deterministic ranker never scores people; person-relative
output would be a later agent lane gated on human review, and is deliberately
not built.

## Failure semantics

- `wiki/.backlinks.json` missing or older than `artifact_max_age_hours`
  (default 48h) → the run **skips loudly** (exit 1). Never ranks a stale graph;
  enable the `backlinks-refresh` engine cron first.
- Actor pages whose slug matches another actor's declared `aliases:` fold into
  it; undeclared near-duplicates (e.g. `lazarus` vs `lazarus-group`) are
  reported on the dashboard as unresolved — never auto-merged (that is the
  entity-resolution gap family, see `docs/application-catalog.md`).

## Deferred by design

Prediction candidates, hunt-hypothesis handoff (okengine#112), actor-profile
frontmatter enrichment, cross-feed entity resolution, and the vendor/
supply-chain variant (okengine#174 — same scorer, different ontology; keep the
core ontology-free).
