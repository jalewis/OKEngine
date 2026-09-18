#!/usr/bin/env bash
# okengine#67 — post-deploy verifier.
#
# Compose validation (deploy.sh step 1) catches config syntax, not whether the deployed stack is
# actually USABLE. This runs live end-to-end checks after `deploy.sh` and prints operator
# remediation for anything that's down or misconfigured.
#
# Domain-agnostic: it discovers host ports via `docker compose port`, reads tokens from .env, and
# uses the engine's standard service names (override via env). Run it from the DEPLOYMENT dir
# (where docker-compose.yml lives), the same place you run deploy.sh:
#
#     bash ../okengine/scripts/post_deploy_verify.sh
#
# Exit 0 = every required check passed (WARNs allowed); exit 1 = one or more FAILs.
set -uo pipefail

# Engine scaffold service names; override for a nonstandard compose.
GW=${OKENGINE_GATEWAY_SVC:-gateway}
MCP=${OKENGINE_MCP_SVC:-okengine-mcp}
READER=${OKENGINE_READER_SVC:-okengine-reader}
COCKPIT=${OKENGINE_COCKPIT_SVC:-okengine-cockpit}
OPERATIONS=${OKENGINE_OPERATION_SVC:-okengine-operation-runner}
POSTGRES=${OKENGINE_POSTGRES_SVC:-postgres}
PROJECTION=${OKENGINE_PROJECTION_SVC:-okengine-projection}
# Keep the verifier's own source path out of the deployment environment's
# ENGINE_DIR namespace. Packs commonly pin ENGINE_DIR in .env; that file is
# sourced below and must not redirect verification to an older checkout.
VERIFY_ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pass=0; warn=0; fail=0
ok()  { printf "  \033[32mPASS\033[0m  %s\n" "$1"; pass=$((pass+1)); }
wn()  { printf "  \033[33mWARN\033[0m  %s\n        ↳ %s\n" "$1" "$2"; warn=$((warn+1)); }
bad() { printf "  \033[31mFAIL\033[0m  %s\n        ↳ %s\n" "$1" "$2"; fail=$((fail+1)); }
dcx() { docker compose exec -T "$@" 2>/dev/null; }

# Compose service list, resolved ONCE and reused. `docker compose config --services 2>/dev/null |
# grep -Fxq` conflates two different answers: compose replied and the service is not defined, and
# compose could not be asked at all. Observed live on 2026-08-17 — five verifiers run back to back,
# one `config` call failed under the contention, and the run reported "projection services are
# absent from effective Compose configuration" on a deployment whose projection was up and healthy,
# then passed three times in a row immediately after. A command that could not be asked must not
# answer for the thing it was asked about.
COMPOSE_SERVICES=$(docker compose config --services 2>/dev/null)
COMPOSE_SERVICES_RC=$?
has_service() {
    [ "$COMPOSE_SERVICES_RC" -eq 0 ] || return 2      # 2 = could not ask, distinct from absent
    printf '%s\n' "$COMPOSE_SERVICES" | grep -Fxq "$1"
}

if [ ! -f docker-compose.yml ] && [ ! -f compose.yml ]; then
    echo "no docker-compose.yml here — run from the deployment dir (where you ran deploy.sh)." >&2
    exit 2
fi
# token/password are read from .env if present (values, not just names)
[ -f .env ] && set -a && . ./.env 2>/dev/null && set +a
MCP_TOKEN=${OKENGINE_MCP_TOKEN:-}
READER_PW=${OKENGINE_READER_PASSWORD:-}

echo "OKEngine post-deploy verification"
echo "================================="

# 1. containers running ------------------------------------------------------
echo "[1] containers"
# The cockpit renders briefings/dashboards/predictions/decks — verify it too, but only when the
# compose actually defines it (minimal stacks legitimately omit it). A hardcoded 3-service list
# never checked the cockpit, so a cockpit that failed to start passed verification (invariant-audit
# B7.5).
svcs="$GW $MCP $READER"
has_service "$COCKPIT"; _cockpit=$?
if [ "$_cockpit" -eq 0 ]; then
    svcs="$svcs $COCKPIT"
elif [ "$_cockpit" -eq 2 ]; then
    wn "cannot read the effective Compose configuration — cockpit presence unknown" \
       "undetectable, not a pass: docker compose config --services failed; re-run, and check the daemon"
fi
for svc in $svcs; do
    state=$(docker compose ps --status running --services 2>/dev/null | grep -Fx "$svc")
    if [ -n "$state" ]; then ok "$svc is running"
    else bad "$svc is not running" "docker compose up -d $svc  (then: docker compose logs $svc)"; fi
done

# The engine-generated override is the upgrade path for existing packs: checking only the source
# file would let Compose omit/override it while deploy still reported healthy. Inspect the running
# gateway's effective Docker health command and require every #650 failure signal. This is config
# evidence, not current health state; the independent live probes below establish current behavior.
gw_health_cid="$(docker compose ps -q "$GW" 2>/dev/null | head -1)"
gw_health_test=""
[ -n "$gw_health_cid" ] && gw_health_test="$(docker inspect \
    --format '{{json .Config.Healthcheck.Test}}' "$gw_health_cid" 2>/dev/null || true)"
if [ -z "$gw_health_test" ] || [ "$gw_health_test" = "null" ]; then
    bad "gateway effective health contract is absent or unreadable" \
        "re-run deploy.sh so docker-compose.okengine-image.yml installs the engine-owned healthcheck"
else
    missing_health=""
    for signal in '.tick.lock' '.scheduler-stalled' 'lock-owner.json' '-mmin +60' '/proc/[0-9]*/stat' ') D '; do
        case "$gw_health_test" in *"$signal"*) ;; *) missing_health="$missing_health $signal" ;; esac
    done
    if [ -n "$missing_health" ]; then
        bad "gateway effective health contract is incomplete (missing:$missing_health)" \
            "re-run deploy.sh and force-recreate gateway so stale lock/run and D-state failures cannot read healthy"
    else
        ok "gateway effective health contract covers scheduler, corpus lock, stale runs, and D-state"
    fi
fi

# PostgreSQL projection is a standard service. Verify both the projector's scan-vs-row/digest
# health contract and the actual typed MCP consumer path; a running container alone is not proof
# that analysis tasks can query it or that reader credentials/freshness enforcement work.
echo "[1p] PostgreSQL projection"
has_service "$PROJECTION"; _projection=$?
if [ "$_projection" -eq 2 ]; then
    wn "cannot read the effective Compose configuration — projection checks skipped" \
       "undetectable, not a pass: docker compose config --services failed; re-run, and check the daemon"
