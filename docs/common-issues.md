# Common issues & gotchas

Recurring, non-obvious pitfalls that have cost real time — grouped by what you're doing. Each is
*symptom → cause → fix*. Add to this as new ones recur (keep them real; delete ones a fix has made
obsolete). This is a companion to the authoring guides and to [`pack-building-challenges.md`](pack-building-challenges.md) (the pack-rot failure modes + pre-ship audit), not a replacement.

## Operating a deployment

- **A model-calling no_agent script dies with "Script timed out after 120s".** Hermes bounds cron
  scripts at 120s (`HERMES_CRON_SCRIPT_TIMEOUT` env / `cron.script_timeout_seconds`). Two rules for
  lanes that call a model via llm_lib: (1) never re-scan the corpus to find work — a 36k-page scan
  alone blows the budget; hand work off through a queue file written by a cheap deterministic lane;
  (2) raise the deployment's timeout (600s) and keep the batch bounded — under load a local model
  runs 6-30s per call, so budget for the slow case, not the happy one.
- **Every cron lane is silent — jobs.json deployed fine but nothing EVER fires.** The gateway is
  missing the cron-plus plugin (`/opt/data/plugins/cron-plus/` — deploy-time runtime, not baked
  into the image). `ensure-runtime.sh` installs it pinned; `deploy-cron-plus-jobs.sh` now fails
  loud when it's absent. Symptom signature: `next_run_at: None` on every job, no
  `/opt/data/cron-plus/*.log`, no runner processes.
- **A cron job reports success but nothing changed.** cron-plus `ok: True` only means the subprocess
  didn't crash — NOT that the agent accomplished its task. Always verify the actual **output file
  exists / changed**; poll `/opt/data/cron-plus/pids/` for completion, then check the vault.
- **An agent lane runs for 20 minutes on a 2-minute task.** Agent lanes default to the slowest model
  in the chain unless pinned. Set the model **per job name** in `<pack>/.okengine/cron-models.json`
  (e.g. `"okengine.predictions:grade": "@deepseek"`); it does NOT inherit a sane default.
- **A local reasoning model (qwen3.x) returns empty content / truncates / burns minutes "thinking".**
  Two very different cases:
  - **Gateway lanes are already protected** — the `custom` provider profile
    (`plugins/model-providers/custom/`) disables thinking by default (`think:false` +
    `reasoning_effort:"none"`), and the `deepseek` profile deliberately keeps V4 reasoning ON. An
    **empty** `agent.reasoning_effort: ''` is the optimal setting (qwen off + deepseek on) — do NOT
    "fix" it to a real effort (that re-enables qwen thinking fleet-wide) or to `none` (that kills
    deepseek reasoning too).
  - **Direct scripts bypass the lever — so don't write direct calls.** Anything that hits the
    model endpoint itself gets thinking ON and the reasoning eats `max_tokens` before the answer →
    `content=''`, `finish_reason=length`, parsed as failure. Use **`scripts/cron/llm_lib.py`**
    (`chat()` / `classify()`) — reasoning-off is its default, opt-in is explicit, and the
    truncation signature raises instead of reading as a bad answer.
    `tests/test_llm_call_discipline.py` FAILS the build on raw chat-completions calls in
    `scripts/`/`extensions/`/`tools/` outside the lib (extensions vendor a copy, keeping the
    filename). For reference, the raw knob is `reasoning_effort: "none"` (`/v1`) or `think: false`
    (native `/api/chat`); `/no_think`, `enable_thinking:false`, and `chat_template_kwargs` are NOT
    honored by Ollama-style servers.
- **A selector-gated agent burns its whole turn re-fetching and writes nothing.** The digest lacked a
  "**trust the digest — don't re-fetch**" instruction. Every selector→agent prompt needs it
  (mirror daily-pdb's pattern), or the agent re-reads each source and never gets to the write.
- **`deploy-cron-*.sh` didn't pick up a composed pack change.** The deployment stages its own
  accepted copy; committing upstream does not mutate a running vault. Refresh the pack-owned copy
  through the ownership gate, then deploy it:
  `framework install-domain <deployment> <updated-pack> --refresh --apply`. A normal re-install
  fails loudly when owned cron definitions or scripts differ, and `framework validate` warns if an
  accepted installed copy is later edited, removed, or replaced. `--refresh` is not a force flag:
  unrelated collisions remain blocked.
- **Container won't recreate / runs as the wrong uid.** Recreate needs `ENGINE_DIR` set and
  `HERMES_UID`/`HERMES_GID` matching the running container user (check `docker inspect … --format
  '{{.Config.User}}'`), not the compose default.
- **A UI refuses to start after you set `OKENGINE_BIND`.** By design: a `trust: private` vault
  exposed off-loopback with no `OKENGINE_READER_PASSWORD` fail-closes (okengine#90 P4a). Set a
  password, bind loopback, or declare the pack `trust: public`.

## Authoring a pack / extension / lane

- **Don't port an origin-system pattern verbatim — OKEngine's model differs.** The enforced write path
  changes the calculus:
  - **Single-phase beats two-phase.** The origin system's propose/dispose (agent emits JSON → deterministic
    apply) existed because its agent wrote via raw `file_write` with no guard. OKEngine's write path
    already prevents field-loss + schema violations, and `update_entity(frontmatter_yaml=…, body
    omitted)` merges only given keys and can't touch the body — same safety, single-phase.
  - **`active` is a canonical open status** (`base-schema` `open_values: [open, active]`) — not drift.
  - **Qualitative confidence** (`low`/`medium`/`high`) is valid (flag-not-gate) — don't coerce it.
- **Agent narrative vs mechanical view — pick the right namespace.** Agent-authored synthesis →
  `briefings/`; deterministic no_agent dashboards → `dashboards/`. Target the wrong one and the agent
  self-corrects **silently** (you only notice by finding where the file landed).
- **A drain against a big backlog runs one giant session.** Cap it: `BATCH_SIZE` (e.g. 3–5/run) so it
  drains over successive runs. Also gate the wake on *fixable* work only — don't wake an agent every
  run for something it can't fix (it'll spin forever).
- **Enum values are the agent's responsibility.** List the exact enum in the prompt; the
  conformance-audit lane is the backstop. A deterministic applier should validate field *type*, not
  re-derive enums.
- **Entity paths & refs** — write by-letter, reference by bare slug. See
  [`entity-partitioning.md`](entity-partitioning.md). This is the #1 source of duplicate canonicals.
- **Before adding an "obvious" new lane, read the extension.yaml comments.** They document which
  additions were deliberately skipped as redundant (e.g. predictions' backtest≈calibration,
  portfolio-watch≈calibration+schema-audit). Most "missing" lanes are already covered — verify
  against existing coverage before building (the port work repeatedly collapsed N candidates to ~1).

## Developing the engine

- **`python -m pytest` fails at collection.** Two `test_compose.py` modules collide under the default
  importer. `--import-mode=importlib` is required (now in `pyproject` `addopts`).
- **The narrow pre-commit gate passes but something downstream broke.** For cross-cutting changes
  (`base-schema.yaml`, `schema_lib.py`, `write_server.py`, the validator/compose path) run the FULL
  suite — the 3-test pre-commit gate misses extension/fixture/validator fallout.
- **A "fix" to entity paths that only handles single-char shards.** `_normalize_entity_shard`
  deliberately only collapses single-char segments; multi-char type-dirs slip through untouched.
- **`glab mr create` 404s here.** Create MRs via `glab api projects/<id>/merge_requests -X POST`.
  And **push the branch before** creating the MR — `glab api` with a non-existent `source_branch`
  fails silently (empty response).
