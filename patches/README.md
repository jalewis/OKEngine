# OKEngine carried patches

OKEngine treats **Hermes as a pinned dependency** and ships its own small
set of patches against core Hermes files. These are **carried** — re-applied on
each Hermes version bump — **not** submitted upstream. They are kept deliberately
small and almost entirely additive so re-applying is cheap and low-conflict.

**Pinned Hermes:** `v2026.7.1` (Hermes **v0.18.0**), commit `7c1a029553d87c43ecff8a3821336bc95872213b`
(recorded in `engine-manifest.yaml`; `build-engine-image.sh` verifies the clone matches).

**Apply:** `patches/apply.sh /path/to/hermes-checkout` (idempotent; verified to
apply clean to a stock `v2026.7.1` tree, every patched file `ast.parse`-clean after).

Everything else the engine adds is **overlay** (new files — see
`engine-manifest.yaml`) or **plugins** (Hermes' plugin system) — neither needs a
patch.

| # | Patch | File | What / why |
|---|---|---|---|
| 01 | `01-file-operations-write-guard.patch` | `tools/file_operations.py` | The **OKF write-guard** hook (calls `tools.schema_validator.schema_reject_reason` to reject non-conformant writes). The one *truly OKF-specific* patch. |
| 02 | `02-file-tools-doubled-path-guard.patch` | `tools/file_tools.py` | The **doubled-path-segment guard** (rejects writes to `wiki/wiki/…`-style CWD-confusion paths). SLIMMED at the v0.18.0 bump: upstream implemented its own read-echo guard (`_is_internal_file_tool_content`), so our read-echo half was dropped as absorbed. |
| 03 | `03-cron-scheduler-failure-path-guard.patch` | `cron/scheduler.py` | The report-only **failure-path toolset guard** (a script-failure agent gets `terminal`/`file`/`code_execution` stripped so it can't mutate the vault while "reporting"). The former `no_agent` half is NATIVE in v0.18.0 (same `job.no_agent` field, same `wakeAgent` contract) and was dropped as absorbed. |
| 04 | `04-usage-pricing-models.patch` | `agent/usage_pricing.py` | Pricing entry upstream lacks (deepseek-v4-flash) so `insights` cost tracking isn't `unknown`. (deepseek-v4-pro and Claude 4.x are upstream-native as of v0.18.0.) |
| 05 | `05-delegate-tool-session-end.patch` | `tools/delegate_tool.py` | End delegate sub-agent rows in `state.db` (without it, sub-agent sessions leak `ended_at IS NULL` rows forever). |
| 06 | `06-cron-per-job-ollama-num-ctx.patch` | `cron/scheduler.py`, `run_agent.py`, `agent/agent_init.py` | Thread a per-job `ollama_num_ctx` from `run_job` → `AIAgent` → `init_agent` (okengine#151). Inert unless a job carries the field. |
| 07 | `07-api-server-inference-model.patch` | `gateway/platforms/api_server.py` | Pin the api_server (interactive chat) model + provider independently of the gateway default (`API_SERVER_INFERENCE_PROVIDER` / `API_SERVER_INFERENCE_MODEL`). Both empty → gateway default. |

**Dropped at v0.18.0 (absorbed upstream):** the vercel_sandbox approval allowlist
(was 06) — native in `tools/approval.py`; the read-echo write guard (half of old
02); the `no_agent` cron short-circuit (half of old 03).

If a patch fails to apply after a Hermes bump, `git apply --3way` it against the
new version, resolve, and regenerate the `.patch` (`git diff <sha^> <sha> -- <file>`).
Full bump record (internal): `docs/hermes-upgrades/v2026.7.1-v0.18.0.md`.
