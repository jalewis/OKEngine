# Deployment topology — engine, instance, pack, domain

How the pieces relate at *runtime*, and the two distinct senses of "running
multiple packs." (The *file-layer* boundary — what code is engine vs pack — is
[`../engine-domain-boundary.md`](../engine-domain-boundary.md); this doc is the
*deployment* model.)

## The concepts

- **Engine** — the reusable foundation: pinned Hermes (`INSTALL.md`) + the OKF
  overlay + carried patches + plugins. One codebase, identical for every
  deployment.
- **Instance** — one *running deployment* of the engine: **one vault + one
  `config.yaml` + one cron fleet + one stack** (`gateway` + `okengine-reader` +
  `okengine-mcp` at one port set).
- **Pack** — the domain layer an instance runs (schema + persona + feeds + crons +
  content). See [`../deploy-a-new-domain.md`](../deploy-a-new-domain.md) §1.
- **Domain** — a contract region *inside a vault*, defined by a `schema.yaml`. The
  engine resolves conformance per page by **walking up to the nearest
  `schema.yaml`**, so one vault can hold several domains. A pack supplies a domain.

## Two senses of "multiple packs"

### A) Multiple domains in ONE vault/instance — supported (walk-up)

A sub-tree can drop its own `schema.yaml` and behave as its own domain; the
nearest one governs each page. **What is per-domain vs instance-global:**

| Per-domain (walk-up to nearest `schema.yaml`) | Instance-global (one per instance) |
|---|---|
| conformance gate (`schema_validator.schema_reject_reason`) | qmd search index (spans the whole vault) |
| write-path permissions + review (`governing_policy`) | `tier-refresh` (reads the **root** schema's `tier` block) |
| migrate / reshelve / reshard drains (`okf_migrate`) | the cron fleet (one `cron-plus/jobs.json`) |
| the `types` taxonomy + required fields | `config.yaml`, `.env`, delivery, `okengine-reader`/`okengine-mcp` (serve the whole vault) |

**How it works:** an instance can run multiple related sub-domains in one
vault — a root domain (root `schema.yaml`) plus a sub-domain under its own
`wiki/<subdomain>/schema.yaml`. A `wiki/entities/x.md` validates against the root
contract; a `wiki/<subdomain>/...` page validates against the sub-domain contract.

**Use when:** related domains you *want* together, under the **same trust
boundary**, where a shared search index / cron cadence / config is fine.

### B) Multiple pack deployments = separate INSTANCES (the default for distinct tasks)

Each pack gets its own vault + its own stack (`gateway`/`reader`/`mcp`) + its own
ports + its own crons/config/delivery. `framework init` scaffolds exactly this — a
self-contained pack dir with its own `docker-compose.yml` and `--port-offset` so
several instances can co-tenant one host (e.g. reader 9200/9300/9400, mcp
8730/8830/8930).

**Use when:** a distinct task, a different audience/operator, **or** a different
**trust boundary**.

## The decision rule

```
related domains, same trust boundary, shared cadence OK   ->  same instance (A)
distinct task / different audience                         ->  own instance (B)
different trust boundary (esp. PUBLIC vs PRIVATE)          ->  own instance (B) — non-negotiable
```

**Never co-mingle public and private content in one vault/instance.** An instance
has *one* qmd index, *one* `okengine-reader`/`okengine-mcp` serving the whole vault, and
*one* cron fleet / `.env` / delivery. Put a public domain in a private instance and
the public reader/MCP can surface private pages and the packs share secrets and
schedules. So **a public pack is always its own instance.**

> **Local-first by default; lock down before you expose.** Out of the box the
> stack binds host ports to `127.0.0.1` (`OKENGINE_BIND`), and the MCP ships a
> generic default bearer token (`okengine-local`) so a fresh `docker compose up`
> works without setup — reachable only from the host. To expose the reader/MCP on
> the LAN, set `OKENGINE_BIND=0.0.0.0` **and** real secrets:
> `OKENGINE_READER_PASSWORD` (HTTP Basic auth on the reader) and a strong
> `OKENGINE_MCP_TOKEN` (bearer auth on the MCP). `framework validate` FAILs if the
> bind is widened while either is still default/empty. Public reference instances
> may run the reader open, but must mount **only** public content and should set
> `OKENGINE_READER_PUBLIC=1` (disables pandoc/WeasyPrint exports, rate-limits the
> expensive endpoints) plus an edge-proxy rate limit — see
> [`okengine-reader/README.md`](../../okengine-reader/README.md#public-deployments).

## Concrete (illustrative)

| Instance | Vault | Domains | Trust | Stack ports |
|---|---|---|---|---|
| **a private instance** | private vault | a root domain + a related sub-domain | private | reader 9200 · mcp 8730 |
| **the reference pack** (okpack-sec) | separate vault | one public security domain | **public** | reader 9400 · mcp 8930 |

A root domain + a related sub-domain can live together (model A) — both private,
related signal, same trust boundary. **A public domain is its own instance**
(model B) because it is public: separate dir, separate git repo, separate stack.
The public reference pack (okpack-sec, a separate repo) builds as model B and touches
nothing in any private instance — they never co-mingle.

## Mental model

> **engine** (one codebase) → **instance** (one vault + stack + cron fleet) →
> **pack/domain** (the content layer). An instance runs one primary pack and *can*
> host extra sub-domains in the same vault (model A) when you want them together;
> a distinct or public task gets its own instance (model B).
