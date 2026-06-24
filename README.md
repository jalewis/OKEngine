# OKEngine

[![CI](https://github.com/jalewis/okengine/actions/workflows/ci.yml/badge.svg)](https://github.com/jalewis/okengine/actions/workflows/ci.yml)
![status: pre-1.0, active development](https://img.shields.io/badge/status-pre--1.0%20active-orange)
![license: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)
![python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

**Project status:** pre-1.0, active development — APIs, schema, and layout may change between
`0.x` releases. Pinned to a specific Hermes-Agent version (see `engine-manifest.yaml`).

> **Cost warning:** OKEngine is designed to run autonomous agents on a schedule.
> With empty feeds it is almost free, but once you add active sources it can make
> continuous LLM calls. A busy pack can cost real money. Read
> [`docs/operating-cost.md`](docs/operating-cost.md) and set provider-side hard
> caps before enabling ingest crons.

**OKEngine** (short for **Open Knowledge Engine**) turns an agent into a wiki maintainer. It
watches sources, writes structured markdown pages, links them together, repairs drift, and keeps
the wiki useful over time. Swap the domain pack and the same engine can maintain a security
wiki, an investor wiki, a legal wiki, a research wiki, or a product-intelligence wiki.

```
a live deployment today = OKEngine @ a pinned Hermes + one domain pack
```

The catalyst was Andrej Karpathy's
[LLM-wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
an agent should accumulate knowledge in a durable wiki instead of rediscovering
it from scratch on every query. OKF compatibility is the small portability floor
(`type` in YAML frontmatter); OKEngine's core is the swappable-topic LLM-wiki
engine.

> **New here?** Start with [`docs/overview.md`](docs/overview.md).

## 30-Second Example

A raw article:

```text
Acme Security acquires LogLens to add cloud detection telemetry.
```

The pack decides what that means and writes a typed page:

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

The deal suggests Acme is repositioning against cloud-native detection vendors.
```

The engine then handles the generic work: validate the write, update indexes,
refresh hot sets, maintain backlinks, expose reader/MCP/search surfaces, and run
repair drains if the page drifts.

## Why

Most "chat with your docs" systems answer a question and forget the work. OKEngine
uses an agent plus a wiki instead:

- Sources are compiled once into reusable pages.
- Entities, concepts, predictions, and sources are cross-linked.
- Claims and predictions can be revisited and graded.
- Maintenance jobs keep the corpus healthy.
- Search loads already-synthesized knowledge, not just raw chunks.

RAG can still be a retrieval technique. It is not the memory layer. The wiki is
the memory.

## Packs

The engine ships zero domain knowledge. A **pack** supplies the domain:
`schema.yaml`, persona, feeds, prompts, crons, optional seed content, and runtime
configuration.

One active pack per instance is the supported deployment model today.

## What's Included

- **Enforced write path:** `okengine-mcp/write_server.py` validates agent writes,
  guards against field loss, blocks reserved files, tombstones instead of deleting,
  and logs writes.
- **Self-maintaining corpus:** cron jobs refresh indexes, hot sets, tiers,
  wikilinks, schema health, source freshness, and repair queues.
- **Reader:** `okengine-reader/` is a read-only web UI for the wiki.
- **MCP query surface:** `okengine-mcp/` exposes read-only tools such as search,
  page fetch, references, and page listing.
- **Framework CLI:** `scripts/framework.py` scaffolds, lists, pulls, and validates
  packs.
- **OKF-compatible floor:** markdown + YAML pages with the minimal `type` field
  baseline where conformance is enabled.

## Quickstart

1. **Clone the engine**

   ```bash
   git clone <engine-repo> okengine
   ```

   No manual Hermes/patch/overlay work needed — `deploy.sh` (step 3) builds the `hermes-agent`
   Docker image (pinned Hermes + `patches/` + overlay) on first run. The deployable
   `docker-compose.yml` ships with the **pack** (step 2), not the engine. See
   [`INSTALL.md`](INSTALL.md) for the build internals / by-hand path.

2. **Get a domain pack**

   Use a catalog pack:

   ```bash
   python scripts/framework.py list
   python scripts/framework.py pull <pack> ../my-brain
   ```

   Or scaffold a new one:

   ```bash
   python scripts/framework.py init ../my-brain --domain "..."
   python scripts/framework.py validate ../my-brain
   ```

   The engine checkout and vault are separate sibling directories:
   `okengine/` + `my-brain/`.

3. **Deploy from the vault directory**

   ```bash
   bash ../okengine/scripts/deploy.sh
   ```

## Cost

OKEngine is a **24/7 autonomous agent system**. With an empty feed list, ingest
crons are wake-gated and the LLM rarely fires. Once you populate feeds, it can make
LLM API calls continuously. Cost depends on feed volume, prompts, model choice,
and retry behavior.

Controls:

- Use a local/free model where possible.
- Set hard spend caps with your model provider.
- Enable the `budget-guard` cron.
- Read [`docs/operating-cost.md`](docs/operating-cost.md) before adding feeds.

## Docs

- [`docs/overview.md`](docs/overview.md) — start here: concept, packs, and
  architecture.
- [`docs/operating-cost.md`](docs/operating-cost.md) — token/cost model and budget
  controls.
- [`INSTALL.md`](INSTALL.md) — install OKEngine on pinned Hermes.
- [`docs/install-selected-pack.md`](docs/install-selected-pack.md) — install an
  existing pack.
- [`docs/install-okpack-sec.md`](docs/install-okpack-sec.md) — install the
  security/threat-intel pack.
- [`docs/install-okpack-ai-research.md`](docs/install-okpack-ai-research.md) —
  install the AI/LLM research-watch pack.
- [`docs/authoring-a-pack.md`](docs/authoring-a-pack.md) — author a new pack.
- [`docs/deploy-a-new-domain.md`](docs/deploy-a-new-domain.md) — pack spec and
  deployment guide.
- [`docs/engine-domain-boundary.md`](docs/engine-domain-boundary.md) — engine vs
  pack boundary.
- [`docs/okf/okengine-conformance-spec.md`](docs/okf/okengine-conformance-spec.md)
  — OKEngine conformance profile and OKF compatibility floor.
- [`docs/okf/`](docs/okf/) — LLM-wiki guides: pattern, vault building,
  integrations, and scaling.
- [`engine-manifest.yaml`](engine-manifest.yaml) — engine-layer file list and
  pinned dependencies.

## Dependencies

Runtime dependencies are cloned separately and pinned in
[`engine-manifest.yaml`](engine-manifest.yaml):

- **[Hermes-Agent](https://github.com/NousResearch/hermes-agent)** — the agent
  runtime. OKEngine consumes it as a pinned dependency and carries a small patch
  set under `patches/`.
- **[cron-plus](https://github.com/jalewis/hermes-cron-plus)** — subprocess-per-job
  scheduler for the cron fleet.

## License

Apache-2.0 — see [`LICENSE`](LICENSE). Carried patches under `patches/` are diffs
against Hermes; Hermes' own license governs Hermes code.
