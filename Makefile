# OKEngine dev tasks. See CONTRIBUTING.md. Run `make help` for the list.
.DEFAULT_GOAL := help
.PHONY: help dev test lint scaffold-check check audit coverage typecheck docker-smoke publish-snapshot

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

dev:  ## install dev/test dependencies
	python -m pip install -r requirements-dev.txt

test:  ## run the test suite (mcp-dependent tests self-skip if `mcp` is absent)
	python -m pytest

lint:  ## syntax + real-bug lint (no style enforcement)
	python -m ruff check --select E9,F63,F7,F82 scripts tools okengine-mcp okengine-reader tests

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
	docker build -t okengine-mcp:smoke okengine-mcp

publish-snapshot:  ## stage a gated, no-history public GitHub snapshot (never pushes; okengine#94)
	bash scripts/publish-snapshot.sh

check: lint test scaffold-check  ## everything CI runs (fast gate; audit/coverage/typecheck are separate)
