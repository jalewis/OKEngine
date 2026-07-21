#!/usr/bin/env bash
# Render-surface smoke: stand up the barebones reader/cockpit/mcp stack over the frozen seeded
# vault, assert on the ACTUAL rendered output (HTTP/content + rendered-DOM), then tear down.
#
# This catches the class of regression the unit suite structurally can't — render/integration bugs
# on a populated vault (leaked wikilink markup, fact-panel-above-body, a nested dashboard vanishing,
# a deck 404, a mis-resolved embed) — each of which returns a green 200 from a liveness probe.
#
# Usage:
#   bash tests/e2e/smoke/smoke-e2e.sh            # build, up, assert, teardown
#   SMOKE_PYTHON=/path/to/venv/bin/python bash tests/e2e/smoke/smoke-e2e.sh
#   bash tests/e2e/smoke/smoke-e2e.sh --keep     # leave the stack up for manual poking
#   bash tests/e2e/smoke/smoke-e2e.sh --no-build # reuse existing images
#
# The venv needs: pytest, and (for the rendered-DOM layer) playwright + a system Chrome
# (channel="chrome").
#
# DEV mode (default): without playwright the DOM tests SKIP and the HTTP layer still gates.
# RELEASE mode (SMOKE_REQUIRE_DOM=1): a missing playwright/Chrome is a hard FAILURE — the DOM layer
#   MUST run, so a release can't be green with the rendered-DOM assertions silently omitted
#   (issue okengine#204, gap 1). The two layers run and report their pass/skip counts separately.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="docker compose -f $HERE/docker-compose.smoke.yml"
PY="${SMOKE_PYTHON:-python3}"
REQUIRE_DOM="${SMOKE_REQUIRE_DOM:-0}"

# CI/dind mode (SMOKE_CI=1, used by the okengine#282 e2e job): the stack runs inside the dind daemon, so
# its published ports live on the dind container's interfaces — reachable from this job at the `docker`
# service hostname, not 127.0.0.1, and they must bind 0.0.0.0 there. Layer the CI compose overlay and
# retarget the probe/test URLs at the dind host. Local runs (SMOKE_CI unset) keep the loopback binding.
HOSTADDR=127.0.0.1
if [ "${SMOKE_CI:-0}" = 1 ]; then
  COMPOSE="$COMPOSE -f $HERE/docker-compose.smoke.ci.yml"
  HOSTADDR="${SMOKE_HOST:-docker}"
fi

# Release-mode preflight: fail BEFORE building the stack if the DOM layer can't run at all.
if [ "$REQUIRE_DOM" = 1 ]; then
  "$PY" -c 'import playwright.sync_api' 2>/dev/null || {
    echo "ERROR: SMOKE_REQUIRE_DOM=1 but 'playwright' is not importable in $PY — the rendered-DOM" >&2
    echo "       layer would silently skip. Install it (pip install playwright) + a system Chrome," >&2
    echo "       or unset SMOKE_REQUIRE_DOM for the dev (HTTP-only) gate." >&2
    exit 3
  }
fi
export SMOKE_READER_URL="http://$HOSTADDR:9880"
export SMOKE_COCKPIT_URL="http://$HOSTADDR:9881"
export SMOKE_MCP_URL="http://$HOSTADDR:8880"
export SMOKE_MCP_TOKEN="okengine-local"          # must match OKENGINE_MCP_TOKEN in docker-compose.smoke.yml

KEEP=0; BUILD=1
for a in "$@"; do
  case "$a" in
    --keep) KEEP=1 ;;
    --no-build) BUILD=0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

teardown() { [ "$KEEP" = 1 ] && { echo "==> --keep: stack left up ($SMOKE_READER_URL / $SMOKE_COCKPIT_URL)"; return; }
             echo "==> teardown"; $COMPOSE down -v >/dev/null 2>&1 || true; }
trap teardown EXIT

echo "==> smoke stack build/up"
[ "$BUILD" = 1 ] && $COMPOSE build >/dev/null
$COMPOSE up -d

echo "==> waiting for reader + cockpit health"
for svc in "$SMOKE_READER_URL/healthz" "$SMOKE_COCKPIT_URL/api/dashboards"; do
  ok=0
  for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m5 "$svc" || true)
    [ "$code" = "200" ] && { ok=1; break; }
    sleep 1
  done
  [ "$ok" = 1 ] || { echo "ERROR: $svc never became healthy" >&2; $COMPOSE logs --tail=40; exit 1; }
  echo "   ok: $svc"
done

# The MCP is auth-gated: /mcp without a token returns 401 (not 200), so it needs its own wait that
# treats "the server answered at all" (code != 000) as up. A 000 = connection refused = the container
# never bound or EXITED (e.g. the okengine#50 default-token fail-closed guard) — surface that clearly.
echo "==> waiting for mcp health (401 without a token = up + auth enforced)"
mok=0
for _ in $(seq 1 30); do
  mcode=$(curl -s -o /dev/null -w '%{http_code}' -m5 "$SMOKE_MCP_URL/mcp" || true)
  [ -n "$mcode" ] && [ "$mcode" != "000" ] && { mok=1; break; }
  sleep 1
done
[ "$mok" = 1 ] || { echo "ERROR: mcp at $SMOKE_MCP_URL never answered (container may have EXITED — check the fail-closed token guard)" >&2; $COMPOSE logs --tail=40 okengine-mcp; exit 1; }
echo "   ok: $SMOKE_MCP_URL/mcp (code $mcode)"

# Run the two layers SEPARATELY so each reports its own pass/skip count (issue okengine#204, gap 1):
#   HTTP/content layer (test_smoke_curl.py) — always gates.
#   rendered-DOM layer (test_smoke_render.py) — gates; in release mode a skip becomes a failure.
set +e
echo "==> [1/2] HTTP/content layer"
"$PY" -m pytest "$HERE/test_smoke_curl.py" -q -p no:warnings -rs
http_rc=$?
echo "==> [2/2] rendered-DOM layer (SMOKE_REQUIRE_DOM=$REQUIRE_DOM)"
SMOKE_REQUIRE_DOM="$REQUIRE_DOM" "$PY" -m pytest "$HERE/test_smoke_render.py" -q -p no:warnings -rs
dom_rc=$?
set -e

echo "==> smoke: http_rc=$http_rc dom_rc=$dom_rc (require_dom=$REQUIRE_DOM)"
# dom_rc: 0 = ran+passed, 1 = a DOM test FAILED, 5 = pytest collected NOTHING (the module
# importorskip'd — playwright absent). In DEV mode (REQUIRE_DOM=0) a skipped DOM layer is the
# documented behavior ("without playwright the DOM tests SKIP and the HTTP layer still gates"), so
# exit 5 there is OK, not a failure. In RELEASE mode the preflight above already hard-failed before
# build if playwright was missing, so we never reach here with 5. Anything else non-zero is fatal.
dom_fatal=1
[ "$dom_rc" = 0 ] && dom_fatal=0
{ [ "$dom_rc" = 5 ] && [ "$REQUIRE_DOM" = 0 ]; } && dom_fatal=0
if [ "$http_rc" != 0 ] || [ "$dom_fatal" != 0 ]; then
  echo "ERROR: a smoke layer failed (http_rc=$http_rc dom_rc=$dom_rc)." >&2
  exit 1
fi
[ "$dom_rc" = 5 ] && echo "   (dev mode: rendered-DOM layer skipped — playwright not installed)"
exit 0