elif [ "$_projection" -eq 0 ]; then
    for svc in "$POSTGRES" "$PROJECTION"; do
        state=$(docker compose ps --status running --services 2>/dev/null | grep -Fx "$svc")
        if [ -n "$state" ]; then ok "$svc is running"
        else bad "$svc is not running" "docker compose up -d --build $svc; docker compose logs $svc"; fi
    done
    # `if cmd; then` rather than assign-then-test-$?: the two are equivalent only while nothing
    # sits between them, and one inserted line silently inverts the verdict. This repo has been
    # bitten by the same shape twice — `deploy-cron-scripts.sh … | tail -3` returning tail's
    # status, and a `$?` read after a pipe reporting the wrong exit code (okengine#602).
    if projection_health=$(timeout 120 docker compose exec -T "$PROJECTION" \
            python /app/service.py --health 2>&1); then
        ok "projection health and file-count conformance passed: $projection_health"
    else bad "projection health/conformance failed" "$projection_health"; fi
    if typed_count=$(timeout 30 docker compose exec -T "$MCP" python -c \
            'import asyncio,json; from okengine.mcp import projection; print(json.dumps(asyncio.run(projection.count_pages())))' 2>&1); then
        ok "typed MCP count_pages query passed: $typed_count"
    else bad "typed MCP projection query failed" "$typed_count"; fi
else
    bad "projection services are absent from effective Compose configuration" \
        "re-run deploy.sh to materialize the standard projection overlay"
fi

# helper: host port + bind for a service's container port
hostport() {   # ip:port, or EMPTY when unpublished
    # docker compose v2 prints ":0" (not empty) for an unpublished port — treating
    # that as a binding made check [3] curl port 0 and FAIL "returned 000" on every
    # stack whose MCP is deliberately bridge-internal (the skeleton default).
    local o; o=$(docker compose port "$1" "$2" 2>/dev/null | tail -1)
    case "$o" in ""|*:0) return 0 ;; esac
    printf '%s\n' "$o"
}

# 2. reader ------------------------------------------------------------------
echo "[2] reader"
RB=$(hostport "$READER" 9200)
if [ -z "$RB" ]; then bad "reader port 9200 not published" "check the reader 'ports:' mapping in docker-compose.yml"
else
    RIP=${RB%:*}; RPORT=${RB##*:}; RURL="http://127.0.0.1:$RPORT"
    code=$(curl -s -o /dev/null -w "%{http_code}" -m8 "$RURL/healthz")
    if [ "$code" = "200" ]; then ok "reader /healthz 200 (on $RB)"
    else bad "reader /healthz returned $code" "docker compose logs $READER ; confirm it bound 0.0.0.0:9200 in-container"; fi
    # auth: with a password set, a protected endpoint must reject anonymous access
    if [ -n "$READER_PW" ]; then
        a=$(curl -s -o /dev/null -w "%{http_code}" -m8 "$RURL/api/about")
        if [ "$a" = "401" ]; then ok "reader auth enforced (anonymous /api/about -> 401)"
        else wn "reader password set but /api/about returned $a (expected 401)" "verify OKENGINE_READER_PASSWORD reached the reader container"; fi
    elif [ "$RIP" = "0.0.0.0" ] || [ "$RIP" = "::" ]; then
        wn "reader is published on $RIP with no OKENGINE_READER_PASSWORD" "set a password, or bind the port to 127.0.0.1, before exposing it"
    else ok "reader open but bound to $RIP (local only)"; fi
fi

# 2b. HARDENED posture (okengine#326 [1]) ------------------------------------
# deployment_validate.check_auth flags unsafe hardened settings, but only in the daily cron lane —
# which dies with the scheduler, so a fresh or mis-set hardened deployment gets no signal at deploy
# time. Reuse the SAME pure evaluator (hardening_lib.hardened_posture_violations) over the .env this
# script already sourced. Empty => hardened profile off, or on-and-safe.
echo "[2b] hardened posture"
hv=$(python3 - "$VERIFY_ENGINE_DIR" <<'PY' 2>/dev/null
import os, sys
sys.path[:0] = [os.path.join(sys.argv[1], "scripts", "cron"),
                os.path.join(sys.argv[1], "src")]
try:
    from hardening_lib import hardened_posture_violations, is_hardened
except Exception as exc:
    print("ERR\t%s" % exc); raise SystemExit(0)
if not is_hardened(os.environ):
    print("OFF")
else:
    viols = hardened_posture_violations(os.environ)
    print("OK" if not viols else "\n".join("V\t" + v for v in viols))
PY
)
if [ -z "$hv" ] || [ "$hv" = "OFF" ]; then
    ok "hardened profile off (OKENGINE_HARDENED unset) — posture checks N/A"
elif [ "$hv" = "OK" ]; then
    ok "OKENGINE_HARDENED on and posture is safe (token, reader auth, rate, exports, UI editing)"
else
    while IFS=$'\t' read -r tag msg; do
        case "$tag" in
            V)   bad "hardened posture: $msg" "fix the flagged .env setting and recreate the affected container" ;;
            ERR) wn  "could not evaluate hardened posture ($msg)" "run from the deployment dir with the engine checkout intact" ;;
        esac
    done <<< "$hv"
fi

