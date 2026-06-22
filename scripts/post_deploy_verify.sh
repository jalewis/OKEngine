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
hostport() { docker compose port "$1" "$2" 2>/dev/null | tail -1; }   # ip:port or empty

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

# 4. MCP write server (stdio, in the gateway) --------------------------------
echo "[4] MCP write server"
CFG=/opt/vault/.hermes-data/config.yaml
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

# 6. search index (qmd) ------------------------------------------------------
# qmd stores its index under XDG dirs inside the mcp container (engine-standard layout);
# bare `qmd` can't find it, so point it at the cache/config explicitly.
echo "[6] search index (qmd)"
QC=${OKENGINE_QMD_CACHE:-/opt/data/qmd/cache}
QCFG=${OKENGINE_QMD_CONFIG:-/opt/data/qmd/config}
ndocs=$(dcx "$MCP" sh -c "XDG_CACHE_HOME=$QC XDG_CONFIG_HOME=$QCFG qmd status 2>/dev/null | grep -iE 'Total:' | grep -oE '[0-9]+' | head -1")
if [ -n "$ndocs" ] && [ "$ndocs" -gt 0 ] 2>/dev/null; then ok "qmd index ready ($ndocs files indexed)"
else wn "qmd index not ready (0 files or status unreadable)" "run 'qmd update' in $MCP, or wait for the corpus-indexer cron to build it"; fi

# summary --------------------------------------------------------------------
echo "================================="
printf "%d pass, %d warn, %d fail\n" "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] && { echo "deployment looks healthy."; exit 0; } || { echo "deployment has FAILs — see remediation above."; exit 1; }
