#!/usr/bin/env bash
# Release-test PREFLIGHT (okengine#204): verify the ONE canonical environment the release gates need,
# so "confirm PASSED, not skipped" is actionable and gates don't silently skip. Reports every
# dependency; exits non-zero if a REQUIRED one (the offline unit-suite deps) is missing — those cause
# the false-green dependency-SKIPs the release must not tolerate. Optional tools (docker, gitleaks,
# playwright browser) are reported as WARN, since some gates (docker-smoke, full-history secret scan,
# the smoke DOM layer) only run when they're present.
#
#   make preflight                       # check with the current python
#   PREFLIGHT_PYTHON=/path/venv/bin/python make preflight
#
# Canonical setup for a full release env:
#   python -m venv .venv && . .venv/bin/activate
#   pip install -e '.[dev]'                      # or: make dev
#   pip install -r okengine-mcp/requirements.txt # MCP-dependent tests
#   pip install fastapi markdown nh3 httpx pyyaml croniter python-docx python-pptx openpyxl striprtf
#   pip install playwright && playwright install chromium    # smoke DOM layer
#   # + docker, gitleaks, Claude Code with Workflow support, ripgrep on PATH
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
PY="${PREFLIGHT_PYTHON:-${SMOKE_PYTHON:-python3}}"
miss=0; warn=0

echo "preflight: python = $("$PY" -V 2>&1) ($PY)"

# REQUIRED — the offline unit suite imports these; a missing one becomes a silent dependency SKIP.
req_mods=(pytest yaml fastapi markdown nh3 httpx croniter mcp docx pptx openpyxl striprtf)
echo "== required (offline unit suite) =="
for m in "${req_mods[@]}"; do
  if "$PY" -c "import $m" 2>/dev/null; then printf '  ✓ %s\n' "$m"
  else printf '  ✗ %s  (MISSING — tests importorskip it -> false-green skip)\n' "$m"; miss=$((miss+1)); fi
done

# OPTIONAL python — only some dev gates need these; report but don't fail the offline gate.
opt_mods=(ruff pytest_cov mypy pip_audit playwright weasyprint pydyf)
echo "== optional (extra dev gates: lint/coverage/typecheck/audit/smoke-DOM/pdf) =="
for m in "${opt_mods[@]}"; do
  if "$PY" -c "import ${m//-/_}" 2>/dev/null; then printf '  ✓ %s\n' "$m"
  else printf '  ⚠ %s  (absent — its gate will not run)\n' "$m"; warn=$((warn+1)); fi
done

# System tools.
echo "== system tools =="
for t in git rg docker gitleaks; do
  if command -v "$t" >/dev/null 2>&1; then printf '  ✓ %s  (%s)\n' "$t" "$(command -v "$t")"
  else printf '  ⚠ %s  (absent)\n' "$t"; warn=$((warn+1)); fi
done
# Agent-driven audit layers are application-hosted Workflows, NOT Node scripts. `node` being
# present proves nothing (running invariant-audit.mjs directly produces an expected syntax error).
if command -v claude >/dev/null 2>&1; then
  printf '  ✓ claude  (%s; confirm this build exposes Workflow(...))\n' \
    "$(claude --version 2>/dev/null || command -v claude)"
else
  echo "  ⚠ claude CLI absent (invariant-audit/re-verify require Claude Code with Workflow support)"
  warn=$((warn+1))
fi
# playwright system browser (the smoke DOM layer needs a system Chrome via channel="chrome")
if "$PY" -c 'import playwright' 2>/dev/null; then
  if "$PY" -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(channel="chrome"); b.close(); p.stop()' 2>/dev/null; then
    echo "  ✓ playwright system chrome (SMOKE_REQUIRE_DOM=1 will run)"
  else echo "  ⚠ playwright present but system chrome not launchable (SMOKE_REQUIRE_DOM=1 would FAIL)"; warn=$((warn+1)); fi
fi

echo "---"
if [ "$miss" -gt 0 ]; then
  echo "preflight: ✗ $miss REQUIRED dep(s) missing — the offline suite would SILENT-SKIP them. Install them (see header) before the release suite." >&2
  exit 1
fi
echo "preflight: ✓ required deps present ($warn optional/tool warning(s) — their gates just won't run)."
exit 0