# 3. MCP read server ---------------------------------------------------------
echo "[3] MCP read server"
MB=$(hostport "$MCP" 8730)
if [ -z "$MB" ]; then wn "MCP port 8730 not published" "the agent reaches it in-network; only publish it if a host client needs it"
else
    MPORT=${MB##*:}; MURL="http://127.0.0.1:$MPORT"
    code=$(curl -s -o /dev/null -w "%{http_code}" -m8 "$MURL/mcp")
    if [ "$code" = "401" ]; then ok "MCP /mcp 401 without token (auth enforced, on $MB)"
        if [ -n "$MCP_TOKEN" ]; then
            ac=$(curl -s -o /dev/null -w "%{http_code}" -m8 -H "Authorization: Bearer $MCP_TOKEN" "$MURL/mcp")
            [ "$ac" = "401" ] && wn "the configured token is rejected (still 401 with Bearer)" "OKENGINE_MCP_TOKEN in .env != the token the MCP container loaded" || ok "MCP accepts the configured token"
        fi
    elif [ "$code" = "200" ] || [ "$code" = "405" ]; then
        bad "MCP /mcp returned $code without a token — auth is OFF on a published port" "set OKENGINE_MCP_TOKEN and recreate $MCP; never expose the MCP unauthenticated"
    else bad "MCP /mcp returned $code" "docker compose logs $MCP"; fi
fi

# 3a. read-MCP baked-lib drift (okengine invariant-audit M-B4.1) -------------
# The read-MCP image BAKES kb_search/kb_graph/tier_lib into /app/scripts and SHELLS OUT to them for
# search/graph — so a change needs a read-MCP image REBUILD, exactly the baked-vs-STAGED trap the
# write-path libs have a check for. Compare the read-MCP's baked copy against the STAGED source of
# truth (/opt/data/scripts, refreshed by deploy-cron-scripts, read via the gateway which mounts it):
# a stage-only deploy makes the staged copy NEW while the read-MCP baked copy stays OLD -> stale
# search. Present on only one side is UNDETECTABLE, never a pass (the M22 one-sided-drift rule).
for lib in kb_search.py tier_lib.py schema_lib.py; do
  mh=$(dcx "$MCP" sh -c "sha256sum /app/scripts/$lib 2>/dev/null | cut -d' ' -f1")
  sh_=$(dcx "$GW" sh -c "sha256sum /opt/data/scripts/$lib 2>/dev/null | cut -d' ' -f1")
  if [ -n "$mh" ] && [ -n "$sh_" ]; then
    [ "$mh" != "$sh_" ] && bad "read-MCP $lib is STALE vs the staged source — served search/graph is out of date" \
        "rebuild the read-MCP image (docker compose build $MCP && up -d $MCP); a stage-only deploy misses it"
  elif [ -n "$mh" ] || [ -n "$sh_" ]; then
    wn "cannot compare read-MCP $lib — present on only one of {read-MCP baked, staged}" \
       "undetectable, not a pass: ensure both the read-MCP image and /opt/data/scripts carry $lib"
  fi
done
mh=$(dcx "$MCP" sh -c "sha256sum /app/config/base-schema.yaml 2>/dev/null | cut -d' ' -f1")
sh_=$(dcx "$GW" sh -c "sha256sum /opt/data/config/base-schema.yaml 2>/dev/null | cut -d' ' -f1")
if [ -n "$mh" ] && [ -n "$sh_" ]; then
  [ "$mh" != "$sh_" ] && bad "read-MCP base-schema.yaml is STALE vs the staged source — tier-filtered search is out of date" \
      "rebuild the read-MCP image (docker compose build $MCP && up -d $MCP); a stage-only deploy misses it"
elif [ -n "$mh" ] || [ -n "$sh_" ]; then
  wn "cannot compare read-MCP base-schema.yaml — present on only one of {read-MCP baked, staged}" \
     "undetectable, not a pass: ensure both /app/config/base-schema.yaml and /opt/data/config/base-schema.yaml exist"
fi

# 3b. write-path baked-lib drift (okengine invariant-audit B2) ---------------
# The enforced okengine-write MCP (write_server.py) runs INSIDE the gateway and imports its write-path
# libs from the BAKED /opt/hermes/scripts/cron (its own parent.parent/scripts/cron), NOT the STAGED
# /opt/data/scripts that the cron fleet + deployment_validate read. A stage-only deploy refreshes the
# staged copy while the baked copy stays OLD -> the enforced write guard runs STALE code while
# everything else runs new (the exact trap deployment_validate.check_write_path_libs and CLAUDE.md's
# deploy-surfaces note describe). Compare the two copies INSIDE $GW, over the same libs as
# _WRITE_PATH_LIBS. Present on only one side is UNDETECTABLE, never a pass (the M22 one-sided rule).
for lib in schema_lib.py id_lib.py id_index.py okf_migrate.py; do
  bh=$(dcx "$GW" sh -c "sha256sum /opt/hermes/scripts/cron/$lib 2>/dev/null | cut -d' ' -f1")
  sh_=$(dcx "$GW" sh -c "sha256sum /opt/data/scripts/$lib 2>/dev/null | cut -d' ' -f1")
  if [ -n "$bh" ] && [ -n "$sh_" ]; then
    [ "$bh" != "$sh_" ] && bad "write-path lib $lib is STALE (baked vs staged) — the enforced write path is running old code; rebuild the gateway image" \
        "rebuild the gateway image (build-engine-image.sh) && docker compose up -d $GW; a stage-only deploy misses it"
  elif [ -n "$bh" ] || [ -n "$sh_" ]; then
    wn "cannot compare write-path lib $lib — present on only one of {baked /opt/hermes/scripts/cron, staged /opt/data/scripts}" \
       "undetectable, not a pass: ensure both the gateway image and /opt/data/scripts carry $lib"
  fi
done

# The write server also bakes these two contract artifacts. Keep this scheduler-independent peer in
# lockstep with deployment_checks.check_write_path_libs: a dead cron-plus lane must not hide drift.
for spec in "config/base-schema.yaml:config/base-schema.yaml" \
            "tools/schema_validator.py:config/schema_validator.py"; do
  baked=${spec%%:*}; staged=${spec#*:}
  name=${baked##*/}
  bh=$(dcx "$GW" sh -c "sha256sum /opt/hermes/$baked 2>/dev/null | cut -d' ' -f1")
  sh_=$(dcx "$GW" sh -c "sha256sum /opt/data/$staged 2>/dev/null | cut -d' ' -f1")
  if [ -n "$bh" ] && [ -n "$sh_" ]; then
    [ "$bh" != "$sh_" ] && bad "write-path contract $name is STALE (baked vs staged) — the enforced write path is running an old contract" \
        "rebuild the gateway image (build-engine-image.sh) && docker compose up -d $GW"
  elif [ -n "$bh" ] || [ -n "$sh_" ]; then
    wn "cannot compare write-path contract $name — present on only one of {baked, staged}" \
       "undetectable, not a pass: ensure both the gateway image and /opt/data/config carry $name"
  fi
done

# 3c. gateway api_server exposure (okengine#120) ----------------------------
# The host-net gateway's OpenAI-compatible api_server (the reader Chat relay target)
# binds per API_SERVER_HOST. If it's listening on a NON-loopback interface it's
# LAN-reachable — unnecessary attack surface even when authenticated. Defense-in-depth
# guard, paralleling the MCP guard above (the equivalent posture #120 asks for).
echo "[3c] gateway api_server exposure"
if ! command -v ss >/dev/null 2>&1; then
    wn "ss unavailable — can't probe api_server (:8642) exposure" "install iproute2 to enable the okengine#120 check"
else
    API_BIND=$(ss -ltn 2>/dev/null | awk '{print $4}' | grep -E ':8642$' | head -1)
    if [ -z "$API_BIND" ]; then ok "api_server not listening on :8642 (Chat/api_server feature off) — no exposure"
    else
        acode=$(curl -s -o /dev/null -w "%{http_code}" -m8 "http://127.0.0.1:8642/v1/models")
        case "$API_BIND" in
            127.0.0.1:*|"[::1]:"*) ok "api_server bound to $API_BIND (loopback-only) — not LAN-exposed" ;;
            *)
                if [ "$acode" = "401" ] || [ "$acode" = "403" ]; then
                    wn "api_server is LAN-exposed on $API_BIND (authenticated)" "defense-in-depth: set API_SERVER_HOST=127.0.0.1, or move the gateway to a bridge (okengine#120/#138); keep a strong API_SERVER_KEY"
                else
                    bad "api_server LAN-exposed on $API_BIND and returned $acode without a key" "set a strong API_SERVER_KEY and bind API_SERVER_HOST=127.0.0.1 (okengine#120)"
                fi ;;
        esac
    fi
fi

# 4. MCP write server (stdio, in the gateway) --------------------------------
echo "[4] MCP write server"
# The runtime config is the pack's .hermes-data mounted at /opt/data, NOT under /opt/vault
# (the vault tree) — checking the wrong path produced false write-path/cron-plus FAILs (okengine#106).
CFG=/opt/data/config.yaml
if dcx "$GW" sh -c "grep -q 'okengine-write' $CFG" ; then ok "okengine-write registered in config.yaml"
else bad "okengine-write not in $CFG" "re-run deploy; the enforced write path must be wired into mcp_servers"; fi
if dcx "$GW" test -f /opt/hermes/okengine-mcp/write_server.py; then ok "write_server.py present in the gateway image"
else bad "write_server.py missing in the gateway" "rebuild the gateway image (scripts/build-engine-image.sh)"; fi

# Hermes launches stdio MCP processes with the server's declared env mapping rather than the
# gateway's image-level environment. Check the child-process contract itself: a shell-level import
# can pass while every cron-scoped writer still resolves a nonexistent wheel-relative catalog.
writer_policy_env=$(dcx "$GW" /opt/hermes/.venv/bin/python -c '
import yaml
d=yaml.safe_load(open("/opt/data/config.yaml")) or {}
bad=[]
for name, spec in (d.get("mcp_servers") or {}).items():
    if name == "okengine-write" or name.startswith("okengine-write-"):
        env=(spec or {}).get("env") or {}
        if env.get("OKENGINE_POLICY_CATALOG") != "/opt/hermes/config/policy/catalog.yaml": bad.append(name)
print(",".join(sorted(bad)) or "OK")')
if [ "$writer_policy_env" = "OK" ]; then
    ok "every stdio write server receives the baked policy catalog path"
else
    bad "stdio writer policy env missing or wrong: ${writer_policy_env:-undetectable}" \
        "rerun ensure-runtime from the merged engine and recreate the gateway"
fi

# 4a. composed policy digest + non-mutating least-privilege probe ----------------
expected_policy=$(OKENGINE_POLICY_CATALOG="$VERIFY_ENGINE_DIR/config/policy/catalog.yaml" \
    python3 "$VERIFY_ENGINE_DIR/tools/policy_plane.py" digest --vault "$PWD" 2>/dev/null)
runtime_policy=$(python3 -c 'import json; print(json.load(open(".okengine/effective-policy.json")).get("digest", ""))' 2>/dev/null)
if [ -n "$expected_policy" ] && [ "$expected_policy" = "$runtime_policy" ]; then
    ok "runtime policy digest matches composed source ($runtime_policy)"
else
    bad "runtime policy digest drift (runtime=${runtime_policy:-missing}, expected=${expected_policy:-unavailable})" \
        "rerun deploy.sh so policy is recomposed from merged engine/pack/extension sources"
fi
probe=$(dcx "$GW" sh -c \
    "cd /opt/vault && /opt/hermes/.venv/bin/python -c 'from tools import policy_plane as p; assert p.engine_catalog_path().is_file(); x=p.effective_policy(); r=p.evaluate_capability(x,\"cron:source-quality-backfill\",\"update\",\"sources/probe\",\"source\",[\"type\"],\"none\"); print((r or {}).get(\"rule_id\", \"ALLOW\"))'")
if [ "$probe" = "source-quality-fields-only" ]; then
    ok "source-quality capability probe rejects protected-field mutation without writing"
else
    bad "source-quality capability probe returned ${probe:-nothing}" \
        "rebuild the gateway image and verify the effective policy artifact"
fi

# 4b. governed operation runner (optional review profile) --------------------
if docker compose --profile review config --services 2>/dev/null | grep -Fxq "$OPERATIONS"; then
    echo "[4b] operation runner"
    if docker compose ps --status running --services 2>/dev/null | grep -Fxq "$OPERATIONS"; then
        if dcx "$OPERATIONS" python3 -c \
            'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8732/healthz",timeout=2)' ; then
            ok "operation runner is healthy and bridge-only"
        else
            bad "operation runner health check failed" "docker compose --profile review logs $OPERATIONS"
        fi
        allow=$(dcx "$OPERATIONS" sh -c 'printf %s "$OKENGINE_OPERATION_ALLOW"')
        if [ -n "$allow" ]; then ok "operation runner has an explicit allowlist"
        else bad "operation runner allowlist is empty" "set OKENGINE_OPERATION_ALLOW to approved operation names"; fi
    elif [ -n "${OKENGINE_OPERATION_ALLOW:-}" ]; then
        wn "operations are allowlisted but the runner is not active" \
           "docker compose --profile review up -d --build $OPERATIONS $COCKPIT"
    fi
fi

# 5. cron-plus registration --------------------------------------------------
echo "[5] cron-plus scheduler"
if dcx "$GW" sh -c "grep -q 'cron-plus' $CFG"; then ok "cron-plus plugin enabled in config.yaml"
else bad "cron-plus not enabled in config.yaml" "without it NO cron schedules; see INSTALL.md §4"; fi
njobs=$(dcx "$GW" sh -c 'python3 -c "import json;print(len(json.load(open(\"/opt/data/cron-plus/jobs.json\")).get(\"jobs\",[])))"' 2>/dev/null)
if [ -n "$njobs" ] && [ "$njobs" -gt 0 ] 2>/dev/null; then ok "cron-plus has $njobs jobs registered"
else bad "cron-plus jobs.json empty/absent" "CRON_PACK_DIR=<pack> bash ../okengine/scripts/deploy-cron-plus-jobs.sh"; fi
# FRESHNESS, not presence: the pinned plugin re-opens .tick.lock with open("w") EVERY tick (verified
# scheduler.py _try_acquire_lock @ 6b230dc; run_ticker tick()s once at startup before the first
# sleep), so a HEALTHY ticker's lock mtime is >= this gateway's start AND advances ~every 60s. But
# the lock is NEVER unlinked and lives on the bind-mounted .hermes-data, so a PRESENCE test passes
# forever off a fossil from a prior life (invariant-audit HIGH). The zero-wait discriminator: a lock
# whose mtime PREDATES the container start is a fossil the CURRENT scheduler never refreshed — it is
# dead on arrival (plugin dir missing / import crash / CRON_PLUS_DISABLED — causes not covered by the
# ownership gate 5c). This closes the re-verify gap where a <180s-old fossil passed right after a roll.
# $GW is the compose SERVICE name (for `docker compose exec`), NOT a container name — resolve the
# real container id for docker inspect (a bare `docker inspect gateway` errors -> empty; and
# `date -d ""` returns TODAY-MIDNIGHT, a bogus nonzero epoch, so guard the empty case explicitly —
# both re-verify gaps that made the fossil discriminator inert on a real deployment).
cid="$(docker compose ps -q "$GW" 2>/dev/null | head -1)"
started="$(docker inspect "$cid" --format '{{.State.StartedAt}}' 2>/dev/null)"
started_epoch=0
[ -n "$started" ] && started_epoch=$(date -d "$started" +%s 2>/dev/null || echo 0)
lock_mtime=$(dcx "$GW" sh -c 'f=/opt/data/cron-plus/.tick.lock; [ -f "$f" ] && stat -c %Y "$f" || echo -1' 2>/dev/null | tr -d '[:space:]')
now_epoch=$(date +%s)
if [ "$lock_mtime" = "-1" ] || [ -z "$lock_mtime" ]; then
  wn "no .tick.lock — scheduler may not have ticked yet" "give it a minute, then re-check; else docker compose logs $GW"
elif [ "$started_epoch" -gt 0 ] 2>/dev/null && [ "$lock_mtime" -lt "$started_epoch" ] 2>/dev/null; then
  bad "cron-plus .tick.lock is a FOSSIL — its mtime predates this gateway's start, so the CURRENT scheduler has NEVER ticked (dead on arrival)" \
      "docker compose logs $GW | grep -iE 'tick error|cron-plus'; check CRON_PLUS_DISABLED in the gateway env and that plugins/cron-plus is present"
elif [ $(( now_epoch - lock_mtime )) -le 180 ] 2>/dev/null; then
  ok "cron-plus is ticking (.tick.lock fresh, $(( now_epoch - lock_mtime ))s old)"
else
  bad "cron-plus .tick.lock is STALE ($(( now_epoch - lock_mtime ))s old, > 3 ticks) — the scheduler ticked once then STOPPED" \
      "docker compose logs $GW | grep -i 'tick error'; check CRON_PLUS_DISABLED in the gateway env and that plugins/cron-plus is present"
fi
# 5b. scheduler-stalled sentinel — the tick.lock freshness check above is BLIND to a
# ticking-but-not-loading scheduler: tick() refreshes .tick.lock via open("w") BEFORE load_jobs(),
# so a store-unreadable stall (the exact condition cron-plus drops .scheduler-stalled for, #197)
# keeps a fresh lock and passes 5a. The sentinel is the machine-readable alarm for "NO lanes
# firing", but its only other reader is deployment_validate — a cron LANE the stalled scheduler
# never runs. Read it HERE, at the scheduler-independent deploy gate (invariant-audit HIGH #2).
stalled=$(dcx "$GW" sh -c 'f=/opt/data/cron-plus/.scheduler-stalled; [ -f "$f" ] && cat "$f" || true' 2>/dev/null)
if [ -n "$stalled" ]; then
  why=$(printf '%s' "$stalled" | python3 -c 'import sys,json;
try: print(json.load(sys.stdin).get("error","") or "unreadable job store")
except Exception: print("unreadable job store")' 2>/dev/null || echo "unreadable job store")
  bad "cron-plus scheduler STALLED sentinel present ($why) — it is ticking but cannot load jobs.json, so NO lanes are firing (5a's fresh .tick.lock is misleading here)" \
      "docker compose logs $GW | grep -iE 'cron-plus|load_jobs'; validate /opt/data/cron-plus/jobs.json, then restart $GW"
fi
# 5c. runtime-dir ownership — the ticker + every lane run AS $HERMES_UID and must OWN /opt/data to
# write .tick.lock/jobs.json. A tree owned by a DIFFERENT uid (brought up with the compose default
# 10000 while the mounted .hermes-data is the operator's uid) kills the scheduler on a
# PermissionError. The .tick.lock check above passes on a CONSISTENT deploy but not on a later uid
# desync (a bare recreate without HERMES_UID) — catch that here, at the deploy-time gate that runs
# regardless of scheduler health (deployment-validate can't: a dead ticker never runs its lane).
want_uid="$(dcx "$GW" sh -c 'echo ${HERMES_UID:-10000}' 2>/dev/null | tr -d '[:space:]')"
got_uid="$(dcx "$GW" stat -c '%u' /opt/data/cron-plus 2>/dev/null | tr -d '[:space:]')"
# The single most critical FILE: cron-plus/jobs.json mis-owned (e.g. root:0600 from a bare
# `docker compose exec`/`docker exec` regenerate with NO -u on the s6 gateway) is UNREADABLE by the
# lane uid, so the scheduler goes dark even though the cron-plus DIR above is correctly owned — the
# exact fleet-stall poison hit live (okengine#193). The dir-level stat misses a mis-owned file in a
# well-owned dir; stat the FILE too, matching deployment_validate.check_runtime_ownership. Empty =>
# absent (already FAILed by the jobs.json check above), so skip.
job_uid="$(dcx "$GW" stat -c '%u' /opt/data/cron-plus/jobs.json 2>/dev/null | tr -d '[:space:]')"
# Distinguish EMPTY probes (exec failed — gateway crash-looping/stopped, exactly the uid-desync 5c
# hunts) from a real match: an empty want_uid/got_uid means nothing was measured, so reporting PASS
# is a vacuous green that violates the repo's 'missing key = WARN undetectable, never a vacuous pass'
# rule (M22) in the one gate that is the designated peer for the in-lane checks a dead ticker can't
# run (invariant-audit #48).
if [ -z "$want_uid" ] || [ -z "$got_uid" ]; then
    wn "cannot verify runtime ownership — the gateway is not exec-able (crash-looping/stopped?), so the uid could not be read; UNDETECTABLE here, not a pass" \
       "docker compose ps $GW; docker compose logs $GW  (this is the failure mode 5c exists to catch)"
elif [ "$got_uid" != "$want_uid" ]; then
    bad "runtime /opt/data/cron-plus owned by uid $got_uid but the gateway runs as $want_uid" \
        "the scheduler dies on .tick.lock; pin HERMES_UID=$got_uid in .env + recreate, or chown .hermes-data to $want_uid"
elif [ -n "$job_uid" ] && [ "$job_uid" != "$want_uid" ]; then
    bad "runtime /opt/data/cron-plus/jobs.json owned by uid $job_uid but the gateway runs as $want_uid" \
        "the scheduler can't READ it (root:0600 poison) and the WHOLE fleet stalls (okengine#193); chown jobs.json to $want_uid, or re-run deploy-cron-plus-jobs.sh with HERMES_UID=$want_uid"
else ok "runtime dir + jobs.json owned by the gateway uid ($got_uid)"; fi

# 5e. secret file modes (okengine#665) — host-side, needs no docker. .env is read by compose on the
# host only, so anything beyond owner-only is a leak of model keys/tokens/DB passwords; the
# read-MCP Bearer token in config.yaml must be readable by the gateway uid, so world-readable is
# a WARN there (it is what --fix-perms produces) and a FAIL only for .env.
if [ -f .env ]; then
    env_mode="$(stat -c '%a' .env 2>/dev/null)"
    if [ -n "$env_mode" ] && [ "${env_mode: -2}" != "00" ]; then
        bad ".env is mode $env_mode — model keys, OKENGINE_MCP_TOKEN and DB passwords are readable by group/other" \
            "chmod 600 .env (deploy.sh/ensure-runtime.sh now do this; an older deploy left the caller's umask)"
    else ok ".env is owner-only (mode ${env_mode:-?})"; fi
fi
if [ -f .hermes-data/config.yaml ]; then
    cfg_mode="$(stat -c '%a' .hermes-data/config.yaml 2>/dev/null)"
    if [ -n "$cfg_mode" ] && [ "${cfg_mode: -1}" != "0" ]; then
        wn ".hermes-data/config.yaml is mode $cfg_mode — the read-MCP Bearer token is world-readable" \
           "acceptable only as a --fix-perms local convenience; prefer HERMES_UID=\$(id -u) so the file can be owner-only"
    else ok "config.yaml is not world-readable (mode ${cfg_mode:-?})"; fi
fi
# 5d. runtime-tree ownership SWEEP — 5c stats exactly TWO paths (the cron-plus dir + jobs.json), so a
# correctly-owned dir can still hold hundreds of mis-owned files underneath and 5c stays green. Not
# hypothetical: 774 root-owned files accumulated across the fleet's .hermes-data over three days
# (okengine#557) with 5c passing throughout, because cron-plus/ and jobs.json were both fine. It went
# unnoticed for a WEEK, and the damage was silent in both directions:
#   * one gateway could not open /opt/data/logs/agent.log at all — it logged NOTHING for seven days;
#   * root-owned cron-plus/runs/<id>/ dirs rejected receipt writes, so a lane RAN, did its work, and
#     lost its receipt — which downstream reads as "the lane did nothing" (empty parse is `unknown`,
#     never `nothing happened`).
# 5c's own comment already names this hazard ("the dir-level stat misses a mis-owned file in a
# well-owned dir") but only closes it for jobs.json. Sweep the tree instead of spot-checking it:
# ~80k files costs <0.5s, which is nothing at a deploy gate.
if [ -n "$want_uid" ]; then
    nstray="$(dcx "$GW" sh -c "find /opt/data ! -uid $want_uid 2>/dev/null | wc -l" | tr -d '[:space:]')"
    # Empty => the probe never ran (gateway not exec-able). Reporting PASS there is the vacuous green
    # the repo's "missing key = WARN undetectable, never a vacuous pass" rule (M22) exists to forbid.
    if [ -z "$nstray" ]; then
        wn "cannot sweep runtime-tree ownership — the find probe returned nothing (gateway not exec-able?); UNDETECTABLE here, not a pass" \
           "docker compose ps $GW; docker compose logs $GW, then re-run once the gateway is up"
    elif [ "$nstray" -gt 0 ] 2>/dev/null; then
        eg="$(dcx "$GW" sh -c "find /opt/data ! -uid $want_uid 2>/dev/null | head -3" | tr '\n' ' ')"
        bad "$nstray file(s) under /opt/data are NOT owned by the gateway uid $want_uid (e.g. ${eg:-?})" \
            "every write to them fails SILENTLY — dropped cron receipts, an unwritable agent.log (okengine#557); fix with 'sudo chown -R $want_uid:$want_uid <pack>/.hermes-data'. Do NOT use ensure-runtime.sh --fix-perms: it makes the tree world-writable, trading an ownership bug for a permissions downgrade"
    else
        ok "runtime tree fully owned by the gateway uid ($want_uid) — $(dcx "$GW" sh -c 'find /opt/data 2>/dev/null | wc -l' | tr -d '[:space:]') paths swept"
    fi
fi

# 5f. VAULT ownership peer (invariant-audit #4) — deployment_validate's ownership check is a
# cron-plus lane, so a stalled scheduler cannot report a root-owned INDEX/dashboard directory.
# Run the SAME shared check independently as the actual lane uid inside the gateway; a host-side
# invocation would compare against the operator's uid and could report a false green or false FAIL.
echo "[5f] vault ownership (scheduler-independent peer)"
if [ -z "$want_uid" ] || ! [[ "$want_uid" =~ ^[0-9]+$ ]]; then
    wn "cannot verify vault ownership — gateway lane uid is unavailable; UNDETECTABLE here, not a pass" \
       "docker compose ps $GW; docker compose logs $GW, then re-run the verifier"
else
    vault_owners=$(dcx -u "$want_uid" "$GW" python3 -c '
import sys
sys.path.insert(0, "/opt/data/scripts")
try:
    import deployment_checks as C
    C.configure("/opt/vault", data="/opt/data", hermes="/opt/hermes")
    findings = C.run(["ownership"])
    for level, area, msg in findings:
        print("%s\t%s\t%s" % (level, area, " ".join(msg.split())))
    if not findings:
        print("OK\townership\tall lane-maintained vault paths owned by the gateway lane uid")
except Exception as exc:
    print("ERR\townership\tshared check unavailable: %s" % exc)
# vault-ownership-peer
' 2>/dev/null)
    if [ -z "$vault_owners" ]; then
        wn "cannot verify vault ownership — in-container check returned nothing; UNDETECTABLE here, not a pass" \
           "docker compose ps $GW; check /opt/data/scripts/deployment_checks.py is staged, then re-run"
    else
        while IFS=$'\t' read -r level area msg; do
            case "$level" in
                OK) ok "$msg" ;;
                FAIL) bad "[$area] $msg" "fix-vault-ownership.sh for this deployment, then re-run post-deploy verification" ;;
                WARN) wn "[$area] $msg" "framework doctor $PWD --checks ownership" ;;
                ERR) wn "vault ownership check unavailable ($msg); UNDETECTABLE here, not a pass" \
                        "check staged deployment_checks.py and gateway Python, then re-run" ;;
                *) wn "vault ownership check returned an unexpected result; UNDETECTABLE here, not a pass" \
                      "inspect the gateway and re-run the verifier" ;;
            esac
        done <<< "$vault_owners"
    fi
