#!/usr/bin/env bash
# Seed a pack's runtime dir (.hermes-data) BEFORE `docker compose up`, and make
# sure the gateway uid can actually write it.
#
# A library pack fetched from git has no .hermes-data/ — it's gitignored runtime
# state, seeded by `framework init`/`pull`. A plain `git clone` (the common case
# for a catalog pack) skips that, and the gateway bind-mounts
# `./.hermes-data:/opt/data`. If the dir is missing at compose-up Docker
# auto-creates it as ROOT; and even when present, the gateway runs as
# HERMES_UID:HERMES_GID (default: the invoking user's uid, so a clone-as-yourself
# tree is writable out of the box) — if you instead PIN a uid the tree isn't
# writable by, the gateway can't `mkdir /opt/data/logs` and stays unhealthy while
# showing as Up (issue #16, okengine#102).
#
# So this: (1) seeds .hermes-data (config.yaml from the engine template + qmd/ +
# logs/), idempotently (an existing config is left untouched); (2) ensures the
# tree is writable by HERMES_UID — and if it isn't, FAILS before compose with an
# actionable message (or fixes it with --fix-perms).
#
# Usage:
#   bash $ENGINE_DIR/scripts/ensure-runtime.sh [pack-dir] [--fix-perms]
#   CRON_PACK_DIR=/path/to/pack bash .../ensure-runtime.sh
# Env: HERMES_UID/HERMES_GID (default: your uid/gid), FIX_PERMS=1 (same as --fix-perms).
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACK_ARG=""; FIX_PERMS="${FIX_PERMS:-0}"
for a in "$@"; do
    case "$a" in
        --fix-perms) FIX_PERMS=1 ;;
        -*) echo "unknown flag: $a" >&2; exit 2 ;;
        *) PACK_ARG="$a" ;;
    esac
done
PACK="$(cd "${PACK_ARG:-${CRON_PACK_DIR:-$PWD}}" && pwd)"
TMPL="$ENGINE_DIR/config/config.yaml.template"
RT="$PACK/.hermes-data"
CFG="$RT/config.yaml"
# okengine#197/#185 + invariant-audit HIGH: resolve the uid the way compose/deploy do (shared
# resolver: env > .env pin > tree owner), NOT a bare $(id -u) that ignores the .env pin — and PIN it
# into .env so a bare `docker compose up` interpolates the SAME uid. Without this, the documented
# step-by-step bring-up seeded the runtime as one uid while compose ran the gateway as ${...:-10000},
# deploying a silently-dead scheduler the writability gate below (checking the wrong uid) couldn't see.
# shellcheck source=lib/hermes_uid.sh
. "$ENGINE_DIR/scripts/lib/hermes_uid.sh"
HUID="$(resolve_hermes_uid "$PACK")"; HGID="$(resolve_hermes_gid "$PACK")"
ENVF="$PACK/.env"
[ -f "$ENVF" ] || : > "$ENVF"
grep -qE '^[[:space:]]*HERMES_UID[[:space:]]*=' "$ENVF" || printf 'HERMES_UID=%s\n' "$HUID" >> "$ENVF"
grep -qE '^[[:space:]]*HERMES_GID[[:space:]]*=' "$ENVF" || printf 'HERMES_GID=%s\n' "$HGID" >> "$ENVF"

mkdir -p "$RT/qmd" "$RT/logs"
[ -f "$RT/.gitkeep" ] || : > "$RT/.gitkeep"

if [ -f "$CFG" ]; then
    echo "ok: $CFG already present (operator values preserved)"
elif [ -f "$TMPL" ]; then
    cp "$TMPL" "$CFG"
    echo "seeded: $CFG  <- $TMPL"
    echo "  review it (model provider, delivery) before deploy; secrets go in .env"
else
    echo "ERROR: engine config template not found: $TMPL" >&2
    exit 1
fi

# Reconcile engine-managed security defaults introduced after a deployment's first seed. Operator
# values remain authoritative: an existing api_server toolset is never overwritten (and the runtime
# validator rejects unsafe values). We only add the formerly-absent lockdown to old configs, preserving
# their comments, formatting, model choices, and secrets instead of YAML round-tripping the whole file.
CFG="$CFG" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["CFG"])
text = path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
top = next((i for i, line in enumerate(lines)
            if re.match(r"^platform_toolsets\s*:", line)), None)
