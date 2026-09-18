# OKEngine dev tasks. See CONTRIBUTING.md. Run `make help` for the list.
.DEFAULT_GOAL := help
.PHONY: help dev editable-install test test-policy test-unit test-integration test-contract test-invariant test-security test-mutation-changed test-mutation-full test-mutation-critical test-mutation-critical-preflight lint package-check wheel prompt-lint scrub preflight test-release release-evidence scaffold-check check audit coverage typecheck diff-check docker-smoke smoke-e2e smoke-release e2e-release resilience-release performance-release sandbox-start sandbox-stop sandbox-reset render-lint content-lint publish-snapshot

# One interpreter for every test/release gate. Prefer the repository's canonical
# environment when it exists; operators and CI can override with TEST_PYTHON=...
TEST_PYTHON ?= $(if $(wildcard $(CURDIR)/.venv/bin/python),$(CURDIR)/.venv/bin/python,python3)
TEST_TIMEOUT ?= timeout --signal=TERM --kill-after=10s
TEST_FAST_DEADLINE ?= 120
# The security layer outgrew the fast deadline it shared. Measured pytest runtime on passing runs:
# 83.2s, 94.2s, 94.7s, 95.5s (pipelines 9719/9721/9725/9734) -- about 1.26x headroom under 120s.
# On a contended runner it reached 89% at the 120s kill (pipeline 9740, job 110495), a projected
# ~135s. A deadline exists to catch a hang, not to fit the work, so this is sized to 2x that
# contended figure. Its own variable, so the six genuinely fast layers keep their 120s bound.
# ci/job-budgets.json records the measurement; tests/test_ci_config.py holds the headroom.
TEST_SECURITY_DEADLINE ?= 300
TEST_INTEGRATION_DEADLINE ?= 600
TEST_FULL_DEADLINE ?= 1200
TEST_MUTATION_DEADLINE ?= 43200
MUTATION_JOBS ?= 1

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

dev:  ## install dev/test dependencies
	python -m pip install -e '.[dev]'

editable-install:
	"$(TEST_PYTHON)" -m pip install -q -e .

test: editable-install  ## run the test suite (mcp-dependent tests self-skip if `mcp` is absent)
	$(TEST_TIMEOUT) $(TEST_FULL_DEADLINE) "$(TEST_PYTHON)" -m pytest

test-policy:  ## collect and audit every test's global layer; write artifacts/test-layers.json
	mkdir -p artifacts
	$(TEST_TIMEOUT) $(TEST_FAST_DEADLINE) "$(TEST_PYTHON)" -m pytest tests/ --collect-only --test-layer-report=artifacts/test-layers.json

test-unit:  ## pure isolated tests; no filesystem/database/network/subprocess I/O
	mkdir -p artifacts
	$(TEST_TIMEOUT) $(TEST_FAST_DEADLINE) "$(TEST_PYTHON)" -m pytest tests/ -m "unit and not external" --strict-layer-skips \
		--junitxml=artifacts/junit-unit.xml

test-integration:  ## real filesystem/database/process/component integration tests
	mkdir -p artifacts
	$(TEST_TIMEOUT) $(TEST_INTEGRATION_DEADLINE) "$(TEST_PYTHON)" -m pytest tests/ -m "integration and not external" --strict-layer-skips \
		--junitxml=artifacts/junit-integration.xml

test-contract:  ## versioned request/response/config interface promises
	mkdir -p artifacts
	$(TEST_TIMEOUT) $(TEST_FAST_DEADLINE) "$(TEST_PYTHON)" -m pytest tests/ -m "contract and not external" --strict-layer-skips \
		--junitxml=artifacts/junit-contract.xml

test-invariant:  ## repository-wide architecture/configuration guarantees
	mkdir -p artifacts
	$(TEST_TIMEOUT) $(TEST_FAST_DEADLINE) "$(TEST_PYTHON)" -m pytest tests/ -m "invariant and not external" --strict-layer-skips \
		--junitxml=artifacts/junit-invariant.xml

test-security:  ## authentication/authorization/input/dependency boundary tests
	mkdir -p artifacts
	$(TEST_TIMEOUT) $(TEST_SECURITY_DEADLINE) "$(TEST_PYTHON)" -m pytest tests/ -m "security and not external" --strict-layer-skips \
		--junitxml=artifacts/junit-security.xml