fi

# 5b. NB: backlinks-refresh no longer needs an iwe binary (okengine#179 — it builds the graph
# with an in-process link-scanner), so there is no gateway iwe dependency to verify here anymore.

# 6. search index (qmd) ------------------------------------------------------
# qmd stores its index under XDG dirs inside the mcp container (engine-standard layout);
# bare `qmd` can't find it, so point it at the cache/config explicitly.
echo "[6] search index (qmd)"
QC=${OKENGINE_QMD_CACHE:-/opt/data/qmd/cache}
QCFG=${OKENGINE_QMD_CONFIG:-/opt/data/qmd/config}
QDIR=${OKENGINE_QMD_DIR:-/opt/data/qmd}
ndocs=$(dcx "$MCP" sh -c "XDG_CACHE_HOME=$QC XDG_CONFIG_HOME=$QCFG qmd status 2>/dev/null | grep -iE 'Total:' | grep -oE '[0-9]+' | head -1")
if [ -n "$ndocs" ] && [ "$ndocs" -gt 0 ] 2>/dev/null; then ok "qmd index ready ($ndocs files indexed)"
else
    # 0 docs is ambiguous: a fresh index still building, OR a PERMANENTLY broken one because the
    # qmd subdir (its own bind-mount, docker-compose.yml `.hermes-data/qmd:/opt/data/qmd`) is owned
    # by a uid the mcp container can't write (e.g. `rm .hermes-data/qmd` + a bare `docker compose up`
    # re-creates the source as root; ensure-runtime.sh only probes the top-level .hermes-data). No
    # cron builds qmd — corpus_indexer.py writes state/corpus-index/*.jsonl, a DIFFERENT index — so
    # "wait for a cron" is a false remedy. Probe writability to tell the two apart.
    if dcx "$MCP" sh -c "touch $QDIR/.pdv_wtest 2>/dev/null && rm -f $QDIR/.pdv_wtest 2>/dev/null"; then
        wn "qmd index not ready (0 files) but $QDIR is writable — still building" \
           "run 'qmd update' in $MCP to build it now, or let the next 'qmd update' populate it"
    else
        bad "qmd index empty and $QDIR is NOT writable by $MCP — 'qmd update' fails with a PermissionError, the index stays empty forever" \
            "chown .hermes-data/qmd to the mcp uid (HERMES_UID) + recreate $MCP; a bare 'docker compose up' after 'rm .hermes-data/qmd' re-creates it root-owned"
    fi
