# OKEngine — Overview & Concept

*Start here if you want the short version: what OKEngine is, why it exists, how
packs work, and where to read next.*

---

## What It Is

**OKEngine** — short for **Open Knowledge Engine** — turns an agent into a wiki maintainer. It watches sources, writes
structured markdown pages, links them together, repairs drift, and keeps the wiki
useful over time. Swap the domain pack and the same engine can maintain a security
wiki, an investor wiki, a legal wiki, a research wiki, or a product-intelligence
wiki.

```
a live deployment today = OKEngine @ a pinned Hermes + one domain pack
```

The catalyst was Karpathy's LLM-wiki idea: an agent should accumulate knowledge in
a durable wiki instead of rediscovering it from scratch on every query. OKF gives
OKEngine a small compatibility floor (`type` in YAML frontmatter); the detailed
positioning lives in [`okf/okengine-conformance-spec.md`](okf/okengine-conformance-spec.md).

---

## A 30-Second Example

Raw input:

```text
Article: Acme Security acquires LogLens to add cloud detection telemetry.
Published: 2026-06-21
```

Pack judgment turns it into a typed page:

```markdown
---
type: vendor
title: Acme Security
sources:
  - "[[sources/2026/06/acme-acquires-loglens]]"
updated: 2026-06-21
---

# Acme Security

Acme Security is expanding from endpoint detection into cloud telemetry through
the LogLens acquisition.

## Strategic read

The deal suggests Acme is repositioning against cloud-native detection vendors
and may pressure adjacent observability/security platforms.
```

Then the engine handles the generic work: validate the write, update indexes,
refresh hot sets, maintain backlinks, expose search/MCP/reader surfaces, and
queue repair jobs if the page drifts.

---

## Why It Exists

Most "chat with your docs" systems are query-only. The model answers what you ask,
then forgets the work. That is useful, but it does not compound.

OKEngine uses an agent plus a wiki instead:

- Sources are compiled once into pages the agent can reuse.
- Entities, concepts, predictions, and sources are cross-linked.
- Maintenance jobs keep the corpus healthy.
- Predictions and claims can be revisited and graded.
- Search loads already-synthesized knowledge, not just raw chunks.

RAG is still useful as a retrieval technique, but it is not the memory layer. The
wiki is the memory.

---

## How Packs Compose

A pack defines what the engine should build for a domain: schema, persona, feeds,
domain crons, prompts, and optional seed content. The engine supplies the shared
machinery.

Composition is the direction of travel. Today, one active pack per instance is the
supported model; first-class multi-pack composition needs schema, cron, trust,
secret, and ownership checks before arbitrary packs can safely share one vault. See
[`design/composable-okpacks.md`](design/composable-okpacks.md).

---

## The Three Pieces

| Piece | Owns | Changes when you switch domains? |
|---|---|---|
| **Runtime** | Hermes-Agent: gateway, model providers, agent loop, transports. Pinned and patched, not forked. | No |
| **Engine** | Write governance, validation, cron machinery, indexing, search, graph tooling, reader/MCP surfaces, deploy tooling. | No |
| **Pack** | Domain schema, persona, feeds, prompts, crons, source choices, content, and secrets. | Yes |

In practice:

- **Framework = Runtime + Engine.**
- **Deployment today = Framework + one pack.**
- **Swap the pack, change the brain.**

The boundary is versioned in [`engine-manifest.yaml`](../engine-manifest.yaml) and
cron ownership is classified in [`config/cron-tiers.yaml`](../config/cron-tiers.yaml).

---

## How It Works

```text
1. Pack chooses sources
   feeds, APIs, repos, filings, papers, notes

2. Pull jobs store raw material
   raw/ is immutable ground truth

3. Pack prompts classify and compile
   raw item -> typed wiki page

4. Engine maintains the wiki
   validate, index, tier, dedupe, backlink, repair, search, expose

5. Agent and readers consume the compiled graph
   reader UI, MCP tools, search, briefings, downstream agents
```

This maps to Karpathy's three layers: immutable `raw/`, agent-maintained `wiki/`,
and a `schema.yaml` plus persona that define the contract and judgment.

### What the engine owns

Once a typed page exists, the engine handles the generic lifecycle: validation,
field-loss guards, tombstones, indexes, hot/warm/cold tiers, wikilinks/backlinks,
reshelving, sharding, repair drains, health checks, search, and delivery surfaces.

### What belongs to the pack

The pack owns domain judgment: what to ingest, how to classify raw material, which
types exist, which fields matter, and what the agent should consider important.
The engine cannot know that a raw article is a `threat-actor`, `company`,
`funding-round`, or `legal-precedent` without the pack.

---

## What Makes It Trustworthy

The conformance profile is effectively complete for the current design: G1
enforced MCP write path, G2 namespace permissions, G3 review flags and tombstones,
and G4 hot/warm/cold tiers are implemented; G5 identifier-manifest is a deliberate
won't-do unless a concrete need appears.

### Enforced Write Path

Every agent write goes through `okengine-mcp/write_server.py`. The write server
validates against the governing `schema.yaml`, applies field-loss and reserved-file
guards, respects namespace permissions, tombstones instead of deleting, and logs
successful writes to `wiki/log.md`.

### Self-Healing Corpus

A cron fleet keeps the wiki healthy: index tree refresh, hot-set refresh,
reshelve, reshard oversized buckets, YAML repair, broken-wikilink drains, schema
drift checks, health dashboards, and source freshness checks.

### Cheap By Construction

Jobs run under `cron-plus` as separate subprocesses. Pure-script jobs use
`no_agent` and skip the LLM entirely; wake-gated jobs run a cheap script first and
only wake the agent when there is real work.

### Search Without Using RAG As Memory

The wiki is the memory. Retrieval is layered on top: `ripgrep` for exact matches,
`qmd` for local lexical/hybrid search, and `IWE` for the markdown knowledge graph.
See [`kb-tooling.md`](kb-tooling.md).

---

## Consumption Surfaces

- **Reader:** `okengine-reader/` renders the wiki, backlinks, embeds, and exports.
- **Read MCP:** `okengine-mcp` exposes `search`, `get_page`, `find_references`,
  and related read-only tools to other agents.
- **Write MCP:** `okengine-write` is the enforced stdio write path used by the
  agent.

---

## Where To Go Next

| Topic | File |
|---|---|
| Concept defense: agent+wiki vs RAG | [`okf/guide-1-agent-wiki-pattern.md`](okf/guide-1-agent-wiki-pattern.md) |
| Build a vault end-to-end | [`okf/guide-2-building-an-agent-vault.md`](okf/guide-2-building-an-agent-vault.md) |
| Connect data sources | [`okf/guide-3-integration-catalog.md`](okf/guide-3-integration-catalog.md) |
| Scale to 100k files | [`okf/guide-4-scaling-to-100k.md`](okf/guide-4-scaling-to-100k.md) |
| Conformance profile | [`okf/okengine-conformance-spec.md`](okf/okengine-conformance-spec.md) |
| Engine vs pack boundary | [`engine-domain-boundary.md`](engine-domain-boundary.md) |
| Pack composition design | [`design/composable-okpacks.md`](design/composable-okpacks.md) |
| Install OKEngine | [`../INSTALL.md`](../INSTALL.md) |
| Install an existing pack | [`install-selected-pack.md`](install-selected-pack.md) |
| Author a new pack | [`authoring-a-pack.md`](authoring-a-pack.md) |
