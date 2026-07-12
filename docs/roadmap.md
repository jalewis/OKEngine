# OKEngine roadmap

OKEngine is an **engine for swappable-topic LLM wikis** — the catalyst is Karpathy's
LLM-maintained-wiki pattern. A live deployment is OKEngine @ a pinned Hermes + one pack or bundle;
**security is the first concrete domain profile**. Pages are portable via **OKF** (Open
Knowledge Format), a small interoperability floor — not the origin or center.

This roadmap ties planned work to issues and priority. It supersedes the earlier roadmap stub
(okengine#59); the live prioritization is maintained in okengine#79.

## Shipped (foundations)
- **Multi-source MDM / canonical golden-record overlay** — per-source observations + a
  deterministic assembler (Admiralty-weighted fusion, conflicts preserved with attribution),
  over-merge resolver, split-forward migration, reader conflict/provenance view, ATT&CK
  relationship edges, and live multi-source vulnerability fusion (epic #38 — closed).
- **Agent chat as wiki-as-memory** — vault-first → web research → write-back, with the search
  index reindexing on write so recall is immediate (#47, #80).
- **Write-path integrity** — field-drift normalization (#46) and one-level entity sharding +
  duplicate reconciliation (#48).
- **Public-release prep (P0)** — framing scrub off OKF-first (#81), version aligned to 0.2.0
  (#82), the framing docs committed (#83), GitHub chosen as the public home with SECURITY +
  badges + publication checklist (#85/#86/#87/#88), and a clean git-history secret scan
  (#84/#89) — all closed. The repo is publish-ready; only the manual GitHub mirror push remains.
- **Post-deploy verifier** — live reader / MCP read+write / auth / cron-plus / index checks,
  wired into `deploy.sh` as the final step (#67).
- **Engine hardening & CI** — schema-cache fail-open (#49), MCP token fail-closed (#50) + `limit`
  clamp (#51), `update_entity` empty-body clear (#52), reader throttle default (#53); CI
  dep/security scan (#54), docker build smoke (#55), coverage (#56), limited type-check (#57),
  dependency pinning (#58) — shipped as runnable `make` targets plus GitHub CI jobs.

## ✅ Shipped since this roadmap was drafted (as of engine v0.11.x)
Several items listed below as "near-term/later" have SINCE SHIPPED — cross-referenced with the
code, not this list (invariant-audit / okengine#209 — a roadmap must not present delivered
capabilities as future work):
- **Operator dashboard** (`operator_dashboard.py`, the cockpit) — #60.
- **Human review workflow** (`framework review`, `review_queue.py`) — #69.
- **Backup & restore** (`framework backup` create/verify/restore/prune) — #65.
- **Pack upgrade workflow** (`framework upgrade` + versioned `migrations/` + rollback gates) — #61/#66.
- **Multi-pack composition preview** (`framework compose-preview`) — #72.
- **Structured observability** (`health_export.py` — Prometheus textfile + webhook + heartbeat) — #64.
- **Extension/plugin system** — 17 shipped tier-1 extensions + full enable/disable/compose lifecycle — #63.

The lists below are the ORIGINAL #79 plan, kept for provenance; treat the shipped items above as done
and rebuild the forward plan from the current open issues.

## Near-term (original #79 plan — see ✅ Shipped above for what has since landed)
1. **Operator dashboard** (SHIPPED) — cron health, ingest freshness, queue/budget, schema drift,
   review queue, broken links, index freshness (#60).
2. **Human review workflow** (SHIPPED) — lifecycle for `needs_review` + reader UI (#69).
3. **Backup & restore** (SHIPPED) — snapshot vault + runtime state, restore, verify, prune (#65).
4. **Pack upgrade workflow** (SHIPPED) — guided diff/merge/validate for `framework pull --update` (#61).
5. **Search/index management UI** — surfaces the qmd/index state the verifier now checks (#68).

## Platform & product (later — several SHIPPED, see ✅ above)
Search/index management UI (#68), content-provenance UI (#70), multi-pack composition preview
(SHIPPED, #72), versioned migration framework (SHIPPED, #66), security hardening profile (#78),
RBAC (#71), structured observability (SHIPPED, #64), formal plugin/extension API (SHIPPED, #63);
import connectors beyond RSS (#76), static export/publishing with a portable OKF projection (#77),
pack catalog UX (#62), one-command local sandbox (#73), golden conformance fixtures (#75),
performance benchmarks (#74).

## Consolidation notes
- **Overlaps to fold, not duplicate:** #60 / #64 / #68 are one *operator-visibility* cluster
  (#67, the verifier, shipped); #78 is the umbrella over the hardening knobs — its #50 / #53
  pieces shipped, leaving #71 (RBAC) + the public-mode profile; #70 builds on the already-shipped
  reader conflict view (#42) — rescoped to what #42 didn't cover.
- **Multi-pack composition (#90, epic)** — #72 (preview/conflict tooling) and #91 (combo
  catalog) are its components; sequence #90 → #72 → #91.
- **OKF stays a compatibility floor:** export keeps a portable OKF projection (#77), but the
  engine is not OKF-defined — topics are swappable; security is just the first profile.