fi

# 8. deployment timezone reaches the UI clocks (okengine#301) ----------------
# A non-UTC TZ in .env must actually reach the reader/cockpit containers, or their clocks AND the
# dates cron scripts stamp onto content render in UTC — the drift that shipped to several readers
# whose (stale-skeleton) compose omitted `TZ=${TZ:-UTC}` on the reader service even though the same
# stack's cockpit had it. Only meaningful when a real zone is intended; unset/UTC is the engine
# default and correct by definition, so we skip it (no false FAIL on a UTC deployment).
echo "[8] deployment timezone -> UI clocks (okengine#301)"
EXPECT_TZ="${TZ:-}"
if [ -z "$EXPECT_TZ" ] || [ "$EXPECT_TZ" = "UTC" ]; then
    ok "TZ unset/UTC — UTC clocks are correct by default (nothing to verify)"
elif ! OKENGINE_VERIFY_TZ="$EXPECT_TZ" python3 -c \
    'import os; from zoneinfo import ZoneInfo; ZoneInfo(os.environ["OKENGINE_VERIFY_TZ"])' \
    >/dev/null 2>&1; then
    bad "deployment TZ=$EXPECT_TZ is not a valid IANA timezone — services would silently disagree with the intended calendar" \
        "correct TZ in .env (for example America/New_York), then recreate every service"
