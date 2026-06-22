# docs/okf — LLM-wiki guides and OKF compatibility notes

This engine implements the **agent-maintained LLM-wiki pattern** (articulated by
Andrej Karpathy). These guides are the **domain-agnostic** reference for that
pattern *as built here*. Google's Open Knowledge Format (OKF) is treated as a
minimal compatibility/interoperability floor for markdown+YAML agent knowledge,
not as the origin of the project. Security is the first concrete worked domain.
The reference pack is **okpack-sec** (a security-focused LLM-wiki pack, maintained
in its own repo); this engine ships no pack content of its own.

## The generalized guides (current, domain-agnostic — start here)

1. [`guide-1-agent-wiki-pattern.md`](guide-1-agent-wiki-pattern.md) — the architecture briefing: why agent+wiki over query-only/RAG, the 3-layer Karpathy pattern, OKF compatibility, + a reference-implementation map to this engine.
2. [`guide-2-building-an-agent-vault.md`](guide-2-building-an-agent-vault.md) — the build guide: vault structure, schema/frontmatter, structural files, agent behavioral contract, conformance/validation, lint, tooling, ingest, deployment.
3. [`guide-3-integration-catalog.md`](guide-3-integration-catalog.md) — connecting data sources: Bundle / Query / Enrichment classes, the feeds→raw→ingest pipeline, provenance, cadence.
4. [`guide-4-scaling-to-100k.md`](guide-4-scaling-to-100k.md) — the scaling strategies, each annotated with how this engine implements it + status.

## Deployment model

- [`../authoring-a-pack.md`](../authoring-a-pack.md) — **build a pack from scratch**, step by step (scaffold → schema → persona → feeds → crons → validate → deploy).
- [`deployment-topology.md`](deployment-topology.md) — **engine vs instance vs pack vs domain**, the two senses of "multiple packs" (multiple domains in one vault via walk-up, vs separate instances per pack), the decision rule, and the **public/private isolation** rule (a public pack is always its own instance).

## Conformance

The OKF conformance program closed at ~95% conformance: G1 MCP write path, G2
namespace permission matrix, G3 confidence flag-model + tombstones, and G4
hot/warm/cold tiers all shipped; only G5 (identifier manifest) remains — a
deliberate won't-do unless qmd latency/cost or an exact private-ID need forces it.
The flat→hierarchical migration (a ~39k-page link-preserving move) is complete.

## Originals (security-domain reference source)

The `guide-*` files above were generalized from the security-domain source docs
(the original concrete domain framing of the same pattern, not the engine's
canonical docs).

Related engine docs (outside this dir):
[`../engine-domain-boundary.md`](../engine-domain-boundary.md),
[`../deploy-a-new-domain.md`](../deploy-a-new-domain.md), [`../kb-tooling.md`](../kb-tooling.md).