block = (
    "platform_toolsets:\n"
    "  api_server:\n"
    "    - okengine\n"
    "    - okengine-write\n"
)
changed = False
if top is None:
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n# Engine-managed network-agent safety baseline (reconciled by ensure-runtime).\n" + block
    changed = True
elif re.match(r"^platform_toolsets:\s*(?:#.*)?(?:\r?\n)?$", lines[top]):
    end = len(lines)
    for i in range(top + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith("#") and not line.startswith((" ", "\t")):
            end = i
            break
    if not any(re.match(r"^[ \t]+api_server\s*:", line)
               for line in lines[top + 1:end]):
        insertion = (
            "  # Engine-managed network-agent safety baseline (reconciled by ensure-runtime).\n"
            "  api_server:\n"
            "    - okengine\n"
            "    - okengine-write\n"
        )
        lines.insert(end, insertion)
        text = "".join(lines)
        changed = True
else:
    print(f"warn: {path} uses an inline/non-mapping platform_toolsets value; "
          "left operator config unchanged (deployment validation will enforce safety)")
if changed:
    path.write_text(text, encoding="utf-8")
    print(f"reconciled: {path} (added missing platform_toolsets.api_server safety baseline)")
PY

# Reconcile the dedicated source-quality writer into older seeded configs. Its
# process environment is the server-verifiable job identity; a prompt/job_id
# argument cannot impersonate it. Insert only when absent and preserve all
# operator-owned model, delivery, and secret configuration verbatim.
CFG="$CFG" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["CFG"])
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

def top_level(name):
    return next((i for i, line in enumerate(lines)
                 if re.match(rf"^{re.escape(name)}\s*:\s*(?:#.*)?$", line.rstrip("\n"))), None)

def section_end(start):
    return next((i for i in range(start + 1, len(lines))
                 if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))

mcp_start = top_level("mcp_servers")
if mcp_start is None:
    # A malformed/minimal operator config is left byte-for-byte unchanged; the
    # existing deployment validator reports its missing required MCP block.
    raise SystemExit(0)
mcp_end = section_end(mcp_start)
key = "  okengine-write-source-quality:"
if any(line.rstrip() == key for line in lines[mcp_start + 1:mcp_end]):
    raise SystemExit(0)

# Repair the short-lived legacy bug that placed this engine-managed entry under
# the following top-level section (commonly `web:`). The reserved key is safe to
# move; no operator-owned fields live beneath it.
misplaced = next((i for i, line in enumerate(lines)
                  if line.rstrip() == key and not (mcp_start < i < mcp_end)), None)
if misplaced is not None:
    remove_start = misplaced
    if misplaced and "Engine-managed server-bound identity" in lines[misplaced - 1]:
        remove_start -= 1
    remove_end = next((i for i in range(misplaced + 1, len(lines))
                       if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= 2),
                      len(lines))
    del lines[remove_start:remove_end]
    mcp_start = top_level("mcp_servers")
    mcp_end = section_end(mcp_start)

block = (
    "  # Engine-managed server-bound identity for source-quality-backfill.\n"
    "  okengine-write-source-quality:\n"
    "    command: /opt/hermes/.venv/bin/python\n"
    "    args:\n"
    "    - /opt/hermes/okengine-mcp/write_server.py\n"
    "    env:\n"
    "      OKENGINE_WRITE_ACTOR: cron:source-quality-backfill\n\n"
)
lines.insert(mcp_end, block)
path.write_text("".join(lines), encoding="utf-8")
print(f"reconciled: {path} (added server-bound source-quality writer)")
PY

# Reconcile every additional cron capability from the deploy-materialized policy into a dedicated
# writer process. This turns policy identity into runtime identity without asking packs to commit
# their secret-bearing .hermes-data/config.yaml. Existing operator entries are never rewritten.
CFG="$CFG" PACK="$PACK" python3 - <<'PY'
import json
import os
import re
from pathlib import Path

