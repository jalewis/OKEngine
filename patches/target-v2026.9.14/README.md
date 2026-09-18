# Hermes v0.21.3 carried patch set

`patches/apply.sh` selects this directory when the manifest pins `v2026.9.14`.
Each artifact must pass `git apply --check` against the exact clean target and
retain its negative behavioral fixtures.

`inventory.json` maps every original carried ID to its final port or retirement.
The existing GitLab patch-inventory
command now audits this target registry as well as the live v0.18.2 set. Its
final mode requires all decisions to be `ported` or `retired` and an actual
checkout at the peeled target SHA to verify the 24-artifact/30-existing-file
adapter threshold. The final set has 22 artifacts, touches exactly 32 files
that exist in pristine v0.21.3, and retires patch 05 while preserving patch
17's forced Responses behavior. The two-file excess records the #630
maintained-runtime decision: Hermes mutates config and SOUL state before an
external adapter can preserve the operator-selected values, so patch 18 also
guards those migrations. The negative inventory fixtures reject
unregistered artifacts, missing declared tests, and budget breaches.

`04-usage-pricing-models.patch` does **not** replay the old flat-peak table.
Candidate v0.21.3 already matches the [current native DeepSeek off-peak
rates](https://api-docs.deepseek.com/quick_start/pricing/), and Pro remains
Pro after 2026-09-14. This target port doubles only the native direct-provider
cost estimate during published UTC peak windows, and rejects a DeepSeek-labelled
non-native endpoint's implicit vendor rate. Response-time calls use the current
aware UTC clock unless an explicit aware billing time is supplied. Historical
Insights now uses an existing durable response-time cost when it has a status,
and labels native direct-provider aggregate-only usage **unknown** when there
is no per-response UTC billing time. A disposable real-`SessionDB` Insights
fixture proves that the unknown bucket remains visible. Older stored estimates
can still reflect outdated rates; actual invoice reconciliation and
dependency-complete GitLab gates remain required before final disposition.

`18-cron-max-iterations.patch` restores the per-job cap consumed by cron-plus.
An explicit value must be an integer in `[1, 90]` (bool, null, strings, floats,
zero, negatives, and larger values are rejected), avoiding candidate Hermes'
unlimited parser. Jobs without a cap inherit a valid global lower limit but
remain bounded to the old safe 90-turn cron ceiling. The 90-turn ceiling is a
proposed OKEngine policy for review, not a production boundary derived from
the 144 currently capped fleet jobs. It must be reconciled with pack and
extension contracts before promotion.

`19-cron-receipt-hint.patch` forks only cron prompt guidance when a dict
`output_contract.completion` equals `per-selected-item`. It warns that selected
work cannot be `[SILENT]` and must receive a fenced per-item receipt, without
claiming every fire selected work. Nonreceipt guidance is byte-identical to
pristine v0.21.3, including delivery and recursion rules. This is a prompt
hint, **not** a substitute for cron-plus's hard receipt verification.

`20-cron-evidence-scan-context.patch` removes only the `sys_prompt_override`
taxonomy label from the scanner for vetted assembled skill evidence. The strict
user-authored cron prompt scanner still blocks that text even when skills are
attached (the pristine target skipped strict user scanning in that branch);
actionable assembled
injection, deception, invisible-Unicode handling, and exfiltration patterns
retain their existing tests. The target upstream test that previously required
the taxonomy phrase to block assembled evidence is updated with a provenance
split fixture.

`10-read-only-file-toolset.patch` preserves the two file subsets but closes
their resolved tools to a fixed allowlist even when plugins or MCP aliases
register mutation tools into their names. This constrains those toolset
names; it is **not** a whole-session read-only policy if `file`, `debugging`,
terminal/code execution, or a platform default composite is also enabled.
The port includes hostile registry/alias fixtures and an explicit mixed-file
selection fixture. Session-wide policy must be checked in each pack and
extension configuration before promotion.
The hostile-registry fixtures fail on a disposable old-style port containing
only the two static toolset definitions (both names resolve mutation tools),
and pass with this target resolver guard. This is the negative gate for the
specific registry/alias escape, not proof of whole-session confinement.

`11-http-status-retry-policy.patch` ports the local-pool total-attempt contract
through candidate v0.21.3's split agent initialization, retry loop, classified
recovery, and terminal recovery surfaces. It applies a configured status's
attempt bound to the current API block (first request counts), suppresses
eager/auth/terminal fallback for `fallback: false` from the first failure,
and prevents an exhausted policy rule from refunding primary retries through
special recovery or transport-client rebuild. A status absent from the policy
retains the generic retry budget; malformed rules are ignored. The disposable
pristine-target negative 503 fixture made eight requests through generic
recovery/restarts instead of the documented six; the ported target makes six.
Fresh-target tests cover 404/500/503/504, mixed statuses, 429 eager fallback
true/false, 401 authentication fallback false, malformed policy shapes, and
generic no-policy behavior (39 focused/adjacent tests passed). This is **not**
an approved local-pool deployment: full GitLab gates, pack/extension policy
reconciliation, and endpoint-observable retry/fallback evidence remain open.
An additional target-source check found that candidate output-cap recovery
called `_compress_context` even with `compression.enabled: false`, then
reported `compression_exhausted` after repeated provider rejection. The
target-only guard retains the bounded max-token clamp, skips history
compaction in disabled mode, and classifies repeated output-cap rejection as
`output_cap_exhausted` in either mode instead of requesting a false gateway
context reset. Full-agent fixtures cover the disabled-compaction overflow
refusal and relay-wrapped HTTP 429 output-cap correction with compaction both
on and off; disposable mutations separately prove that disabled-mode history
compaction, enabled-mode behavior loss, and false reset signals are rejected.
These additions are locally tested but
not yet GitLab-qualified or endpoint-observed.

`12-mcp-resource-uri-guidance.patch` ports the useful distinction between an
MCP resource URI and a host-local path into candidate v0.21.3's split schema
and handler modules. It does **not** replay the old blanket claim that only
`list_resources` URIs are valid: the [MCP resource specification](https://modelcontextprotocol.io/specification/2025-11-25/server/resources)
allows `file://` resources and server-advertised URI templates. The target
schema directs listed-resource reads to the exact URI, acknowledges template
instances, and suggests `read_file` or an available vault `get_page` tool for
the corresponding non-MCP tasks. A conditional correction is appended only
when a failed `file://` call looks like resource-not-found/invalid-URI; valid
listed or template-derived `file://` reads and transport failures keep their
normal behavior. Pristine-target negative fixtures fail the missing guidance;
the port passes 7 focused plus 22 adjacent MCP tests. The broader upstream
`test_mcp_tool.py` module cannot collect on this host because optional MCP SDK
sampling types are absent; GitLab dependency-complete evidence remains open.

`13-cron-completed-tool-handlers.patch` changes the per-fire cron-plus execution
counter from assistant rows containing `tool_calls` to concrete handler returns.
Candidate v0.21.3 can synthesize an assistant tool call and error response for
an invalid name without dispatching a handler; the old counter called that
executed work. A disposable old-style negative fixture fails (1 instead of 0),
while the fresh target port passes 11 focused/adjacent tests. A connector
transport/error envelope also stays at zero; only its response can count.
The ContextVar
sink follows cron worker contexts, resets between fires, and does not turn a
telemetry error into a duplicate tool retry. A positive count still proves
neither item relevance nor durable write; cron-plus receipt/write verification
remains a separate hard gate. Its `executed_tool_call_turns` stored field and
other consumers must be reconciled with this new handler-count meaning before
promotion. Broader host tests hit an independently reproduced upstream
Python 3.14 daemon-pool incompatibility; dependency-complete GitLab evidence
remains open. In the eight-port combined run, the existing disposable cron
fixture needed an explicit model to satisfy the staged turn-limit policy;
the port adds that model rather than bypassing the gate. All 97 staged-port
focused tests then pass together on the disposable target.

`14-cron-executed-writes.patch` replaces the old post-turn message-history
regex/fallback with dispatch-time evidence from the registered
`mcp__okengine_write*` handler's returned MCP success line. It records the
server's canonical post-shard relative path, operation, and version, never the
model's requested path or a synthetic tool-role row. Error envelopes, refused
writes, unsafe paths, malformed acknowledgements, and unrelated MCP namespaces
produce no write receipt. A disposable old-style negative fixture incorrectly
accepted a forged tool-result row; the port rejects it. The real candidate MCP
result renderer contract and 27 focused/adjacent tests pass. The nine staged
ports also pass 119 focused tests together. Cron-plus must
still read back the target page and reconcile selected-item identity; this
telemetry alone is not durable-write evidence. A successful response followed
by a late client-visible read remains required in GitLab/canary evidence.

`16-llamacpp-props-path-policy.patch` learns the successful `/v1/props` or
`/props` route during candidate metadata refreshes, with positive/negative TTLs
rather than the old permanent negative memo. Bare and parameterized router
probes are keyed separately; a missing child cannot suppress a different loaded
child, and unloaded children remain unprobed as upstream intended. Known
llama.cpp context probing skips its unsupported `/v1/models/{model}` detail
URL but retains the useful `/v1/models` list fallback. A pristine-target
negative fixture made that guaranteed detail request; the port did not.
Five new real HTTP-boundary tests plus adjacent native metadata tests passed
42/42 against a disposable target and `HERMES_HOME`; the ten staged ports pass
124 focused tests together. Full GitLab contracts and
measured canary 404-rate evidence remain open.

`21-responses-attribution.patch` adds bounded, header-safe OKEngine client and
conversation identities at the physical Responses SDK request boundary. It
preserves case-insensitive caller headers, emits a one-shot marker when no
session exists, and does not alter Chat Completions calls. A disposable
pristine v0.21.3 wire fixture fails because `X-Client-Id` is absent; seven
target-port tests pass against the exact pinned OpenAI SDK, including real
MockTransport requests, retry, and a second summary-like request. This is
target-only attribution, not approval to switch the five active Qwen/plain
custom deployments to Responses: API-mode/provider policy and live routing
must be decided separately before promotion.

`09-22-cron-mcp-scope.patch` combines original patches 09 and 22 using native
v0.21.3 `discover_mcp_tools(allowed_mcp_names=...)` instead of replaying a
server-registration overlay. An explicit empty or `no_mcp` allowlist remains
MCP-free, and a built-in-only job list no longer inherits globally enabled
MCP servers. Malformed/null lists and platform-resolution errors cannot widen
to full defaults. Discovery sees only the resolved job toolsets, and each named
MCP server (including OKEngine read/write surfaces) must offer its own
canonical tool in the active profile before agent construction. An unrelated
connected server or one missing half of read/write cannot satisfy that gate;
an optional local-only lane can survive MCP discovery failure. Three disposable
pristine-target negative fixtures fail at empty-list widening, built-in-only
MCP widening, and missing required-surface refusal. The port passes 32
focused/adjacent cron tests.
This gate still requires dependency-complete GitLab tests, pack/job allowlist
reconciliation, and scheduled read/write evidence before promotion.

`23-mcp-registry-recovery.patch` ports the carried reconnect/registry invariant
onto the decomposed v0.21.3 discovery, registration, agent-turn, and cron
surfaces. A configured profile-visible live connection can republish its own
missing handler once; a known parked server receives a reconnect nudge and a
poll bounded to five seconds. A synchronous full discovery call is deliberately
absent from the miss path because its native lock/connect waits can exceed the
recovery budget. Disabled/unconfigured servers, lossy-name collisions, and
differently authenticated routes fail closed. Registry dispatch emits an
authentic, turn-scoped miss signal only after recovery fails; an untrusted MCP
result containing `Unknown tool` cannot forge it. The agent interrupts only
for a tool in its advertised snapshot, and cron rejects any retained loss
before writing a successful audit outcome. Pristine-target negative fixtures
fail live-handler republish and permit a clean cron fire despite a retained
registry loss; ten focused target tests include the observable failed
`run_job` result and its disposable `usage_audit.jsonl` error row. Full GitLab,
The same staged artifact now records terminal OKEngine read/write transport
failure at structural handler paths: disconnected session, transport-only
open breaker, host-generated retry failure, or unrecovered RPC exception.
Application-only breaker rejection, re-authentication requirements, and
untrusted tool text do not become a transport signal. A real registry-dispatched
disconnected handler plus a success-shaped
agent response produces a failed cron result and durable audit error; removing
the handler signal makes that fixture fail. The merged patch applies after the
other ordered target artifacts in a disposable v0.21.3 source, and 16 adjacent
transport/registry/breaker tests pass locally. This is not runner or live
reconnect evidence. Full GitLab, real transport drop/reconnect, standing `fleet_status`
read-MCP diagnosis (#608), and scheduled-path evidence are still required.
The inventory's separate `upstream_contract_tests` list includes pristine
v0.21.3's TaskGroup transport-reconnect regression. It is run beside the
ported-patch tests without falsely claiming that patch 23 changes the upstream
test file; the target gate rejects malformed, duplicate, overlapping, or
missing supplemental test paths. Passing that source contract is not live
evidence that the fleet's current TaskGroup parks have stopped.
Because patch 23 also touches the complete cron prompt-preparation function,
its declared prompt-gate fixtures now check the native corrupt-config refusal
for agent jobs and the `no_agent` watchdog exemption with a real allowed script.
Disabling the config gate in a disposable target tree made the refusal fixture
fail; restoring it made the fixtures pass. The same disposable home exercises
the JSON `wakeAgent=false` early return, a failed script's report-only prompt,
and injected-user-prompt refusal without constructing an agent. These tests
cover existing native behavior, not a claim that patch 23 fixes config parsing
or #608. Four setup-sequence fixtures also preserve the scope boundary:
malformed per-job toolsets block instead of widening, a required OKEngine read
MCP surface with no canonical handler blocks, and a built-in-only job survives
optional MCP discovery failure without acquiring unrelated toolsets. Discovery
is an autospec-enforced external connector stub; the scheduler's allowlist and
blocking decisions are real. An unknown delivery platform blocks in preflight
before runtime setup or MCP discovery. None proves the live TaskGroup read-MCP
park has recovered.

The supplemental list also runs v0.21.3's native Responses request-failure
diagnostics, phase-aware TTFB watchdog, and full `AIAgent` Responses regression
modules. Patch 21 touches the complete streaming function, so the target
coverage gate must include its existing retry, failure, interrupt, and
terminal-state behavior as well as OKEngine's attribution tests. These native
modules do not by themselves prove the five active custom Qwen endpoints have
the same wire contract; patch 17's route/preflight policy remains open.

`03-script-failure-report-only.patch` ports the failed data-collection script
guard through v0.21.3's split prompt preparation and agent construction. A
failed script now creates a report-only agent with an explicit empty toolset
allowlist rather than denying just `terminal`, `file`, and `code_execution`:
the three-name list would not cover MCP writers, plugins, or future/composite
toolsets. The prompt quotes the error and forbids investigation or repair.
A disposable negative run on the thirteen-port target still gave the failed
script's agent all three requested mutating toolsets; the port gives it no
tools while a successful script retains the requested list. Five focused and
adjacent cron tests pass. This patch must apply **after** `09-22` and `23` in
the target manifest because its scheduler hunks use their resolved-agent and
registry-telemetry context. Full GitLab and scheduled-path evidence remain
required before disposition.

`01-file-operations-vault-guard.patch` ports the carried vault-file boundary
across candidate v0.21.3's `ShellFileOperations` write, replace, delete, and
move methods; V4A operations funnel through those methods. A weak model's
line-number/pipe-frontmatter read echo is refused without rejecting ordinary
Markdown tables. Vault Markdown creation, engine/pack-reserved updates,
tombstone resurrection, and invalid schema writes are refused before mutation.
Unlike the carried host-`realpath` guard, the new probe resolves paths and
symlinks in the terminal backend's cwd. Writes follow final symlinks, while
`mv`/unlink check the link itself; a move over an existing page validates the
source content at the destination's governing schema, including a symlink
being replaced. An unavailable/malformed backend probe or missing validator
fails closed while `WIKI_PATH` is configured; the schema validator's own
runtime error profile remains fail-open. Two original negative fixtures
failed on the nineteen-port target before the guard, and later move/schema
and symlink fixtures caught two partial-port bypasses. The final target with
the actual engine overlay passes 15 focused real-subprocess tests plus 102
adjacent native file/V4A tests (two Windows-only tests are not runnable on
this Linux host). Full GitLab, built-image, remote-backend contract, late
client-visible readback, and canary evidence remain open; the probe/write
gap is a residual race requiring review.

`02-doubled-write-path.patch` retains the carried CWD-confusion refusal at
the candidate `write_file_tool` dispatch boundary, but checks the path after
task-aware resolution rather than calling host `Path.resolve()` on the raw
agent-relative string. This matters when the gateway process sits in a
different checkout from the agent's terminal cwd, and avoids host symlink
dereference for a container backend. A disposable negative fixture on the
fourteen-port target accepted `wiki/index.md` from a terminal already in
`vault/wiki`, yielding `vault/wiki/wiki/index.md`; the port refuses before
the write handler, while a normal `reports/index.md` is passed as the correct
absolute sibling path. Thirteen focused/adjacent path tests pass. Like the
original, this guard covers `write_file_tool`, while patch01 now covers direct
`ShellFileOperations` and V4A mutation. Other mutation surfaces and the
probe/write race need final safety review. GitLab and canary evidence remain open.

Original patch `05-delegate-tool-session-end.patch` is a **retirement
candidate**, not a target port. Pristine v0.21.3 moves child cleanup to
`tools/delegate_tool_child_run.py`; both normal cleanup and the timed-out
worker's Future callback call `child.close()`. Native `AIAgent.close()` calls
`_finalize_owned_session_row()`, which stamps `end_session(...,
"agent_close")` before releasing the child's dedicated `SessionDB` handle.
The delegate builder marks that handle as owned and does not disable
end-on-close. A test-only target artifact builds a real parent and delegated
child, creates the child row at the conversation-start boundary, then checks
normal and Future-deferred close. Both tests read back non-null `ended_at`
plus `agent_close` through a fresh `SessionDB` reader, and verify that deferred
close does not end the row before worker completion. The focused tests pass
(2/2) in a disposable dependency-complete Hermes environment. Removing native
session finalization in a disposable negative fixture made the normal test fail
with its intended open-row assertion; the source change was restored. A fake
child's `close()` that merely closed the DB had produced a false negative and
is not a valid upstream contract test.
No OKEngine consumer depends on the old `delegate_complete` reason string.
Dependency-complete CI, both normal and deferred delegation integration, and
canary row evidence remain required before the patch is formally retired.

Original `15-model-metadata-probe-cache.patch`'s permanent URL-only memo is
**superseded** by pristine v0.21.3: native server-type detection normalizes
the server root, caches positive verdicts for one hour, caches negative
verdicts for five minutes in memory, and persists successful probes for five
minutes. Five native positive/negative/expiry fixtures passed. The old
process-lifetime memo would be unsafe in a long-lived multiplexed gateway.
However, pristine target keyed those verdicts and its disk L2 by URL **only**:
a 401 under one profile hid the same endpoint from an authorized profile, and
an authorized disk result leaked a false capability verdict to a different
key. Both real-`httpx.MockTransport` negative fixtures failed on pristine and
the fifteen-port target. `15-server-type-auth-cache.patch` is a **replacement
hardening port**: it keys both memory and disk by normalized URL plus a
non-reversible credential fingerprint, preserves the native TTL and legacy
unauthenticated keys, and avoids caching callable/minted tokens without a
stable identity. A token-rotation fixture and existing native probe/disk
fixtures pass together (23 tests). Full GitLab, image, and cross-profile
canary behavior are required before production disposition.

`06-cron-per-job-ollama-num-ctx.patch` restores the carried per-job Ollama
served-context override through v0.21.3's split cron construction, AIAgent
forwarder, and initialization. A task-local positive integer takes precedence
over shared `model.ollama_num_ctx`; absent jobs keep native configuration and
metadata detection. Invalid explicit values (including bool, zero, negative,
string, float, and containers) fail before constructing a cron agent, and
the direct agent parameter also rejects malformed values rather than silently
coercing them. The effective smaller served window clamps the compressor.
A disposable negative fixture failed all 12 initial contracts before the
port; the fresh pinned target with all preceding staged ports passes 27
focused/native tests. The engine profile pre-deploy validator now rejects
malformed context values too (an eight-shape negative fixture plus the
framework validation path). This does not establish an approved fleet-wide context
size or prove a serving endpoint accepts every requested value; profile/job
policy, GitLab, and live request/compaction evidence remain open.

`07-api-server-default-runtime.patch` restores an API-chat-only default
provider/model without changing the bulk gateway default. It resolves and
replaces the provider, key, endpoint, API mode, request overrides, and
capabilities as one tuple, then leaves per-request and session overrides to
the native v0.21.3 precedence. Unlike the carried v0.18.2 patch, an explicit
unconfigured provider or resolver failure does **not** silently fall back to
the bulk route or to paid OpenRouter. Provider-only configuration uses that
provider's default model or fails closed if none exists. Three original
negative fixtures failed on the staged target before this port; nine focused
boundary tests and three adjacent routing/creation tests pass after it.
The provider alias and model policy, GitLab gates, image artifact, and live
API-versus-bulk evidence remain open before production disposition.

`08-web-search-rotation.patch` ports the opt-in `web.backend: rotate`
selection through v0.21.3's search/extract split. Pristine target treated
`rotate` as a literal provider name, so four disposable negative fixtures
failed. The port round-robins currently available, registered,
search-capable providers under a lock; Serper joins via the existing engine
overlay plugin, while paid xAI stays outside the inherited default ring.
The shared setting selects a stable extraction-capable provider, never a
search-only Serper/Brave/ddgs backend; an explicit capability-specific
`web.search_backend` or `web.extract_backend` still wins. An observable test
dispatches four searches through real registry entries and sees alternating
common envelopes. Five port tests plus 52 native web-config tests pass after
installing the target's pinned optional `parallel-web` dependency into a
disposable test venv. The carried default ring remains the old cohort
(`tavily`, `exa`, `parallel`, `firecrawl`, `searxng`, `brave-free`, `ddgs`) plus
registered nonlegacy plugins; new native keyed Perplexity/KeEnable are **not**
automatically admitted, to avoid surprising paid routing. That boundary
needs explicit policy/allowlist reconciliation before production promotion;
full GitLab, overlay registration, and canary web-search/extract evidence are
still open.
