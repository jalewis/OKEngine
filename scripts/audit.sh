#!/usr/bin/env bash
# okengine security audit (okengine#280) — Python SAST (bandit) + dependency CVE scan (pip-audit),
# mirroring the okpacks-library security-audit gate so the ENGINE gets the same coverage its packs do.
# Every medium-or-higher Bandit finding is a hard failure. There is deliberately
# no accepted-findings baseline: a new finding must be fixed or justified at the
# exact line with a narrow, reviewed ``# nosec B...`` annotation.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
fail=0

echo "==> pip-audit (dependency CVE scan)"
if command -v pip-audit >/dev/null 2>&1; then
  for r in okengine-mcp/requirements.txt okengine-cockpit/requirements.txt \
           okengine-reader/requirements.txt requirements-dev.txt requirements-extract.txt; do
    [ -f "$r" ] || continue
    echo "-- $r --"
    pip-audit -r "$r" || fail=1
  done
else
  echo "ERROR: pip-audit not installed — pip install pip-audit"; fail=1
fi
echo

echo "==> bandit (Python SAST, medium+; zero accepted findings)"
if command -v bandit >/dev/null 2>&1; then
  bandit -ll -q -r scripts okengine-mcp tools -x '*/__pycache__/*' || fail=1
else
  echo "ERROR: bandit not installed — pip install bandit"; fail=1
fi
echo

if [ "$fail" -ne 0 ]; then
  echo "audit: FAILURES above"; exit 1
fi
echo "audit: clean (zero medium+ SAST findings; no dependency CVEs)"
