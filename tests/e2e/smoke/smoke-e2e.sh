#!/usr/bin/env bash
# Mandatory release smoke/E2E harness. It owns a disposable vault, bounded production-like
# containers, diagnostic evidence, and teardown verification. No release mode permits a skip.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
PY="${SMOKE_PYTHON:-python3}"
MODE=e2e
KEEP=0
BUILD=1
for arg in "$@"; do
  case "$arg" in
    --smoke) MODE=smoke ;;
    --e2e) MODE=e2e ;;
    --resilience) MODE=resilience ;;
    --performance) MODE=performance ;;
    --keep) KEEP=1 ;;
    --no-build) BUILD=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

RELEASE="${SMOKE_RELEASE:-0}"
HOSTADDR=127.0.0.1
COMPOSE=(docker compose -f "$HERE/docker-compose.smoke.yml")
if [ "${SMOKE_CI:-0}" = 1 ]; then
  COMPOSE+=(-f "$HERE/docker-compose.smoke.ci.yml")
  HOSTADDR="${SMOKE_HOST:-docker}"
fi

ARTIFACT_DIR="${SMOKE_ARTIFACT_DIR:-$ROOT/artifacts/$MODE-release}"
mkdir -p "$ARTIFACT_DIR"
ARTIFACT_DIR="$(cd "$ARTIFACT_DIR" && pwd)"
# Evidence must describe this invocation only.  A retained local artifact directory otherwise
# appends a new fault timeline beside stale logs/JUnit from an older run, making a green release
# bundle non-auditable.  Remove only the harness-owned filenames, never the directory itself.
rm -f -- \
  "$ARTIFACT_DIR/compose-images.jsonl" "$ARTIFACT_DIR/compose-ps.txt" \
  "$ARTIFACT_DIR/docker-version.txt" "$ARTIFACT_DIR/fault-timeline.jsonl" \
  "$ARTIFACT_DIR/fault-receipts.jsonl" "$ARTIFACT_DIR/health-state.json" \
  "$ARTIFACT_DIR/performance-raw.json" "$ARTIFACT_DIR/performance-summary.json" \
  "$ARTIFACT_DIR/performance-environment.json" "$ARTIFACT_DIR/performance-junit.xml" \
  "$ARTIFACT_DIR/git-sha.txt" "$ARTIFACT_DIR/resilience-junit.xml" \
  "$ARTIFACT_DIR/service-logs.txt" "$ARTIFACT_DIR/teardown.txt"
# Docker-in-Docker resolves bind-mount sources in the daemon service container, not in this job
# container. GitLab shares CI_PROJECT_DIR with both containers, while their /tmp filesystems are
# distinct. Keep the disposable vault inside that shared directory in CI so the production-like
# services receive the seeded files instead of an empty daemon-side directory.
if [ "${SMOKE_CI:-0}" = 1 ]; then
  VAULT_TMP="$(mktemp -d "${CI_PROJECT_DIR:-$ROOT}/.okengine-$MODE-vault.XXXXXX")"
else
  VAULT_TMP="$(mktemp -d "${TMPDIR:-/tmp}/okengine-$MODE-vault.XXXXXX")"
fi
cp -a "$HERE/vault/." "$VAULT_TMP/"
# mktemp deliberately creates mode 0700. The release images run as non-root users, so a bind
# mount of that directory is present but untraversable inside the containers and every seeded read
# returns 404. Expose traversal on the disposable root; fixture files retain their own modes and
# the bind mount still controls whether each service may write.
chmod a+rx "$VAULT_TMP"
export SMOKE_VAULT="$VAULT_TMP"
export SMOKE_QMD="$VAULT_TMP/.qmd"
mkdir -p "$SMOKE_QMD"
export SMOKE_READER_URL="http://$HOSTADDR:9880"
export SMOKE_COCKPIT_URL="http://$HOSTADDR:9881"
export SMOKE_MCP_URL="http://$HOSTADDR:8880"
export SMOKE_REVIEW_URL="http://$HOSTADDR:8881"
export SMOKE_GATEWAY_URL="http://$HOSTADDR:8882"
export SMOKE_MCP_TOKEN=okengine-local
export SMOKE_REVIEW_TOKEN=okengine-smoke-review-secret
export SMOKE_RELEASE="$RELEASE"
export SMOKE_REQUIRE_DOM="$RELEASE"
export SMOKE_ARTIFACT_DIR="$ARTIFACT_DIR"
if [ "$(id -u)" = 0 ]; then
  export SMOKE_UID=10001 SMOKE_GID=10001
  chown -R "$SMOKE_UID:$SMOKE_GID" "$VAULT_TMP"
else
  SMOKE_UID="$(id -u)"; SMOKE_GID="$(id -g)"; export SMOKE_UID SMOKE_GID
fi

capture_evidence() {
  git -C "$ROOT" rev-parse HEAD >"$ARTIFACT_DIR/git-sha.txt" 2>/dev/null || true
  "${COMPOSE[@]}" ps --all >"$ARTIFACT_DIR/compose-ps.txt" 2>&1 || true
  "${COMPOSE[@]}" logs --no-color >"$ARTIFACT_DIR/service-logs.txt" 2>&1 || true
  "${COMPOSE[@]}" images --format json >"$ARTIFACT_DIR/compose-images.jsonl" 2>&1 || true
  docker version >"$ARTIFACT_DIR/docker-version.txt" 2>&1 || true
}

