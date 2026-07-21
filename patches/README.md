# OKEngine carried patches

OKEngine treats **Hermes as a pinned dependency** and ships its own small
set of patches against core Hermes files. These are **carried** — re-applied on
each Hermes version bump — **not** submitted upstream. They are kept deliberately
small and almost entirely additive so re-applying is cheap and low-conflict.

**Pinned Hermes:** `v2026.7.7.2` (Hermes **v0.18.2**), commit `9de9c25f620ff7f1ce0fd5457d596052d5159596`
(recorded in `engine-manifest.yaml`; `build-engine-image.sh` verifies the clone matches).

**Apply:** `patches/apply.sh /path/to/hermes-checkout` (idempotent; verified to
apply clean to a stock `v2026.7.7.2` tree, every patched file `ast.parse`-clean after).

Everything else the engine adds is **overlay** (new files — see
`engine-manifest.yaml`) or **plugins** (Hermes' plugin system) — neither needs a
patch.

| # | Patch | File | What / why |
|---|---|---|---|
| 01 | `01-file-operations-write-guard.patch` | `tools/file_operations.py` | The **OKF write-guard** hook: rejects non-conformant writes (`tools.schema_validator.schema_reject_reason`), Read-echo corruption, AND — so the file tool can't be a weaker second write path around the enforced okengine-write MCP — the write path's structural refusals for `.md` writes under `WIKI_PATH` — engine-managed **reserved vault files** (`HOT/log/INDEX*/health/bundle/_`- and `.`-prefixed), **pack-declared `reserved_files`** (`schema_validator.reserved_files_for`), and a page whose content is **`status: tombstoned`** (never resurrect; reads the whole page, CRLF/case-tolerant). Enforced on **every write leg** — `write_file`, `patch_replace`, **and `move_file`/`delete_file`** (so the V4A Move/Delete patch ops can't bypass it). invariant-audit M12 (+ re-verify). The one *truly OKF-specific* patch. |
| 02 | `02-file-tools-doubled-path-guard.patch` | `tools/file_tools.py` | The **doubled-path-segment guard** (rejects writes to `wiki/wiki/…`-style CWD-confusion paths). SLIMMED at the v0.18.0 bump: upstream implemented its own read-echo guard (`_is_internal_file_tool_content`), so our read-echo half was dropped as absorbed. Re-anchored (content unchanged) at the v0.18.2 bump. |
| 03 | `03-cron-scheduler-failure-path-guard.patch` | `cron/scheduler.py` | The report-only **failure-path toolset guard** (a script-failure agent gets `terminal`/`file`/`code_execution` stripped so it can't mutate the vault while "reporting"). The former `no_agent` half is NATIVE in v0.18.0 (same `job.no_agent` field, same `wakeAgent` contract) and was dropped as absorbed. |
| 04 | `04-usage-pricing-models.patch` | `agent/usage_pricing.py` | Pricing entry upstream lacks (deepseek-v4-flash) so `insights` cost tracking isn't `unknown`. (deepseek-v4-pro and Claude 4.x are upstream-native as of v0.18.0.) |
| 05 | `05-delegate-tool-session-end.patch` | `tools/delegate_tool.py` | End delegate sub-agent rows in `state.db` (without it, sub-agent sessions leak `ended_at IS NULL` rows forever). |
| 06 | `06-cron-per-job-ollama-num-ctx.patch` | `cron/scheduler.py`, `run_agent.py`, `agent/agent_init.py` | Thread a per-job `ollama_num_ctx` from `run_job` → `AIAgent` → `init_agent` (okengine#151). Inert unless a job carries the field. |
| 07 | `07-api-server-inference-model.patch` | `gateway/platforms/api_server.py` | Pin the api_server (interactive chat) model + provider independently of the gateway default (`API_SERVER_INFERENCE_PROVIDER` / `API_SERVER_INFERENCE_MODEL`). Both empty → gateway default. |
| 08 | `08-web-backend-rotation.patch` | `tools/web_tools.py` | **Opt-in web-search provider rotation** (okengine#190): `web.backend: rotate` round-robins across the AVAILABLE backends per call, spreading free-tier rate-limit load instead of pinning one. RESHAPED at the v0.18.2 bump: availability resolves through upstream's `_is_backend_available` chokepoint + the new `web_search_registry`, so plugin providers (e.g. our serper overlay) join the rotation automatically — the hardcoded backend list is gone. Additive — any other value / unset is the stock single-pick. |

**Dropped at v0.18.2 (absorbed upstream):** Serper backend recognition (was 09) —
v0.18.2's web-provider registry resolves ANY `register_web_search_provider()` plugin in
`_get_backend()` / `_is_backend_available()` natively (upstream #28651/#31873/#32698), so the
`plugins/web/serper/` overlay is now a first-class backend with no patch needed.

**Dropped at v0.18.0 (absorbed upstream):** the vercel_sandbox approval allowlist
(was 06) — native in `tools/approval.py`; the read-echo write guard (half of old
02); the `no_agent` cron short-circuit (half of old 03).

If a patch fails to apply after a Hermes bump, `git apply --3way` it against the
new version, resolve, and regenerate the `.patch` (`git diff <sha^> <sha> -- <file>`).
Full bump records (internal): `docs/hermes-upgrades/v2026.7.7.2-v0.18.2.md`,
`docs/hermes-upgrades/v2026.7.1-v0.18.0.md`.

**Watch items to fold into the next bump** (deferred; tracked in GitLab):
- **okengine#255** — pick up the upstream DeepSeek-v4 **pricing snapshot + reasoning-timeout
  floor**. The fleet runs `deepseek-v4-flash`/`-pro`, but this pin predates those entries, so
  budget-guard cost accounting for v4 is approximate and the reasoning timeout uses a default
  (accuracy only — v4 resolves + runs fine, the ids are served natively by the DeepSeek API).
  Reconcile against carried patch 04 (`04-usage-pricing-models.patch`), which may overlap.