cfg = Path(os.environ["CFG"])
policy_path = Path(os.environ["PACK"]) / ".okengine" / "effective-policy.json"
if not policy_path.is_file():
    raise SystemExit(0)
try:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit("ERROR: cannot read deploy-materialized effective policy")

lines = cfg.read_text(encoding="utf-8").splitlines(keepends=True)
mcp_start = next((i for i, line in enumerate(lines)
                  if re.match(r"^mcp_servers\s*:\s*(?:#.*)?$", line.rstrip("\n"))), None)
if mcp_start is None:
    raise SystemExit(0)
mcp_end = next((i for i in range(mcp_start + 1, len(lines))
                if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))
existing = {match.group(1) for line in lines[mcp_start + 1:mcp_end]
            if (match := re.match(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$", line.rstrip("\n")))}
blocks = []
for actor in sorted((policy.get("capabilities") or {})):
    if not actor.startswith("cron:"):
        continue
    name = "okengine-write-" + re.sub(r"[^a-z0-9]+", "-", actor[5:].lower()).strip("-")
    if name in existing:
        continue
    blocks.append(
        f"  # Engine-managed server-bound identity for {actor}.\n"
        f"  {name}:\n"
        "    command: /opt/hermes/.venv/bin/python\n"
        "    args:\n"
        "    - /opt/hermes/okengine-mcp/write_server.py\n"
        "    env:\n"
        f"      OKENGINE_WRITE_ACTOR: {actor}\n\n"
    )
if blocks:
    lines.insert(mcp_end, "".join(blocks))
    cfg.write_text("".join(lines), encoding="utf-8")
    print(f"reconciled: {cfg} (added {len(blocks)} policy-bound cron writer(s))")
PY

# okengine#257: OKENGINE_EDITING is the UI-editing switch. The reader Chat writes back to the vault
# via the okengine-write MCP in the api_server toolset, so editing OFF must DROP okengine-write
# (read-only chat: wiki Q&A still works). Unlike the baseline above (which never overwrites), this is
# an AUTHORITATIVE security control — it manages exactly the okengine-write line to match the flag.
# Default ON for back-compat (unset -> editing on). Takes effect on the next gateway recreate.
CFG="$CFG" OKENGINE_EDITING="${OKENGINE_EDITING:-}" python3 - <<'PY'
import os, re
from pathlib import Path
path = Path(os.environ["CFG"])
editing = str(os.environ.get("OKENGINE_EDITING") or "").strip().lower() not in ("0", "false", "no", "off")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
pt = next((i for i, l in enumerate(lines) if re.match(r"^platform_toolsets\s*:", l)), None)
if pt is not None:
    end = len(lines)
    for i in range(pt + 1, len(lines)):
        if lines[i].strip() and not lines[i].lstrip().startswith("#") and not lines[i].startswith((" ", "\t")):
            end = i; break
    api = next((i for i in range(pt + 1, end) if re.match(r"^[ \t]+api_server\s*:", lines[i])), None)
    if api is not None:
        api_ind = len(lines[api]) - len(lines[api].lstrip())
        lend = end
        for i in range(api + 1, end):
            l = lines[i]
            if not l.strip() or l.lstrip().startswith("#"):
                continue
            ind = len(l) - len(l.lstrip())
            if not l.lstrip().startswith("-") and ind <= api_ind:
                lend = i; break
        write_idx = next((i for i in range(api + 1, lend)
                          if re.match(r"^[ \t]*-\s*okengine-write\s*$", lines[i])), None)
        ok_idx = next((i for i in range(api + 1, lend)
                       if re.match(r"^[ \t]*-\s*okengine\s*$", lines[i])), None)
        if not editing and write_idx is not None:
            del lines[write_idx]
            path.write_text("".join(lines), encoding="utf-8")
            print(f"okengine#257: OKENGINE_EDITING=0 -> dropped okengine-write from the api_server "
                  f"toolset in {path} (UI chat is now READ-ONLY)")
        elif editing and write_idx is None and ok_idx is not None:
            lines.insert(ok_idx + 1, re.sub(r"okengine\s*$", "okengine-write\n", lines[ok_idx]))
            path.write_text("".join(lines), encoding="utf-8")
            print(f"okengine#257: OKENGINE_EDITING on -> ensured okengine-write in the api_server "
                  f"toolset in {path} (UI chat can edit)")
PY

# --- runtime version marker: stamp the ACTUAL engine/Hermes being deployed ---
# The reader's About reads this so it reports what's RUNNING, not the pack's DECLARED
# engine.version pins (which can be stale/wrong vs the deployed engine — a public pack pinned
# to an older engine still deploys on a newer one, and its hermes_pin then lies). okengine#119.
MANIFEST="$ENGINE_DIR/engine-manifest.yaml"
if [ -f "$MANIFEST" ]; then
    _rel="$(awk -F': *' '/^engine_release:/{print $2; exit}' "$MANIFEST" | awk '{print $1}')"
    _htag="$(awk -F': *' '/pinned_tag:/{print $2; exit}' "$MANIFEST" | awk '{print $1}')"
    _hsha="$(awk -F': *' '/pinned_sha:/{print $2; exit}' "$MANIFEST" | awk '{print $1}')"
    _esha="$(git -C "$ENGINE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    {
        printf 'engine_release: %s\n' "${_rel:-unknown}"
        printf 'hermes_pin: %s\n'     "${_htag:-unknown}"
        printf 'hermes_sha: %s\n'     "${_hsha:-unknown}"
        printf 'engine_sha: %s\n'     "$_esha"
    } > "$RT/engine-runtime.yaml"
    echo "stamped: $RT/engine-runtime.yaml  (engine ${_rel:-?} · Hermes ${_htag:-?})"
fi

# NB: no iwe binary is staged for the gateway anymore — backlinks-refresh builds the graph with an
# in-process link-scanner (okengine#179). iwe is now used only by the MCP, which bakes its own.

# --- MCP auth: keep the gateway's read-MCP client header in sync with the token ---
# The read server requires `Bearer <OKENGINE_MCP_TOKEN>`, falling back to the
# built-in "okengine-local" when that env var is unset. The seeded config.yaml
# ships "okengine-local"; if the operator set a real OKENGINE_MCP_TOKEN in .env,
# rewrite the header to match or every read MCP call 401s (okengine#32). Runs even
# when CFG already existed, so a token added after the first deploy is picked up.
if [ -f "$CFG" ]; then
    MTOK=""
    if [ -f "$PACK/.env" ]; then
        _t="$(grep -E '^[[:space:]]*OKENGINE_MCP_TOKEN[[:space:]]*=' "$PACK/.env" | tail -1 | cut -d= -f2- || true)"
        _t="$(printf '%s' "$_t" | sed -E 's/^[[:space:]"'\'']+//; s/[[:space:]"'\'']+$//')"
        [ -n "$_t" ] && MTOK="$_t"
    fi
    # The read server REFUSES to serve the built-in "okengine-local" default on its
    # (container) 0.0.0.0 bind — it's public/well-known — so a fresh deploy with no real
    # token leaves the mcp crash-looping. Generate a secret and persist it to .env so the
    # mcp boots out of the box; the header rewrite below matches it (okengine#105).
    if [ -z "$MTOK" ] || [ "$MTOK" = "okengine-local" ]; then
        MTOK="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
        ENVF="$PACK/.env"
        if [ -f "$ENVF" ] && grep -qE '^[[:space:]]*OKENGINE_MCP_TOKEN[[:space:]]*=' "$ENVF"; then
            MTOK="$MTOK" ENVF="$ENVF" python3 - <<'PY'
import os, re, pathlib
f = pathlib.Path(os.environ["ENVF"])
f.write_text(re.sub(r'(?m)^[ \t]*OKENGINE_MCP_TOKEN[ \t]*=.*$',
                    "OKENGINE_MCP_TOKEN=" + os.environ["MTOK"], f.read_text(encoding="utf-8")),
             encoding="utf-8")
PY
        else
            printf 'OKENGINE_MCP_TOKEN=%s\n' "$MTOK" >> "$ENVF"
        fi
        echo "  generated a secret OKENGINE_MCP_TOKEN in .env (the read MCP refuses the built-in default on its bind)"
    fi
    # Python rewrite (no sed-escaping pitfalls with token punctuation); touches
    # only the okengine read-server Authorization header. Handles both the quoted
    # (`"Bearer x"`) and unquoted (`Bearer x`) YAML forms, preserving the quoting.
    _auth_status="$(CFG="$CFG" MTOK="$MTOK" python3 - <<'PY'
import os, re, pathlib
cfg = pathlib.Path(os.environ["CFG"]); tok = os.environ["MTOK"]
t = cfg.read_text(encoding="utf-8")
new = re.sub(r'(Authorization:[ \t]*"?)Bearer [^\n"]*("?)[ \t]*$',
             lambda m: m.group(1) + "Bearer " + tok + m.group(2),
             t, flags=re.M)
if new != t:
    cfg.write_text(new, encoding="utf-8")
    print("changed")
else:
    print("nochange")
PY
)"
    if [ "$_auth_status" = "changed" ]; then
        if [ "$MTOK" = "okengine-local" ]; then
            echo "mcp auth: read-MCP header reset to Bearer okengine-local (built-in local default)"
        else
            echo "mcp auth: read-MCP header synced to OKENGINE_MCP_TOKEN from .env"
        fi
    fi
fi

# --- pack trust -> OKENGINE_TRUST in .env (okengine#90 P4a) ---
# Surface the pack's `trust` (pack.yaml) so the reader can fail-closed on a PRIVATE vault exposed
# without a password. A PUBLIC pack MUST set this so it isn't wrongly refused when exposed; the
# compose default is the fail-safe `private`. An operator-set OKENGINE_TRUST in .env is left as-is.
if [ -f "$PACK/pack.yaml" ]; then
    ENVF="$PACK/.env"
    if [ ! -f "$ENVF" ] || ! grep -qE '^[[:space:]]*OKENGINE_TRUST[[:space:]]*=' "$ENVF"; then
        PTRUST="$(PACK="$PACK" python3 <<'PY'
import os, pathlib, yaml
try:
    m = yaml.safe_load(pathlib.Path(os.environ["PACK"], "pack.yaml").read_text()) or {}
except Exception:
    m = {}
print(str(m.get("trust") or "private").strip().lower())
PY
)"
        printf 'OKENGINE_TRUST=%s\n' "$PTRUST" >> "$ENVF"
        echo "  trust: OKENGINE_TRUST=$PTRUST in .env (reader refuses a private vault exposed without a password — okengine#90 P4a)"
    fi
