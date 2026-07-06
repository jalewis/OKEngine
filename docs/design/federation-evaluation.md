# Federation — evaluation: a secure instance consuming a curation vault as read-only lookup

**Issue:** okengine#166 · **Status:** design — evaluation only, no commitment
**Relates to:** [`scoped-mcp-spec.md`](scoped-mcp-spec.md) (#132/#138 token + bridge machinery),
[`../okf/deployment-topology.md`](../okf/deployment-topology.md) (walk-up sub-domains, trust rule)

## 1. Topology and the hard rule

Two instances, split by trust: **instance A** (protected vault, sensitive content) consumes
**instance B** (a normal, wholly non-sensitive curation instance) as a read-only *lookup* corpus.
The invariant is directional: **content flows B→A only; A→B is at most a read request.** Whatever
carries the flow — a live query channel or a sync job — *is* the trust boundary and must be
provably one-way. This composes with the existing per-instance trust rule
(`docs/okf/deployment-topology.md:63-67`): B is a separate instance precisely because it is a
different trust boundary; federation must not quietly re-merge them.

## 2. Current state — what the engine already provides

- **The read MCP is a consumable surface by design.** `okengine-mcp/server.py` exposes the vault
  as read-only tools (`search`, `get_page`, `find_references`, `retrieve_context`, `graph_stats`,
  `list_pages`), every result carrying its vault path as provenance (`server.py:9-10,119`).
  Networked via `OKENGINE_MCP_TRANSPORT=streamable-http` + bearer auth (`_ScopedAuth`,
  `server.py:291-320`). Nothing in it mutates the vault.
- **Exposure is gated, with one big caveat.** Binding beyond loopback with the built-in default
  token fails closed (`server.py:256-264`); `framework validate` FAILs a widened bind with
  default/empty secrets (`docs/okf/deployment-topology.md:69-80`). Scoped tokens exist
  (`okengine-mcp/scope.py`, minted by `scripts/extension_tokens.py`) but **only gate
  `get_page`/`retrieve_context` and filter `list_pages`; `search`/`find_references`/`graph_stats`
  are full-vault for any authenticated caller** — a documented v1 deferral (`server.py:24-29`,
  `scoped-mcp-spec.md` §"Deferred"). Consequence for #166: *any* token A holds effectively reads
  all of B. Fine iff B is wholly non-sensitive — which the topology already requires — but it
  rules out "federate only the safe namespaces of a mixed vault."
- **The gateway wires additional MCP servers by config.** `config/config.yaml.template:61-79` is a
  named `mcp_servers:` map (`okengine:` → the read service URL + bearer header; `okengine-write:`
  → the stdio write server). A second read endpoint is one more map entry.
- **Walk-up sub-domains.** The nearest `schema.yaml` governs each page — conformance
  (`tools/schema_validator.py:_find_schema`, :77-114) and write policy
  (`governing_policy`, :336-361) both walk up. A sub-tree with its own `schema.yaml` is its own
  contract region inside one vault (`docs/okf/deployment-topology.md:24-42`). This is the natural
  read-only mount point for a mirrored corpus.
- **Per-namespace write permissions, enforced at the MCP write path.**
  `permissions: {default: {create,update,delete}, namespaces: {<ns>: {…}}}` in `schema.yaml`
  (grammar: `templates/pack/skeleton/schema.yaml:43-48`; resolution `_ns_perm`,
  `okengine-mcp/write_server.py:382-387`; enforcement `_policy_reject`, :390-402, called from
  create/update/patch/append/converge at :639,696,835,905,1018).
- **The search index and reader are instance-global and self-maintaining.** The read MCP's index
  maintainer registers the whole `wiki/` tree and reindexes on change
  (`server.py:323-433`; change poll default 30 s, `OKENGINE_MCP_INDEX_POLL_SECONDS`).
  `list_pages` already scans sub-domain namespaces via the `*/<ns>` glob (`server.py:185`). The
  reader serves the whole vault (`docs/okf/deployment-topology.md:34`).
- **No content-sync verb exists.** `framework pull --update` refreshes a pack *definition*, never
  vault content (`scripts/framework_pull.py` docstring); `framework import` (okengine#154)
  *adopts* a foreign vault by rewriting it into the pack's schema — the opposite of a
  fidelity-preserving mirror. A periodic one-way content sync is a **gap** (small — see §6).

## 3. Pattern 1 — live federated query

A's gateway gets B's read MCP as a second `mcp_servers:` entry, e.g.:

```yaml
# instance A config.yaml (from config/config.yaml.template:61-79)
mcp_servers:
  okengine:            # A's own vault (unchanged)
    url: http://okengine-mcp:8730/mcp
    headers: {Authorization: "Bearer <A-token>"}
  okengine-lookup:     # instance B's read surface
    url: https://<instance-B>/mcp
    headers: {Authorization: "Bearer <B-issued-token>"}
```

**What exists:** everything on the wire. B serves streamable-http with bearer auth today; the
fail-closed default-token guard and `framework validate` gate the exposure; results carry B's
paths, so provenance is automatic. B can mint A a store-bound token via the #132 machinery.

**What it costs:**

- **Query side-channel — structural, not fixable by config.** Every query string A's agent writes
  is sent to B and lands in B's process (and whatever B logs). One query embedding a sensitive
  term leaks it. No engine mechanism constrains outbound query content (§5 Q4).
- A must make outbound calls at all — already disqualifying for an isolated A.
- Scoped tokens don't reduce B's exposure meaningfully (the `search`-tools deferral above), so the
  token buys authentication, not compartmentalization.
- Tool-identity collision: both read servers self-name `okengine` (`FastMCP("okengine")`,
  `server.py:69`). The `mcp_servers:` map key is the config-level identity; whether the pinned
  Hermes disambiguates tools by map key or by the server's self-reported name has **not been
  verified** and must be, before this pattern is used (§5 Q1).

**Verdict:** viable when A is *restricted-access* (private LAN, trusted operators) and live
freshness matters. Not acceptable for an A whose threat model includes what A's agent might say.

## 4. Pattern 2 — replicated read-only mirror

B's namespaces are pulled one-way into A's vault as a `wiki/lookup/` sub-domain; A's agent queries
its **own** read MCP. Assembled entirely from existing walk-up/permission/index primitives:

1. **Sync (the one new piece).** A pulls B's `wiki/` namespaces into `wiki/lookup/` with pull-only
   credentials (rsync over ssh, or `git pull` of a repo B publishes — git gives an audit log of
   exactly what crossed). `--delete`/reset semantics make the mirror self-healing: local drift is
   clobbered on the next sync. A never holds a credential that can write to B — that is the
   provable one-way property. Runs as a cron job in A's fleet (source
   `config/engine-crons.json`, merged by `scripts/cron_pack_split.py`).
2. **Contract.** B's `schema.yaml` lands at `wiki/lookup/schema.yaml`; the sync overlays
   `permissions: {default: {create: false, update: false, delete: false}}` onto it. Walk-up then
   does the rest: mirrored pages validate against **B's** contract, not A's (no conformance-gating
   as if owned), and `_policy_reject` refuses agent create/update anywhere in the subtree
   ("namespace 'lookup' is not agent-writable").
3. **Index/read surface — zero work.** The MCP index maintainer picks the new files up within one
   poll (`server.py:396-418`); `search` results and `list_pages` rows carry `lookup/…`-prefixed
   paths (the `*/<ns>` glob at `server.py:185` even folds `lookup/entities` into
   `list_pages("entities")`), so the agent always sees which vault a fact came from.
4. **Reader — zero work to render.** One reader serves the whole vault; `lookup/…` pages appear at
   their prefixed paths.

**Known imperfections (small, honest):**

- `tombstone_entity` skipped `_policy_reject` (it checked only write-scope + reserved-file + the
  schema gate), so A's agent *could* tombstone a mirrored page — **closed during this evaluation**:
  `_tombstone` now clears the same `update` permission gate as every other mutation
  (`write_server.py`; regression `tests/test_write_server.py::test_tombstone_respects_update_denied_namespace`).
  (`flag_for_review` is not an issue — it only appends to the review queue, never mutates the
  target, `write_server.py:745-765`.)
- The permission matrix binds only the MCP write path (`governing_policy` docstring,
  `tools/schema_validator.py:336-347`). Direct-file writers (repair drains, migrations) are not
  gated by it. Mitigation is the same self-healing sync; drains should nonetheless be checked
  against the mirror before enabling this in anger.

**Verdict:** strictly more isolating — no outbound queries, no query side-channel, one auditable
content-only crossing per sync. The default for a genuinely protected A.

## 5. The six open questions

1. **Tool-name collision.** Mechanism: a second named entry in the gateway's `mcp_servers:` map
   (`config/config.yaml.template:61-79`), e.g. `okengine-lookup:`. Open sub-issue: both servers
   report `FastMCP("okengine")` (`server.py:69`); confirm against the pinned Hermes whether the
   map key or the self-reported name namespaces the tools. If the latter, add a one-line
   `OKENGINE_MCP_NAME` env override in `server.py` so B can self-name `okengine-lookup`. Pattern 2
   dissolves the question entirely (one MCP, path prefixes distinguish the corpora) — a further
   argument for it.
2. **Read-only namespace — expressible today, with one enforcement hole.** The grammar already
   says it: in `wiki/lookup/schema.yaml`, `permissions.default.create: false` +
   `permissions.default.update: false` (+ `delete: false`) — keys read by `_ns_perm`
   (`write_server.py:382-387`), enforced by `_policy_reject` (:390-402), resolved per page by
   walk-up (`governing_policy` → `_find_schema`, `tools/schema_validator.py:336-361,77-114`).
   The same walk-up gives the subtree B's conformance contract, so mirrored pages are not gated
   as if A owned them. **Gaps:** (a) `_tombstone` bypassed `_policy_reject` — FIXED with this
   evaluation (see §4); (b) the `delete` key is collected by `_ns_perm` but never checked in
   `_policy_reject` — `delete: false` is realized today only by the absence of any hard-delete
   tool; stays documented, not built.
3. **Sync mechanism + provenance.** Recommend **pull-from-A** via rsync-over-ssh or a git repo B
   publishes (git preferred: signed, diffable audit trail of every crossing). Explicitly *not*
   `framework import` (#154 — it retypes/re-homes into A's schema; a mirror must preserve B's
   contract) and not `framework pull --update` (definition-only). Provenance: **do not rewrite
   page frontmatter** — mutating mirrored pages breaks fidelity and makes every sync a diff. The
   `lookup/` path prefix is the provenance, and every MCP result already carries the path
   (`server.py:9-10`). If page-level stamping is ever wanted, `maintained_by`/`discovered_by`
   are already reserved universal fields (`config/base-schema.yaml:33-34`) — B can stamp them at
   export; A must not.
4. **Query hygiene (Pattern 1) — gap, and we should not fill it.** No query-filter mechanism
   exists anywhere on the outbound path; the only lever is prompt policy in the pack persona
   (`CLAUDE.md`), which is advisory, not enforcement. A real filter is a DLP engine — out of
   scope. Treat the side-channel as inherent to Pattern 1 and select patterns by threat model
   instead.
5. **Freshness vs isolation.** Pattern 1: live. Pattern 2 staleness = sync cadence + index
   pickup (≤ `OKENGINE_MCP_INDEX_POLL_SECONDS`, default 30 s, plus the debounce cooldown,
   `server.py:357-374`). A curated corpus changes on human timescales; hourly or daily sync
   loses nothing that matters. Freshness is not a reason to accept Pattern 1's side-channel.
6. **Reader UX.** Render it: A's reader serves the whole vault already, and the `lookup/` prefix
   is visible provenance in every URL and rail entry. A visual "external" badge does not exist
   (nothing in `okengine-reader/app.py` styles by namespace origin; `rail_top_section`,
   :378-398, only pins synthesized sections) — a cosmetic gap, not a blocker. Do not build an
   agent-only mode; hiding the corpus from humans who can already query it via Chat buys nothing.

## 6. Recommendation

**Pattern by trust level.** Genuinely protected A → **Pattern 2** (replicated read-only mirror);
the whole point of A is isolation, and the mirror's only boundary crossing is periodic,
content-only, and auditable. Restricted-access A where live freshness is a hard requirement →
Pattern 1 is acceptable, with the query side-channel named in the deployment's threat model.
**Both patterns require B to be a wholly non-sensitive instance** — the `search`-tool scoping
deferral (`server.py:24-29`) means any authenticated consumer can read all of B.

**Exists today** (no code): networked read MCP + bearer auth + fail-closed exposure gates;
`mcp_servers:` multi-entry wiring; walk-up sub-domain contracts; the
`permissions.default.create/update: false` read-only grammar; instance-global self-maintaining
index; path-prefix provenance end to end.

**To build (itemized, small):**
1. A one-way lookup-sync job (script + engine-template cron def): pull-only rsync/git into
   `wiki/lookup/`, land B's `schema.yaml` with the deny-permissions overlay, self-healing reset
   semantics. The only genuinely new piece.
2. ~~Close the tombstone hole~~ — done with this evaluation (`_tombstone` now routes through
   `_policy_reject`; regression in `tests/test_write_server.py`).
3. (Pattern 1 only, if ever pursued) verify Hermes tool-name disambiguation for a second
   `mcp_servers:` entry; add an `OKENGINE_MCP_NAME` override to `server.py` if needed.

**Not yet:** a `framework federate` verb or any federation registry/protocol; bidirectional or
push-based sync (pull-only is the one-way proof); an outbound query filter for Pattern 1;
read-scope filtering of `search`/`find_references`/`graph_stats` (already a tracked #132
deferral — don't couple it to this); reader "external" badging; multi-B fan-in. Evaluate again
when a second consumer or a mixed-sensitivity B actually shows up.
