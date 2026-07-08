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

pass=0; warn=0; fail=0
ok()  { printf "  \033[32mPASS\033[0m  %s\n" "$1"; pass=$((pass+1)); }
wn()  { printf "  \033[33mWARN\033[0m  %s\n        ↳ %s\n" "$1" "$2"; warn=$((warn+1)); }
bad() { printf "  \033[31mFAIL\033[0m  %s\n        ↳ %s\n" "$1" "$2"; fail=$((fail+1)); }
dcx() { docker compose exec -T "$@" 2>/dev/null; }

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
for svc in "$GW" "$MCP" "$READER"; do
    state=$(docker compose ps --status running --services 2>/dev/null | grep -Fx "$svc")
    if [ -n "$state" ]; then ok "$svc is running"
    else bad "$svc is not running" "docker compose up -d $svc  (then: docker compose logs $svc)"; fi
done

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

# 3. MCP read server ---------------------------------------------------------
echo "[3] MCP read server"
MB=$(hostport "$MCP" 8730)
if [ -z "$MB" ]; then wn "MCP port 8730 not published" "the agent reaches it in-network; only publish it if a host client needs it"
else
    MIP=${MB%:*}; MPORT=${MB##*:}; MURL="http://127.0.0.1:$MPORT"
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

# 3b. gateway api_server exposure (okengine#120) ----------------------------
# The host-net gateway's OpenAI-compatible api_server (the reader Chat relay target)
# binds per API_SERVER_HOST. If it's listening on a NON-loopback interface it's
# LAN-reachable — unnecessary attack surface even when authenticated. Defense-in-depth
# guard, paralleling the MCP guard above (the equivalent posture #120 asks for).
echo "[3b] gateway api_server exposure"
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

# 5. cron-plus registration --------------------------------------------------
echo "[5] cron-plus scheduler"
if dcx "$GW" sh -c "grep -q 'cron-plus' $CFG"; then ok "cron-plus plugin enabled in config.yaml"
else bad "cron-plus not enabled in config.yaml" "without it NO cron schedules; see INSTALL.md §4"; fi
njobs=$(dcx "$GW" sh -c 'python3 -c "import json;print(len(json.load(open(\"/opt/data/cron-plus/jobs.json\")).get(\"jobs\",[])))"' 2>/dev/null)
if [ -n "$njobs" ] && [ "$njobs" -gt 0 ] 2>/dev/null; then ok "cron-plus has $njobs jobs registered"
else bad "cron-plus jobs.json empty/absent" "CRON_PACK_DIR=<pack> bash ../okengine/scripts/deploy-cron-plus-jobs.sh"; fi
if dcx "$GW" test -f /opt/data/cron-plus/.tick.lock; then ok "cron-plus is ticking (.tick.lock present)"
else wn "no .tick.lock — scheduler may not have ticked yet" "give it a minute, then re-check; else docker compose logs $GW"; fi
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
if [ -n "$want_uid" ] && [ -n "$got_uid" ] && [ "$got_uid" != "$want_uid" ]; then
    bad "runtime /opt/data/cron-plus owned by uid $got_uid but the gateway runs as $want_uid" \
        "the scheduler dies on .tick.lock; pin HERMES_UID=$got_uid in .env + recreate, or chown .hermes-data to $want_uid"
elif [ -n "$want_uid" ] && [ -n "$job_uid" ] && [ "$job_uid" != "$want_uid" ]; then
    bad "runtime /opt/data/cron-plus/jobs.json owned by uid $job_uid but the gateway runs as $want_uid" \
        "the scheduler can't READ it (root:0600 poison) and the WHOLE fleet stalls (okengine#193); chown jobs.json to $want_uid, or re-run deploy-cron-plus-jobs.sh with HERMES_UID=$want_uid"
else ok "runtime dir + jobs.json owned by the gateway uid (${got_uid:-?})"; fi

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

# summary --------------------------------------------------------------------
echo "================================="
printf "%d pass, %d warn, %d fail\n" "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] && { echo "deployment looks healthy."; exit 0; } || { echo "deployment has FAILs — see remediation above."; exit 1; }