test-mutation-changed:  ## mutate explicitly mapped production modules changed from MUTATION_BASE
	mkdir -p "$(MUTATION_ARTIFACTS)"
	$(TEST_TIMEOUT) $(TEST_MUTATION_DEADLINE) "$(TEST_PYTHON)" ci/mutation_gate.py --mode changed \
		--base "$${MUTATION_BASE:-origin/main}" --deadline-seconds "$(MUTATION_GATE_BUDGET)" \
		--jobs "$(MUTATION_JOBS)" --artifacts "$(MUTATION_ARTIFACTS)" \
		--shard-count "$(MUTATION_SHARD_COUNT)" --shard-index "$(MUTATION_SHARD_INDEX)"

# The gate's OWN budget, set below the outer timeout so the campaign stops starting targets and
# reports what it did not reach, instead of being killed with the report unwritten (okengine#599).
# 11.5h inside the 12h outer timeout leaves 30 minutes to flush/upload evidence. Admission then
# reserves a further 20%, so each shard may declare at most 9.2h of measured work (#655).
MUTATION_GATE_BUDGET ?= 41400
MUTATION_ARTIFACTS ?= artifacts/mutation
MUTATION_SHARD_COUNT ?= 1
MUTATION_SHARD_INDEX ?= 0
MUTATION_BASELINE_PROOF ?=

test-mutation-full:  ## run the complete reviewed mutation manifest (weekly CI)
	mkdir -p "$(MUTATION_ARTIFACTS)"
	$(TEST_TIMEOUT) $(TEST_MUTATION_DEADLINE) "$(TEST_PYTHON)" ci/mutation_gate.py --mode full \
		--deadline-seconds "$(MUTATION_GATE_BUDGET)" --jobs "$(MUTATION_JOBS)" \
		--artifacts "$(MUTATION_ARTIFACTS)" --shard-count "$(MUTATION_SHARD_COUNT)" \
		--shard-index "$(MUTATION_SHARD_INDEX)"

test-mutation-critical:  ## the declared critical targets — the daily signal (nightly CI)
	mkdir -p "$(MUTATION_ARTIFACTS)"
	$(TEST_TIMEOUT) $(TEST_MUTATION_DEADLINE) "$(TEST_PYTHON)" ci/mutation_gate.py --mode full \
		--critical-only --deadline-seconds "$(MUTATION_GATE_BUDGET)" --jobs "$(MUTATION_JOBS)" \
		--artifacts "$(MUTATION_ARTIFACTS)" --shard-count "$(MUTATION_SHARD_COUNT)" \
		--shard-index "$(MUTATION_SHARD_INDEX)" \
		$(if $(MUTATION_BASELINE_PROOF),--baseline-proof "$(MUTATION_BASELINE_PROOF)",)

test-mutation-critical-preflight:  ## prove every unique critical baseline once before nightly shards
	mkdir -p "$(MUTATION_ARTIFACTS)"
	$(TEST_TIMEOUT) $(TEST_FULL_DEADLINE) "$(TEST_PYTHON)" ci/mutation_gate.py --mode full \
		--critical-only --preflight-only --jobs 1 --artifacts "$(MUTATION_ARTIFACTS)"

# How many consecutive runs without a score before a target counts as dropped out. 1 would fire on
# a single bad night and train everyone to ignore the report, which is how seventeen red nightlies
# became invisible in the first place (okengine#612).
MUTATION_HISTORY_DIR ?= artifacts/mutation-history
MUTATION_HISTORY_THRESHOLD ?= 3

mutation-history:  ## name targets that have silently STOPPED being measured (okengine#612)
	"$(TEST_PYTHON)" ci/fetch_mutation_summaries.py --out "$(MUTATION_HISTORY_DIR)"
	"$(TEST_PYTHON)" ci/mutation_history.py --summaries "$(MUTATION_HISTORY_DIR)" \
		--critical-only --threshold "$(MUTATION_HISTORY_THRESHOLD)"

lint:  ## syntax + real-bug lint (no style enforcement)
	python -m ruff check --select E9,F63,F7,F82 scripts tools okengine-mcp okengine-reader tests
	"$(TEST_PYTHON)" scripts/audit/import_boundary.py

package-check:  ## build and inspect the versioned engine wheel
	rm -rf artifacts/wheel
	"$(TEST_PYTHON)" scripts/build_engine_wheel.py --out artifacts/wheel
	"$(TEST_PYTHON)" -c 'import zipfile,glob; p=glob.glob("artifacts/wheel/okengine-*.whl"); assert len(p)==1; z=zipfile.ZipFile(p[0]); expected={"okengine/cli.py","okengine/operations/framework.py","okengine/operations/run.py","okengine/projection/projector.py","okengine/mcp/scope.py","okengine/mcp/projection.py","tools/schema_validator.py","tools/policy_plane.py","output_contract_enforce.py","converge.py","id_lib.py","schema_lib.py","id_index.py","okf_migrate.py"}; assert expected <= set(z.namelist())'

