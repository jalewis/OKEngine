# Product boundary and runtime adapter decision

Status: accepted. Contract version: 1.

## Primary product

OKEngine is the governed compilation and maintenance plane for evidence-backed knowledge products.
It compiles declarative packs into a governed knowledge-maintenance system, maintains canonical
Markdown under policy, and exposes consistent projections and provenance to product consumers.

The stable kernel is schema, policy, corpus transactions, provenance, deterministic/model-assisted
maintenance, and derived projections. A feature belongs in the kernel only when multiple knowledge
products need it to preserve correctness, governance, or portability. Domain-specific analysis and
presentation belong in applications or extensions.

## Runtime adapter

Hermes is an execution adapter and pinned runtime dependency, not the product boundary. Adapter
contract v1 requires the runtime to:

- start a bounded job with an engine-supplied identity, tool allowlist, model route, deadline, and
  correlation context;
- expose only the requested canonical MCP tool surface and report initialization failures;
- return authoritative tool-call/write evidence and one terminal disposition receipt;
- preserve cancellation, timeout, retry, and resource-limit semantics;
- route canonical mutations through the engine-governed writer rather than native file mutation;
- expose exact runtime/version provenance and remain testable against an unmodified pinned build.

The adapter may schedule and execute agent turns. It does not own schema interpretation, mutation
authorization, canonical identity, transaction epochs, knowledge-quality policy, or projections.
Those remain engine responsibilities. An alternate runtime may implement the same versioned
contract; extensions may not depend directly on undocumented Hermes internals.

## Consumers

Reader, cockpit, and analytical applications are consumers of kernel contracts. They may live in
this repository for coordinated delivery, but their view models and product workflows do not define
the kernel. They consume read/projection, review, operation, provenance, and health APIs. A consumer
capability moves into the kernel only after a cross-product governance or portability need is shown.

## Decision filter

Roadmap, extension, and API proposals must state:

1. which kernel capability or consumer owns the behavior;
2. the stable contract used across the boundary;
3. whether canonical state, policy, or provenance changes;
4. how the behavior works with a different runtime adapter;
5. why an extension or application cannot own it when kernel placement is proposed.

Runtime-specific changes prefer adapter code, plugins, or upstream contributions. A downstream
patch is justified only when the versioned adapter cannot otherwise be implemented and must enter
the governed patch inventory with retirement criteria.

## Explicit non-goals

OKEngine is not a general-purpose agent runtime, a canonical relational datastore, a vertical
application monolith, a universal CMS, or a model provider/inference server. PostgreSQL and search
indexes remain derived planes; Markdown remains canonical. Packs and applications remain free to
define vertical semantics without expanding the engine kernel.

## Compatibility and change control

Changes to the kernel list, component classification, or adapter contract version require a new
recorded architecture decision and migration plan. `config/product-boundary.json` is the
machine-readable decision used by CI; documentation and code ownership must agree with it.
