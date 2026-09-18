# Target-only Hermes v0.21.3 plugin overlay set

This directory is **not** used by the current v0.18.2 image. The image builder
selects it only for exact tag `v2026.9.14` and refuses a missing custom or
Serper plugin. OpenRouter remains upstream-owned and is verified separately.
The production `plugins/` directory stays
unchanged while the target contracts are ported. Do not copy the old provider
profiles here: they would replace v0.21.3 reasoning/routing safeguards.

Serper is staged under `plugins/web/serper/` against the target's real
`plugins.web._common` helpers and `BaseWebSearchProvider`. Its credentials use
the config-aware `provider_env`, and its search envelope, unavailable route,
interrupted route, HTTP/JSON failures, malformed payloads, and count limits
are checked in `tests/test_serper_contract.py`. The contract tests must be run
from a dependency-complete checkout at the exact peeled target commit with
that checkout on `PYTHONPATH`; missing dependencies are failures, not skips.
The test reproduces the current plugin's config-only credential miss before
asserting that the target plugin sees the key.

The custom profile is now staged from v0.21.3's native profile with a small
OKEngine delta: local Chat Completions temperature, default thinking-off and
`num_ctx` only on identifiable Ollama endpoints, and the native per-call
model-fetch signature. The current Qwen Coder `custom` endpoint answered
`Server: llama.cpp` to a read-only `/api/version` probe; its port 11436 does
not identify Ollama. The old overlay sent Ollama-only `think` and `options`
fields there. Nineteen target contracts pass, including that old-overlay
negative fixture, the real transport kwargs, hosted/local separation,
reasoning clamp, malformed values, and temperature/context boundaries.
The target Responses transport does not consult this profile; resolving
patch 17's Qwen wire policy and testing that path remain separate gates.

The old OpenRouter overlay is **retired for this target**, rather than copied.
All of its material additions are native in v0.21.3, whose profile also has
conversation-affinity routing, speed-tier endpoint pins, mandatory Anthropic
reasoning protection, catalog effort clamping, current fallback models, and
per-call model fetch. The target build leaves that module untouched and checks
exact source and manifest hashes after patches; a changed file aborts build.
Eleven native contracts pass, including old-overlay negative evidence and a
manifest-drift rejection. No OKEngine-specific OpenRouter delta was found
that justifies replacing the target module. Final qualification must also verify
Serper's
registration and routing from the immutable image artifact; isolated class
tests are not live gateway evidence. The staged `hermes-target-contracts`
GitLab lane executes all registered patch tests and these three provider/plugin
contracts against a fresh pinned clone; its JUnit and summary must be green
and attributable before this overlay set can qualify for a pin switch.