wheel: package-check  ## build the distributable engine wheel under artifacts/wheel

prompt-lint:  ## semantic prompt contradictions, references, receipts, and size inventory
	mkdir -p artifacts
	"$(TEST_PYTHON)" scripts/prompt_lint.py --artifact artifacts/prompt-metrics.json

scrub:  ## domain-leak gate — 0=clean, 1=leak (conventional exit codes for CI/hooks; okengine#204)
	bash scripts/scrub-check.sh

preflight:  ## verify the canonical release-test environment (deps/tools present; okengine#204)
	PREFLIGHT_PYTHON="$(TEST_PYTHON)" bash scripts/preflight.sh

test-release:  ## full suite with the ALLOWED-SKIP policy enforced — a missing-dep skip FAILS (okengine#204)
	PREFLIGHT_PYTHON="$(TEST_PYTHON)" bash scripts/preflight.sh
	$(MAKE) test-policy TEST_PYTHON="$(TEST_PYTHON)"
	OKENGINE_REQUIRE_FULL_DEPS=1 $(TEST_TIMEOUT) $(TEST_FULL_DEADLINE) "$(TEST_PYTHON)" scripts/check-test-skips.py

release-evidence:  ## validate EVIDENCE=... and optional TAG=... against the release-audit policy
	@test -n "$(EVIDENCE)" || { echo "usage: make release-evidence EVIDENCE=scripts/audit/evidence/vX.Y.Z.json [TAG=vX.Y.Z]"; exit 2; }
	python scripts/audit/release_evidence.py validate "$(EVIDENCE)" $(if $(TAG),--tag "$(TAG)")

scaffold-check:  ## scaffold a pack and validate it end-to-end
	rm -rf /tmp/okengine-scaffold-check
	python scripts/framework_init.py /tmp/okengine-scaffold-check --domain "CI Check"
	python scripts/framework_validate.py /tmp/okengine-scaffold-check --quiet
	cd /tmp/okengine-scaffold-check && python validate.py
	rm -rf /tmp/okengine-scaffold-check

audit:  ## supply-chain CVE scan + python security lint (okengine#54/#280)
	# Single source of truth — the same gate the CI security-audit job runs. pip-audit over every
	# requirements file + bandit (medium+, zero-baseline) over scripts/okengine-mcp/tools. The
	# reader/cockpit web code is deliberately NOT bandit-gated here (subprocess/template noise);
	# their deps are still CVE-scanned, and public CI keeps an informational full-scope bandit.
	bash scripts/audit.sh

coverage: editable-install  ## run tests with a coverage report (okengine#56)
	# Child-process tests can run from temporary working directories. Pin the absolute
	# config path so every coverage fragment records compatible branch data. Match CI's
	# full-dependency and import-mode contract; an incomplete environment fails before pytest.
	PREFLIGHT_PYTHON="$(TEST_PYTHON)" bash scripts/preflight.sh
	"$(TEST_PYTHON)" -c 'import pytest_cov' || { \
		echo "coverage: pytest-cov is required in $(TEST_PYTHON); run make dev" >&2; exit 1; }
	$(MAKE) test-policy TEST_PYTHON="$(TEST_PYTHON)"
	OKENGINE_REQUIRE_FULL_DEPS=1 COVERAGE_RCFILE="$(CURDIR)/pyproject.toml" \
		$(TEST_TIMEOUT) $(TEST_FULL_DEADLINE) "$(TEST_PYTHON)" -m pytest tests/ -q --import-mode=importlib --cov \
		--cov-config="$(CURDIR)/pyproject.toml" --cov-report=term-missing \
		--cov-report=json:coverage.json --cov-fail-under=100
	"$(TEST_PYTHON)" scripts/check_branch_coverage.py coverage.json --config pyproject.toml \
		--line-min 100 --per-file

typecheck:  ## blocking typed-subsystem ratchet, including operations + projection
	mkdir -p artifacts
	$(TEST_TIMEOUT) $(TEST_FAST_DEADLINE) "$(TEST_PYTHON)" -m mypy \
		--junit-xml artifacts/junit-typecheck.xml

diff-check:  ## reject whitespace errors in DIFF_BASE...HEAD, or the local index/worktree
	mkdir -p artifacts
	@if [ -n "$(DIFF_BASE)" ]; then \
		$(TEST_TIMEOUT) $(TEST_FAST_DEADLINE) git diff --check "$(DIFF_BASE)...HEAD"; \
	else \
		$(TEST_TIMEOUT) $(TEST_FAST_DEADLINE) git diff --check && \
		$(TEST_TIMEOUT) $(TEST_FAST_DEADLINE) git diff --cached --check; \
	fi >artifacts/diff-check.txt 2>&1
	@git rev-parse HEAD >artifacts/diff-check-sha.txt