else
    UI_AUTH=(); [ -n "$READER_PW" ] && UI_AUTH=(-u "${OKENGINE_READER_USER:-okengine}:$READER_PW")
    # served tz from a UI JSON endpoint, or empty if unreachable/unauthorized/pre-#301
    _served_tz() { curl -s -m8 "${UI_AUTH[@]}" "$1" 2>/dev/null \
        | python3 -c "import sys,json;print((json.load(sys.stdin) or {}).get('tz',''))" 2>/dev/null; }
    _check_ui_tz() {   # $1=label $2=service $3=api-path
        local b; b=$(hostport "$2" 9200)
        if [ -z "$b" ]; then wn "$1 port unpublished — clock tz UNVERIFIED" "expose $2, or check its container TZ directly (docker compose exec $2 printenv TZ)"; return; fi
        local tz; tz=$(_served_tz "http://127.0.0.1:${b##*:}$3")
        if [ -z "$tz" ]; then wn "$1 clock tz UNVERIFIED (no tz in $3 — auth, or a pre-#301 image)" "confirm OKENGINE_READER_USER/PASSWORD reach $2, or rebuild its image"
        elif [ "$tz" = "$EXPECT_TZ" ]; then ok "$1 clock tz=$tz (matches TZ)"
        else bad "$1 clock tz=$tz but the deployment TZ=$EXPECT_TZ" "add '- TZ=\${TZ:-UTC}' to the $2 service environment in docker-compose.yml, then: docker compose up -d --force-recreate $2 (okengine#301)"; fi
    }
    _check_ui_tz "reader" "$READER" "/api/about"
    if has_service "$COCKPIT"; then
        _check_ui_tz "cockpit" "$COCKPIT" "/api/config"
    fi