fi

# --- read-MCP client URL: service name on the per-pack bridge (okengine#138) ---
# The gateway shares the compose default bridge with okengine-mcp, so it dials the MCP by SERVICE
# NAME on the container port (8730) — no host port, no port_offset, no cross-pack collision.
# Normalize any pre-#138 seeded URL (http://localhost:<port>/mcp, offset or not) to that form.
if grep -qE 'url: http://localhost:[0-9]+/mcp' "$CFG" 2>/dev/null; then
    sed -i -E 's#url: http://localhost:[0-9]+/mcp#url: http://okengine-mcp:8730/mcp#' "$CFG"
    echo "  mcp url: read-MCP client pointed at okengine-mcp:8730 (service name on the bridge — okengine#138)"
fi

# --- cron-plus: the REQUIRED scheduler plugin (engine-manifest dependencies.cron-plus) ---
# The engine's whole cron fleet runs on it; without it the gateway comes up with a silently DEAD
# scheduler (jobs.json deploys fine, nothing ever fires — a live deployment shipped exactly this
# way). It is deploy-time runtime (NOT vendored, NOT baked into the gateway image): it lives at
# <pack>/.hermes-data/plugins/cron-plus (= /opt/data/plugins/cron-plus in the gateway). Install it
# here, pinned to the manifest SHA, so the documented quickstart cannot produce a dead scheduler.
CP_DIR="$RT/plugins/cron-plus"
if [ "${OKENGINE_CRON_PLUS_SKIP:-0}" = "1" ]; then
    echo "  cron-plus: install skipped (OKENGINE_CRON_PLUS_SKIP=1 — host-run hermes keeps the plugin at ~/.hermes/plugins; tests run hermetic)"
