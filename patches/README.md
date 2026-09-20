# OKEngine carried patches

OKEngine treats **Hermes as a pinned dependency** and ships its own small
set of patches against core Hermes files. These are **carried** — re-applied on
each Hermes version bump — **not** submitted upstream. They are kept deliberately
small and almost entirely additive so re-applying is cheap and low-conflict.

**Pinned Hermes:** `v2026.9.14` (Hermes **v0.21.3**), commit
`345cd2b057a452236de401d3534b8502a7465e8d` (recorded in `engine-manifest.yaml`;
`build-engine-image.sh` verifies the clone matches). The active governed artifacts and detailed
dispositions are under [`target-v2026.9.14/`](target-v2026.9.14/). The root-level patch files and
table below are retained as the historical `v2026.7.7.2` set so old deployments remain auditable;
`apply.sh` selects the target directory from the manifest pin.

**Apply:** `patches/apply.sh /path/to/hermes-checkout` (idempotent; the target-contract gate
verifies the selected artifacts against the exact manifest tag and SHA).

Everything else the engine adds is **overlay** (new files — see
`engine-manifest.yaml`) or **plugins** (Hermes' plugin system) — neither needs a
patch.

[`inventory.json`](inventory.json) is the machine-enforced source of truth for ownership,
classification, touched upstream surfaces, tests, retirement criteria, and conflict risk. Run
`python scripts/audit/patch_inventory.py` before every bump. CI rejects an unregistered patch,
header/surface drift, a missing test, or a breached patch/change budget. Release evidence embeds
the validated inventory digest and aggregate risk counts, binding the downstream burden to the
audited release.

The budget is 24 patches and 30 distinct touched upstream files. Crossing either threshold—or
resolving conflicts in the same high-risk surface on two consecutive Hermes upgrades—requires an
architecture decision between a formal adapter and a maintained downstream runtime. A bump must
attempt the inventory against an unmodified pinned Hermes checkout, run every declared test, record
conflicts, and evaluate every retirement criterion; a clean `git apply` is compatibility evidence,
not a reason to retain a patch indefinitely.

| # | Patch | File | What / why |
|---|---|---|---|
| 01 | `01-file-operations-write-guard.patch` | `tools/file_operations.py` | The **OKF write-guard** hook: rejects non-conformant writes (`tools.schema_validator.schema_reject_reason`), Read-echo corruption, AND — so the file tool can't be a weaker second write path around the enforced okengine-write MCP — the write path's structural refusals for `.md` writes under `WIKI_PATH` — engine-managed **reserved vault files** (`HOT/log/INDEX*/health/bundle/_`- and `.`-prefixed), **pack-declared `reserved_files`** (`schema_validator.reserved_files_for`), and a page whose content is **`status: tombstoned`** (never resurrect; reads the whole page, CRLF/case-tolerant). Enforced on **every write leg** — `write_file`, `patch_replace`, **and `move_file`/`delete_file`** (so the V4A Move/Delete patch ops can't bypass it). invariant-audit M12 (+ re-verify). The one *truly OKF-specific* patch. |
| 02 | `02-file-tools-doubled-path-guard.patch` | `tools/file_tools.py` | The **doubled-path-segment guard** (rejects writes to `wiki/wiki/…`-style CWD-confusion paths). SLIMMED at the v0.18.0 bump: upstream implemented its own read-echo guard (`_is_internal_file_tool_content`), so our read-echo half was dropped as absorbed. Re-anchored (content unchanged) at the v0.18.2 bump. |
| 03 | `03-cron-scheduler-failure-path-guard.patch` | `cron/scheduler.py` | The report-only **failure-path toolset guard** (a script-failure agent gets `terminal`/`file`/`code_execution` stripped so it can't mutate the vault while "reporting"). The former `no_agent` half is NATIVE in v0.18.0 (same `job.no_agent` field, same `wakeAgent` contract) and was dropped as absorbed. |
| 04 | `04-usage-pricing-models.patch` | `agent/usage_pricing.py` | Historical v0.18.2 artifact for the former `deepseek-v4-flash`/Pro pricing contract. The active v0.21.3 port preserves historical Pro sessions and prices canonical `deepseek-flash` with explicit direct-provider peak/off-peak handling; see `target-v2026.9.14/README.md`. |
| 05 | `05-delegate-tool-session-end.patch` | `tools/delegate_tool.py` | End delegate sub-agent rows in `state.db` (without it, sub-agent sessions leak `ended_at IS NULL` rows forever). |
| 06 | `06-cron-per-job-ollama-num-ctx.patch` | `cron/scheduler.py`, `run_agent.py`, `agent/agent_init.py` | Thread a per-job `ollama_num_ctx` from `run_job` → `AIAgent` → `init_agent` (okengine#151). Inert unless a job carries the field. |
| 07 | `07-api-server-inference-model.patch` | `gateway/platforms/api_server.py` | Pin the api_server (interactive chat) model + provider independently of the gateway default (`API_SERVER_INFERENCE_PROVIDER` / `API_SERVER_INFERENCE_MODEL`). Both empty → gateway default. |
| 08 | `08-web-backend-rotation.patch` | `tools/web_tools.py` | **Opt-in web-search provider rotation** (okengine#190): `web.backend: rotate` round-robins across the AVAILABLE backends per call, spreading free-tier rate-limit load instead of pinning one. RESHAPED at the v0.18.2 bump: availability resolves through upstream's `_is_backend_available` chokepoint + the new `web_search_registry`, so plugin providers (e.g. our serper overlay) join the rotation automatically — the hardcoded backend list is gone. Additive — any other value / unset is the stock single-pick. |
| 09 | `09-cron-scoped-mcp-init.patch` | `cron/scheduler.py` | Initialize only MCP servers named by the active cron lane's resolved `enabled_toolsets`; prevents unrelated writer lanes from becoming live in a job process and removes the 29-server/402-tool startup fan-out. |
| 10 | `10-read-only-file-toolset.patch` | `toolsets.py`, `hermes_cli/tools_config.py` | Add `file_read` with only `read_file` and `search_files`, allowing model-write lanes to inspect evidence without a native write or patch path around the governed MCP writer. |
| 11 | `11-http-status-retry-policy.patch` | `agent/agent_init.py`, `agent/conversation_loop.py` | Add opt-in per-HTTP-status attempt and fallback policy. This lets shared local inference pools treat capacity `503` as backoff (not provider failover), cap `500` retries, and fail fast on deterministic `404`/`504` responses without changing provider defaults. |
| 12 | `12-mcp-resource-uri-guidance.patch` | `tools/mcp_tool.py` | Make the MCP resource contract explicit in schemas and failed `file://` calls: only use URIs returned by `list_resources`; use `read_file` for explicit local paths and `get_page` for vault pages. Prevents provider-independent tool loops observed with Qwen and DeepSeek. |
| 13 | `13-cron-executed-tool-calls.patch` | `cron/scheduler.py` | Publish the number of assistant turns that actually carried tool calls onto the job dict (`_okengine_executed_tool_calls`), so the receipt verifier can refuse a run that returned a well-formed receipt having executed nothing (okengine#477). Counted exactly as `agent/turn_finalizer.py` counts its own `tool_turns` diagnostic. Written onto the job dict because cron-plus hands `run_job()` the same object it later hands its receipt check — no return signature changes, no other caller affected. |
| 14 | `14-cron-executed-writes.patch` | `cron/scheduler.py` | Publish the writes a run ACTUALLY executed onto the job dict (`_okengine_executed_writes`), taking the **canonical** path from the write tool's own result (`created <path> v<n>`), which `write_server` emits AFTER `_partitioned_create_path` shards the page. The model reports the FLAT path it requested, so receipts failed "accepted write does not exist" for writes that had succeeded, and hand-transcribed sha256 digests were unreliable (okengine#469/#478). Same publish-onto-the-job-dict mechanism as patch 13. |
| 15 | `15-model-metadata-probe-cache.patch` | `agent/model_metadata.py` | Memoize `detect_local_server_type()` per normalized base_url. The probe walks a capability ladder (LM Studio → ollama → llama.cpp → vLLM) in which all but the matching rung necessarily 404; with no cache and five call sites including per-request paths, every agent init re-walked it — the inference host measured ~150 failed probes/hour arriving as bursts of 4 per lane start. Server type cannot change without a restart and this process is per-cron-job, so a per-process memo is safe; negative results are cached too (okengine#478). |
| 16 | `16-model-metadata-probe-shape.patch` | `agent/model_metadata.py` | Stop issuing requests the server cannot answer. (a) Remember which `/props` URL a base actually responds on — llama.cpp builds differ (`/v1/props` current, `/props` older) and the probe tried the prefixed form FIRST on every metadata fetch, so a `/props` server ate a guaranteed 404 each time (86 counted in one sample window). (b) Skip the per-model `GET /v1/models/<model>` when the server is llama.cpp, which serves no such endpoint (9 counted). Complements patch 15, which memoized the capability ladder itself (okengine#484). |
| 17 | `17-qwen-coder-responses-api.patch` | `run_agent.py`, `agent/agent_init.py`, `agent/codex_responses_adapter.py`, `agent/codex_runtime.py` | Route custom-provider Qwen Coder models through Hermes' existing `codex_responses` transport and normalize every request at the shared wire boundary. This covers reconstructed max-iteration summaries and retries as well as normal turns, and emits content-free shape diagnostics on HTTP 400. The local service implements Responses tool calls; its Chat Completions compatibility route is retired. Other custom-provider models remain conservative and retain upstream transport selection (okengine#492/#500). |
| 18 | `18-cron-max-iterations.patch` | `cron/scheduler.py` | Honor a per-job `max_iterations` ceiling before the global interactive default, so bounded backfill lanes fail terminally instead of looping through as many as 90 model/tool turns. |
| 19 | `19-cron-receipt-hint.patch` | `cron/scheduler.py` | Replace the generic `[SILENT]` guidance for per-selected-item jobs with a mandatory receipt instruction. A selected backfill item must always receive an explicit terminal disposition. |
| 20 | `20-cron-evidence-scan-context.patch` | `tools/cronjob_tools.py` | Keep `system prompt override` blocked in operator-authored cron directives, but allow the bare taxonomy phrase in runtime-injected evidence and vetted skill prose. Security feeds commonly use it as a table cell or capability label; it is not itself an actionable override instruction (okengine#506). |
| 21 | `21-qwen-pool-attribution.patch` | `agent/codex_runtime.py` | Attach `X-Client-Id: okengine/<pack>[/<component>]` and a stable session-backed `X-Conversation-Id` to every Responses request at the shared streaming wire boundary; stateless calls use `X-Oneshot`. This makes NAT'd pool usage attributable by deployment and preserves KV routing across retries/compaction (okengine#523). |
| 22 | `22-cron-mcp-surface-enforcement.patch` | `cron/scheduler.py` | Resolve and initialize the explicit per-job MCP allowlist before constructing the agent, then fail the run if a required OKEngine server produced zero canonical MCP tools. Split from patch 09 so both patches remain clean, independently verifiable carried changes (okengine#522). |
| 23 | `23-mcp-registry-recovery.patch` | `tools/mcp_tool.py`, `tools/registry.py`, `agent/tool_executor.py`, `cron/scheduler.py` | Recover an advertised MCP tool when transport parking races re-registration; if bounded recovery fails, preserve the registry-loss signal outside compressed conversation history and fail the cron run instead of allowing a blind success (okengine#608). |

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

**Resolved watch item:** okengine#255 was reconciled during the v0.21.3 bump. The active patch-04
port covers canonical `deepseek-flash`, historical Pro cost semantics, direct-provider billing
windows, and the runtime metadata/timeout behavior exercised by the target contract suite.