fi

# 9. deployment self-checks (shared library, okengine#405) -------------------
# The filesystem checks below run through the SAME shared library the daily in-gateway validator and
# `framework doctor` use (scripts/cron/deployment_checks.py) — one source of truth instead of a shell
# re-implementation. Only the host-readable, uid-neutral checks run here (auth/toolset lockdown,
# composed schema, sub-domains, timezone, partition dups, rules, extensions, provenance, pins,
# operation runs). The BAKED-vs-staged
# write-path drift (3b), runtime-uid ownership (5c), and vault ownership (5f) stay above: they require in-container
# access (the baked /opt/hermes libs, the container's HERMES_UID) that a host-side read cannot reach.
echo "[9] deployment self-checks (shared library)"
selfchecks=$(python3 - "$VERIFY_ENGINE_DIR" "$PWD" <<'PY' 2>/dev/null
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "scripts", "cron"))
try:
    import deployment_checks as C
except Exception as exc:
    print("ERR\t\t%s" % exc); raise SystemExit(0)
dep = sys.argv[2]
C.configure(dep, data=os.path.join(dep, ".hermes-data"),
            hermes=os.path.join(dep, ".no-baked-image-on-host"))
subset = ["pins", "schema", "subdomains", "crons", "timezone", "partition-dups",
          "rules", "extensions", "provenance", "operations", "auth"]