finish() {
  rc=$?
  trap - EXIT
  set +e
  capture_evidence
  if [ "$KEEP" = 1 ]; then
    printf 'stack retained; disposable vault: %s\n' "$VAULT_TMP" >"$ARTIFACT_DIR/teardown.txt"
  else
    "${COMPOSE[@]}" down -v --remove-orphans >>"$ARTIFACT_DIR/teardown.txt" 2>&1
    down_rc=$?
    leftovers="$("${COMPOSE[@]}" ps -q 2>/dev/null)"
    if [ "$down_rc" -ne 0 ] || [ -n "$leftovers" ]; then
      printf 'ERROR: teardown incomplete (down_rc=%s leftovers=%s)\n' "$down_rc" "$leftovers" \
        >>"$ARTIFACT_DIR/teardown.txt"
      rc=1
    else
      echo "teardown verified: zero project containers" >>"$ARTIFACT_DIR/teardown.txt"
      rm -rf -- "$VAULT_TMP"
    fi
  fi
  exit "$rc"
}
trap finish EXIT

if [ "$MODE" = e2e ] && [ "$RELEASE" = 1 ]; then
  "$PY" -c 'import playwright.sync_api' >/dev/null 2>&1 || {
    echo "ERROR: release E2E requires the Playwright Python package" >&2
    exit 3
  }
  if [ -z "${SMOKE_BROWSER_EXECUTABLE:-}" ] && ! command -v google-chrome >/dev/null 2>&1; then
    echo "ERROR: release E2E requires SMOKE_BROWSER_EXECUTABLE or system Google Chrome" >&2
    exit 3
  fi
fi

services=(reader okengine-mcp cockpit)
if [ "$MODE" = e2e ]; then
  services+=(review-write)
elif [ "$MODE" = resilience ] || [ "$MODE" = performance ]; then
  services+=(review-write responses-gateway)
fi
echo "==> $MODE stack build/up (release=$RELEASE)"
if [ "$BUILD" = 1 ]; then
  timeout 1200 "${COMPOSE[@]}" build "${services[@]}"
fi
timeout 180 "${COMPOSE[@]}" up -d "${services[@]}"

wait_http() {
  name=$1; url=$2; expected=$3
  deadline=$((SECONDS + 90))
  while [ "$SECONDS" -lt "$deadline" ]; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)"
    if [ "$expected" = any ]; then
      [ -n "$code" ] && [ "$code" != 000 ] && return 0
    elif [ "$code" = "$expected" ]; then
      return 0
    fi
    sleep 1
  done
  echo "ERROR: $name did not become ready at $url (last HTTP $code)" >&2
  return 1
}

# A process-level health endpoint can turn green before the reader's initial vault scan has
# populated its page index.  Release tests exercise the indexed read path immediately, so readiness
# must include the seeded page they depend on; otherwise a cold runner races healthz and reports a
# spurious 404 (#537).
wait_body_contains() {
  name=$1; url=$2; needle=$3
  deadline=$((SECONDS + 90))
  body=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    body="$(curl -fsS --max-time 5 "$url" 2>/dev/null || true)"
    case "$body" in
      *"$needle"*) return 0 ;;
    esac
    sleep 1
  done
  echo "ERROR: $name did not expose its seeded read path at $url" >&2
  return 1
}

wait_http reader "$SMOKE_READER_URL/healthz" 200
wait_body_contains reader-index \
  "$SMOKE_READER_URL/api/page?path=entities/a/apt-smoke" SMOKE_BODY_SENTINEL
wait_http cockpit "$SMOKE_COCKPIT_URL/api/dashboards" 200
wait_http mcp "$SMOKE_MCP_URL/mcp" any
if [ "$MODE" = e2e ] || [ "$MODE" = resilience ]; then
  wait_http review-write "$SMOKE_REVIEW_URL/healthz" 200
fi
if [ "$MODE" = resilience ] || [ "$MODE" = performance ]; then
  wait_http responses-gateway "$SMOKE_GATEWAY_URL/healthz" 200
fi

# Browser/PDF integration may legitimately exceed the unit suite's generic 30s watchdog on a cold
# runner. Keep a hard per-test ceiling, plus the tighter command-level ceilings below.
common=(--strict-layer-skips --timeout=90 -q -p no:warnings -rs)
if [ "$MODE" = smoke ]; then
  timeout 180 "$PY" -m pytest "$HERE/test_startup_smoke.py" "${common[@]}" \
    --junitxml="$ARTIFACT_DIR/smoke-junit.xml"
elif [ "$MODE" = e2e ]; then
  set +e
  timeout 600 "$PY" -m pytest "$HERE/test_smoke_curl.py" "${common[@]}" \
    --junitxml="$ARTIFACT_DIR/e2e-http-junit.xml"
  http_rc=$?
  timeout 300 "$PY" -m pytest "$HERE/test_smoke_render.py" "${common[@]}" \
    --junitxml="$ARTIFACT_DIR/e2e-dom-junit.xml"
  dom_rc=$?
  set -e
  dom_fatal=$dom_rc
  if [ "$dom_rc" = 5 ] && [ "$RELEASE" = 0 ]; then
    dom_fatal=0
  fi
  echo "==> E2E layers: http_rc=$http_rc dom_rc=$dom_rc release=$RELEASE"
  if [ "$http_rc" -ne 0 ] || [ "$dom_fatal" -ne 0 ]; then
    echo "ERROR: one or more mandatory E2E layers failed" >&2
    exit 1
  fi
elif [ "$MODE" = resilience ]; then
  timeout 600 "$PY" -m pytest "$ROOT/tests/resilience/test_release_fault_matrix.py" \
    "${common[@]}" --junitxml="$ARTIFACT_DIR/resilience-junit.xml"
else
  timeout 900 "$PY" -m pytest "$ROOT/tests/performance/test_release_performance.py" \
    "${common[@]}" --timeout=180 --junitxml="$ARTIFACT_DIR/performance-junit.xml"
fi

echo "==> $MODE passed; evidence: $ARTIFACT_DIR"
