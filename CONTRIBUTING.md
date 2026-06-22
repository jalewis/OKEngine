# Contributing to OKEngine

Thanks for your interest. OKEngine is the **domain-agnostic engine for swappable-topic LLM
wikis** — it turns a pinned Hermes-Agent into an agent that builds and maintains a topic wiki
(security is the first domain profile). Pages are portable via OKF, a small interoperability
floor. This guide covers the dev setup and the conventions that keep the engine clean and
reusable.

## Dev setup

```sh
python -m venv .venv && . .venv/bin/activate
make dev          # pip install -r requirements-dev.txt (pytest, pyyaml, ruff, + the check tools)
make test         # python -m pytest
make lint         # syntax + real-bug lint (no style enforcement)
make check        # lint + test + scaffold-check (the fast gate CI runs)
```

The test suite runs offline. The tests that exercise the MCP servers **self-skip**
when the `mcp` package isn't installed; for the full suite also
`pip install -r okengine-mcp/requirements.txt`.

Deeper quality checks (separate from the fast `make check` gate; each also a CI job):

```sh
make audit        # pip-audit (CVEs) + bandit (python security lint)        — okengine#54
make coverage     # pytest --cov, term report                              — okengine#56
make typecheck    # mypy over the core tools + MCP write path (baseline)   — okengine#57
make docker-smoke # build the reader + mcp images                          — okengine#55
```

Targets: `make help`. Single command to run everything the fast gate runs: `make check`.

## The one rule that matters most: keep the engine domain-agnostic

OKEngine ships **no domain knowledge** — no vendor/product names, no taxonomy, no
deployment-specific paths or hosts. Anything domain-specific is a **pack** input
(a `schema.yaml` field, an env var, a config file), never a literal in engine code.
See [`docs/engine-domain-boundary.md`](docs/engine-domain-boundary.md) for the
engine ⇄ pack split and [`engine-manifest.yaml`](engine-manifest.yaml) for the
enumerated boundary.

Before opening a PR that touches engine code, sanity-check nothing domain-specific
leaked in (the same check CI-adjacent reviewers run):

```sh
grep -rinE "<vendor/product names>|<private hostnames>|192\.168\.|10\.0\." \
  --include="*.py" --include="*.md" --include="*.yaml" --include="*.json" --include="*.sh" .
```

## Conventions

- **Every fix gets a regression test** under `tests/` (except pure infra/config).
- **The MCP write path is the enforced contract.** Agent writes go through
  `okengine-mcp/write_server.py`, which validates against the pack's `schema.yaml`.
  Don't add a bypass.
- **Generated artifacts are not hand-edited** (e.g. `config/cron-plus-jobs.json` is
  produced by `scripts/cron_pack_split.py`). Edit the source, then regenerate.
- **Hermes is a pinned dependency, not a fork.** Engine changes are additions
  (overlay) or carried patches under `patches/`, never an in-place Hermes edit.
- Keep new code in the style of the surrounding code; the lint gate enforces only
  real bugs (syntax, undefined names), not style.

## Pull requests

1. Branch from `main`.
2. `make check` passes locally.
3. Describe what changed and why; link any issue.
4. CI (lint + tests on 3.11–3.13 + scaffold validation) must be green.

## Security

Please report vulnerabilities privately — see [`SECURITY.md`](SECURITY.md). Do not
open a public issue for a security problem.