for level, area, msg in C.run(subset):
    print("%s\t%s\t%s" % (level, area, " ".join(msg.split())))
PY
)
if [ -z "$selfchecks" ]; then
    ok "shared checks produced no findings (or the engine checkout is unavailable)"
else
    while IFS=$'\t' read -r level area msg; do
        [ -z "$level" ] && continue
        case "$level" in
            FAIL) bad "[$area] $msg" "framework doctor $PWD --checks $area  (fix, then re-run deploy)" ;;
            WARN) wn  "[$area] $msg" "framework doctor $PWD --checks $area" ;;
            INFO) ok  "[$area] $msg" ;;
            ERR)  wn  "shared checks unavailable ($msg)" "run from the deployment dir with the engine checkout intact" ;;
        esac
    done <<< "$selfchecks"
fi

# unmaintained running services -----------------------------------------------
# A container that is RUNNING but absent from the resolved Compose configuration is one no deploy
# can ever touch: `docker compose up -d --build` only knows the services `config` resolves, so an
# orphan keeps running whatever image it started with, for ever, silently.
#
# The live case this was written for: okcti-test runs okengine-operation-runner and
# okengine-review-write behind `profiles: ["review"]` with COMPOSE_PROFILES unset. `config`
# resolved 6 services, `ps` showed 8, and the two invisible ones drifted 20 commits behind while
# every deploy reported success. That is also the root of the okengine#590 incident below --
# provenance caught the symptom, and this catches the reason.
if [ "$COMPOSE_SERVICES_RC" -eq 0 ]; then
    # `if ! var=$(...)` rather than reading $? after the assignment (okengine#602): the status
    # belongs to the command that produced it, and a masked one is how a check stops failing.
    if ! running_svcs=$(docker compose ps --services 2>/dev/null); then
        wn "cannot list running services — orphan check UNDETECTABLE, not a pass" \
           "docker compose ps --services failed here; re-run when the daemon is responsive"
    else
        orphans=""
        for svc in $running_svcs; do
            has_service "$svc" || orphans="$orphans $svc"
        done
        if [ -n "$orphans" ]; then
            bad "running but NOT in the resolved Compose config:$orphans" \
                "no deploy can rebuild or recreate these — they run their original image for ever. If they are profile-gated, pin the profile (e.g. COMPOSE_PROFILES=review in .env); if they are obsolete, remove them."
        else
            ok "every running service is in the resolved Compose config"
        fi
    fi
fi

# image provenance (okengine#590) --------------------------------------------
# The other three deploy surfaces are checked; this is the fourth. `okcti-review-write` --
# write_server.py mounting the vault read-write -- ran three weeks on an image 20 commits old,
# missing write-path ENFORCEMENT the gateway had already gained, and nothing said so. An invariant
# enforced at one boundary and not the other is not enforced, and here both boundaries were the
# same file at two ages. The gateway image is covered by its org.okengine.git_sha label; these
# are compose-built from a docker-compose.yml the PACK owns, so they are verified by content.
img_project="${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}"
if img_out=$(python3 "$VERIFY_ENGINE_DIR/scripts/image_drift.py" \
        --project "$img_project" --engine-dir "$VERIFY_ENGINE_DIR" 2>&1); then
    ok "engine images built from this source ($(printf '%s' "$img_out" | grep -c 'in sync') in sync)"
else
    bad "engine image(s) not built from this source: $(printf '%s' "$img_out" | grep -E 'DRIFTED|UNDETECTABLE' | tr '\n' ' ')" \
        "rebuild and roll them: docker compose build <svc> && docker compose up -d --force-recreate <svc>"
fi

# summary --------------------------------------------------------------------
echo "================================="
printf "%d pass, %d warn, %d fail\n" "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] && { echo "deployment looks healthy."; exit 0; } || { echo "deployment has FAILs — see remediation above."; exit 1; }
