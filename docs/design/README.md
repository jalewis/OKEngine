# Design documents — status index

Design docs record how a subsystem was reasoned about; they are **not** the current contract (the
code + `engine-manifest.yaml` + the reference docs are). This index gives each a reliable lifecycle
status so a reader can tell shipped architecture from open exploration at a glance (okengine#209 §12).
Where a doc's own header disagrees with this table, this table wins; where it disagrees with the
code, the **code** wins.

Status legend: **Shipped** — implemented and live · **Active** — being built / partially shipped ·
**Draft** — proposed, not built · **Exploratory** — evaluation/notes, no commitment.

| Document | Status | Subsystem |
|---|---|---|
| [composable-okpacks.md](composable-okpacks.md) | **Shipped** | multipack composition (globally-disjoint type ownership) |
| [composable-okpacks-v1-plan.md](composable-okpacks-v1-plan.md) | **Shipped** | the v1 composition build plan (delivered) |
| [composed-schema-spec.md](composed-schema-spec.md) | **Shipped** | base⊕pack⊕extension schema fold + composed artifact |
| [extension-system.md](extension-system.md) | **Shipped** | three-tier extension discovery + owned/extended schema |
| [extension-lifecycle.md](extension-lifecycle.md) | **Shipped** | enable/disable/compose lifecycle |
| [discovery-spec.md](discovery-spec.md) | **Shipped** | the extension discovery scanner |
| [scoped-mcp-spec.md](scoped-mcp-spec.md) | **Shipped** | per-extension scoped MCP tokens |
| [sidecar-contract.md](sidecar-contract.md) | **Shipped** (machinery; live execution operator-opt-in) | sidecar extension contract |
| [multi-source-entity-resolution.md](multi-source-entity-resolution.md) | **Shipped** | MDM / canonical golden-record overlay (epic #38, closed) |
| [actor-risk-ranking.md](actor-risk-ranking.md) | **Draft** | a census-grounded actor risk score (v1 not built) |
| [federation-evaluation.md](federation-evaluation.md) | **Exploratory** | cross-vault federation — evaluation, no commitment |

(Domain-specific design docs are kept private and are not listed here.)