else
    # DELEGATE to install-cron-plus.sh — the single source of truth for the cron-plus install. It
    # clones/re-pins to the manifest SHA AND applies the carried patches (job-env + after-ordering +
    # the after_ordering.py overlay). This block used to clone-and-checkout ONLY, and its "present"
    # branch never re-pinned or re-patched — so a deploy that reached cron-plus through ensure-runtime
    # (rather than deploy.sh's separate install-cron-plus.sh call) ran an UNPATCHED scheduler, and
    # extension crons (which need per-job `env` for scoped MCP tokens #132/#210 and `after:` freshness
    # ordering #129) silently broke (invariant-audit HIGH #6). One installer, always patched.
    bash "$ENGINE_DIR/scripts/install-cron-plus.sh" "$PACK"
fi

# --- SOUL.md write-lock vs config migration (Hermes v0.18.0 upgrade path) ---
# Hermes protects SOUL.md read-only (444) by design — but v0.18.0's startup config migration
# (schema 30 -> 32, scripts/docker_config_migrate.py) REWRITES it and dies on the read-only bit:
# "Migration failed; restored config.yaml ... Permission denied: /opt/data/SOUL.md". The gateway
# then boots on the OLD schema and the cron scheduler silently stops ticking (live incident,
# first v0.18.0 deployment). Make it owner-writable pre-compose; harmless when already writable.
if [ -f "$RT/SOUL.md" ] && [ ! -w "$RT/SOUL.md" ]; then
    chmod u+w "$RT/SOUL.md" 2>/dev/null         && echo "  SOUL.md: made owner-writable (v0.18.0 config migration rewrites it; 444 kills the migration and the cron ticker)"         || echo "  WARN: SOUL.md is read-only and could not be unlocked — the v0.18.0 config migration will fail; chmod u+w it as its owner before compose"
