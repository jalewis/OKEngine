# OKEngine carried patches

OKEngine treats **Hermes as a pinned dependency** and ships its own small
set of patches against core Hermes files. These are **carried** — re-applied on
each Hermes version bump — **not** submitted upstream. They are kept deliberately
small and almost entirely additive so re-applying is cheap and low-conflict.

**Pinned Hermes:** `v2026.6.5` (Hermes **v0.16.0**), commit `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`
(recorded in `engine-manifest.yaml`; `build-engine-image.sh` verifies the clone matches).
**Apply:** `patches/apply.sh /path/to/hermes-checkout` (idempotent; verified to
apply clean to a stock `v2026.6.5` tree).

Everything else the engine adds is **overlay** (new files — see
`engine-manifest.yaml`) or **plugins** (Hermes' plugin system) — neither needs a
patch.

| # | Patch | File | What / why |
|---|---|---|---|
| 01 | `01-file-operations-write-guard.patch` | `tools/file_operations.py` | The **OKF write-guard** hook (calls `tools.schema_validator.schema_reject_reason` to reject non-conformant writes) **+** the read-echo corruption guard (rejects writes where a weak model echoed the line-numbered Read display back into content — a real corruption mode). The schema hook is the one *truly OKF-specific* patch; the rest below are generic hardening. |
| 02 | `02-file-tools-read-echo-guard.patch` | `tools/file_tools.py` | Read-echo detection helpers (`_looks_like_line_numbered_read_output`, doubled-path) — companion to 01. |
| 03 | `03-cron-scheduler-no-agent-failure-guard.patch` | `cron/scheduler.py` | `no_agent` short-circuit (pure-script crons skip the LLM entirely) **+** the report-only **failure-path toolset guard** (a script-failure agent gets `terminal`/`file`/`code_execution` stripped, so a report-only failure handler cannot run destructive commands like `rm` against the vault). |
| 04 | `04-usage-pricing-models.patch` | `agent/usage_pricing.py` | Pricing entries upstream lacks (deepseek-v4-flash/pro, Claude 4.x) so `insights` cost tracking isn't `unknown`. |
| 05 | `05-delegate-tool-session-end.patch` | `tools/delegate_tool.py` | End delegate sub-agent rows in `state.db` (without it, sub-agent sessions leak `ended_at IS NULL` rows forever). |
| 06 | `06-approval-vercel-sandbox.patch` | `tools/approval.py` | Add `vercel_sandbox` to the recognized sandbox env-type allowlist. |
| 07 | `07-cron-trusted-digest-looser-scan.patch` | `cron/scheduler.py` | Scan a cron job's **trusted prerun-script digest** with the **looser** ruleset Hermes already uses for install-vetted skill docs (drops command-shape patterns, still blocks real injection directives). An OKF vault compiles security intel that *describes* attacker commands (e.g. `cat ~/.aws/credentials`), which tripped the strict `read_secrets` pattern and permanently blocked ingest/curation crons (~40% of runs). The digest is engine-generated from vault content, not user/skill input. |

If a patch fails to apply after a Hermes bump, `git apply --3way` it against the
new version, resolve, and regenerate the `.patch` (`git diff <new-pin> -- <file>`).