review-context:  ## canonical branch/SHA/release/dirty/origin-main evidence header
	mkdir -p artifacts
	"$(TEST_PYTHON)" scripts/review_context.py --strict --artifact artifacts/review-context.json

docker-smoke:  ## build the reader + mcp images (no run) — catches Dockerfile/dep breakage (okengine#55)
	docker build -f okengine-reader/Dockerfile -t okengine-reader:smoke .
	# mcp Dockerfile COPYs the shared scripts/cron/kb_* wrappers → build context is the repo ROOT
	# (matches `docker compose build okengine-mcp`, which sets context: .). Building with the
	# okengine-mcp/ subdir as context fails: those COPYs resolve outside it.
	docker build -f okengine-mcp/Dockerfile -t okengine-mcp:smoke .

smoke-e2e:  ## render-surface e2e: seed a vault, run reader/cockpit/mcp, assert on rendered output (playwright)
	# Stands up the barebones read stack over a frozen seeded vault and asserts on the ACTUAL
	# rendered HTML/PDF + rendered DOM — the render/integration regressions unit fixtures miss.
	# Point SMOKE_PYTHON at a venv with pytest (+ playwright & system Chrome for the DOM layer).
	bash tests/e2e/smoke/smoke-e2e.sh --e2e

smoke-release:  ## blocking minimal startup/auth/critical-path smoke over disposable services
	SMOKE_RELEASE=1 SMOKE_PYTHON="$(TEST_PYTHON)" \
		bash tests/e2e/smoke/smoke-e2e.sh --smoke

e2e-release:  ## blocking production-like HTTP/DOM/write-read workflow over disposable services
	SMOKE_RELEASE=1 SMOKE_PYTHON="$(TEST_PYTHON)" \
		bash tests/e2e/smoke/smoke-e2e.sh --e2e

resilience-release:  ## blocking running-stack fault injection with recovery/telemetry evidence
	SMOKE_RELEASE=1 SMOKE_PYTHON="$(TEST_PYTHON)" \
		bash tests/e2e/smoke/smoke-e2e.sh --resilience

performance-release:  ## blocking 100-user/1000-rpm latency, memory, and critical-path gate
	SMOKE_RELEASE=1 SMOKE_PYTHON="$(TEST_PYTHON)" \
		bash tests/e2e/smoke/smoke-e2e.sh --performance

sandbox-start:  ## start the verified local sample vault (reader :9880, cockpit :9881, MCP :8880)
	# Reuse the render-smoke stack as the contributor sandbox: it builds the images, verifies every
	# surface against seeded content, then leaves the loopback-only stack running for exploration.
	bash tests/e2e/smoke/smoke-e2e.sh --keep

sandbox-stop:  ## stop the local sample-vault sandbox and remove its disposable index volume
	docker compose -f tests/e2e/smoke/docker-compose.smoke.yml down -v --remove-orphans

sandbox-reset:  ## reset disposable sandbox state, rebuild/verify it, and leave it running
	$(MAKE) sandbox-stop
	$(MAKE) sandbox-start

render-lint:  ## sweep a LIVE deployment's whole vault through the reader and flag rendered-output defects
	# The real-data companion to smoke-e2e: crawls every page via the reader and flags leaked
	# builder markup / literal wikilinks / broken embeds in the rendered output — the class that
	# reaches users on stored content that clean fixtures pass. Point READER_URL at the deployment.
	python scripts/cron/render_lint.py --reader-url $${READER_URL:-http://127.0.0.1:9400}

content-lint:  ## scan a vault's SOURCE for degenerate generations (word-salad, code-switching bleed)
	# The content-quality layer render-lint can't see: a page full of repetition-loop filler or
	# latin-fused CJK renders a clean 200. Reads the markdown directly (fast). Point VAULT at the
	# deployment root (contains wiki/), or set WIKI.
	python scripts/cron/content_lint.py --vault $${VAULT:-.} $${WIKI:+--wiki $$WIKI}

publish-snapshot:  ## stage a gated, no-history public GitHub snapshot (never pushes; okengine#94)
	@if [ -f scripts/publish-snapshot.sh ]; then \
	  bash scripts/publish-snapshot.sh; \
	else \
	  echo "publish-snapshot is a SOURCE-REPO-only target — its script is excluded from public snapshots (invariant-audit #59). Run it from the source repo."; \
	fi

check: scrub lint test scaffold-check  ## everything CI runs (fast gate; audit/coverage/typecheck are separate)