fi

# --- writability: the gateway (uid HUID) must be able to write the runtime tree ---
_writable_by() {  # <dir> <uid> <gid> — true if uid/gid can write <dir>
    local d="$1" u="$2" g="$3" ou og perm
    ou="$(stat -c '%u' "$d")"; og="$(stat -c '%g' "$d")"; perm="$(stat -c '%A' "$d")"
    { [ "$ou" = "$u" ] && [ "${perm:2:1}" = "w" ]; } && return 0   # owner-write
    { [ "$og" = "$g" ] && [ "${perm:5:1}" = "w" ]; } && return 0   # group-write
    [ "${perm:8:1}" = "w" ] && return 0                            # other-write
    return 1
}

if _writable_by "$RT" "$HUID" "$HGID"; then
    : # the container uid can write the runtime — good
elif [ "$FIX_PERMS" = "1" ]; then
    chmod -R a+rwX "$PACK"
    echo "fix-perms: made $PACK group/other-writable so uid $HUID can write it"
    echo "  (local-deploy convenience — the tree is now world-writable; for a shared host"
    echo "   prefer running the whole stack as your own uid:"
    echo "   export HERMES_UID=\$(id -u) HERMES_GID=\$(id -g) — avoids both world-write and the"
    echo "   chown-vs-deploy.sh conflict, see okengine#33)"
else
    cat >&2 <<MSG
ERROR: the gateway runs as uid $HUID:$HGID, but $PACK is owned by $(id -un) (uid $(id -u))
       and is not writable by $HUID — the container cannot create /opt/data/logs and
       will fail to start (it may still show as 'Up'). Fix one of:
  - export HERMES_UID=\$(id -u) HERMES_GID=\$(id -g)  (recommended: run the whole stack as
                                                       yourself. deploy.sh writes the tree as you
                                                       and the gateway remaps to your uid — no
                                                       chown, no conflict)
  - re-run with --fix-perms                          (quick local: chmod -R a+rwX \$PACK — the
                                                       tree becomes world-writable)
  - sudo chown -R $HUID:$HGID "$PACK"                 (ONLY if you ALSO run deploy.sh as uid $HUID:
                                                       chowning to $HUID while you deploy as
                                                       $(id -un) breaks deploy.sh, which must write
                                                       the tree — okengine#33)
MSG
    exit 1
fi
