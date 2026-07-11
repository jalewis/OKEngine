# OKEngine dev tasks. See CONTRIBUTING.md. Run `make help` for the list.
.DEFAULT_GOAL := help
.PHONY: help dev test lint scrub preflight test-release scaffold-check check audit coverage typecheck docker-smoke smoke-e2e render-lint content-lint publish-snapshot

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

dev:  ## install dev/test dependencies
	python -m pip install -r requirements-dev.txt

test:  ## run the test suite (mcp-dependent tests self-skip if `mcp` is absent)
	python -m pytest

lint:  ## syntax + real-bug lint (no style enforcement)
	python -m ruff check --select E9,F63,F7,F82 scripts tools okengine-mcp okengine-reader tests

scrub:  ## domain-leak gate — 0=clean, 1=leak (conventional exit codes for CI/hooks; okengine#204)
	bash scripts/scrub-check.sh

preflight:  ## verify the canonical release-test environment (deps/tools present; okengine#204)
	bash scripts/preflight.sh

test-release:  ## full suite with the ALLOWED-SKIP policy enforced — a missing-dep skip FAILS (okengine#204)
	bash scripts/preflight.sh
	python scripts/check-test-skips.py

scaffold-check:  ## scaffold a pack and validate it end-to-end
	rm -rf /tmp/okengine-scaffold-check
	python scripts/framework_init.py /tmp/okengine-scaffold-check --domain "CI Check"
	python scripts/framework_validate.py /tmp/okengine-scaffold-check --quiet
	cd /tmp/okengine-scaffold-check && python validate.py
	rm -rf /tmp/okengine-scaffold-check

audit:  ## supply-chain CVE scan + python security lint (okengine#54)
	python -m pip_audit -r requirements-dev.txt
	python -m pip_audit -r okengine-mcp/requirements.txt
	python -m pip_audit -r okengine-reader/requirements.txt
	python -m bandit -q -ll -r scripts tools okengine-mcp okengine-reader -x tests

coverage:  ## run tests with a coverage report (okengine#56)
	python -m pytest --cov --cov-report=term-missing

typecheck:  ## limited static type check — core tools + MCP (okengine#57)
	python -m mypy

docker-smoke:  ## build the reader + mcp images (no run) — catches Dockerfile/dep breakage (okengine#55)
	docker build -t okengine-reader:smoke okengine-reader
	# mcp Dockerfile COPYs the shared scripts/cron/kb_* wrappers → build context is the repo ROOT
	# (matches `docker compose build okengine-mcp`, which sets context: .). Building with the
	# okengine-mcp/ subdir as context fails: those COPYs resolve outside it.
	docker build -f okengine-mcp/Dockerfile -t okengine-mcp:smoke .

smoke-e2e:  ## render-surface e2e: seed a vault, run reader/cockpit/mcp, assert on rendered output (playwright)
	# Stands up the barebones read stack over a frozen seeded vault and asserts on the ACTUAL
	# rendered HTML/PDF + rendered DOM — the render/integration regressions unit fixtures miss.
	# Point SMOKE_PYTHON at a venv with pytest (+ playwright & system Chrome for the DOM layer).
	bash tests/e2e/smoke/smoke-e2e.sh

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
	bash scripts/publish-snapshot.sh

check: scrub lint test scaffold-check  ## everything CI runs (fast gate; audit/coverage/typecheck are separate)
