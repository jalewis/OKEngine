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
# host-only secrets file: owner-only from birth, tightened if inherited (okengine#665)
[ -f "$ENVF" ] || ( umask 077; : > "$ENVF" )
chmod 600 "$ENVF" 2>/dev/null || echo "WARN: could not chmod 600 $ENVF (not the owner?) — secrets may be readable by others" >&2
# Append one `KEY=value` line to a .env, first ending an unterminated last line. Without that, a
# .env whose last line had no newline glued the new assignment onto it, so the token on that line
# gained a `HERMES_UID=<uid>` suffix: the token was corrupted and the pin lost.
_env_append() {   # $1=file  $2=line
    if [ -s "$1" ] && [ -n "$(tail -c 1 "$1")" ]; then printf '\n' >> "$1"; fi
    printf '%s\n' "$2" >> "$1"
}
grep -qE '^[[:space:]]*HERMES_UID[[:space:]]*=' "$ENVF" || _env_append "$ENVF" "HERMES_UID=$HUID"
grep -qE '^[[:space:]]*HERMES_GID[[:space:]]*=' "$ENVF" || _env_append "$ENVF" "HERMES_GID=$HGID"

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
    "    - okengine-write-chat\n"
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
            "    - okengine-write-chat\n"
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
existing = next((i for i in range(mcp_start + 1, mcp_end)
                 if lines[i].rstrip() == key), None)
if existing is not None:
    block_end = next((i for i in range(existing + 1, mcp_end)
                      if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= 2),
                     mcp_end)
    if not any("OKENGINE_OUTPUT_CONTRACT_MODE:" in line
               for line in lines[existing:block_end]):
        actor_line = next((i for i in range(existing, block_end)
                           if "OKENGINE_WRITE_ACTOR:" in lines[i]), None)
        if actor_line is not None:
            lines.insert(actor_line + 1, "      OKENGINE_OUTPUT_CONTRACT_MODE: enforce\n")
            path.write_text("".join(lines), encoding="utf-8")
            print(f"reconciled: {path} (enforced source-quality output contract)")
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
    "    tools:\n"
    "      resources: false\n"
    "      prompts: false\n"
    "    env:\n"
    "      OKENGINE_WRITE_ACTOR: cron:source-quality-backfill\n"
    "      OKENGINE_CRON_JOBS: /opt/data/cron-plus/jobs.json\n"
    "      OKENGINE_OUTPUT_CONTRACT_MODE: enforce\n\n"
)
lines.insert(mcp_end, block)
path.write_text("".join(lines), encoding="utf-8")
print(f"reconciled: {path} (added server-bound source-quality writer)")
PY

# okengine#983: the Agent Chat (api_server) writes through its OWN actor-bound server, never the
# generic actor-less okengine-write (which the write path treats as admin). Register it on every
# deployment, under the fixed name cron_pack_split reserves; an existing entry is never rewritten.
CFG="$CFG" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["CFG"])
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
mcp_start = next((i for i, line in enumerate(lines)
                  if re.match(r"^mcp_servers\s*:\s*(?:#.*)?$", line.rstrip("\n"))), None)
if mcp_start is None:
    raise SystemExit(0)
mcp_end = next((i for i in range(mcp_start + 1, len(lines))
                if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))
if any(re.match(r"^  okengine-write-chat:\s*(?:#.*)?$", lines[i].rstrip("\n"))
       for i in range(mcp_start + 1, mcp_end)):
    raise SystemExit(0)
lines.insert(mcp_end, (
    "  # Engine-managed server-bound identity for the Agent Chat (okengine#983).\n"
    "  okengine-write-chat:\n"
    "    command: /opt/hermes/.venv/bin/python\n"
    "    args:\n"
    "    - /opt/hermes/okengine-mcp/write_server.py\n"
    "    tools:\n"
    "      resources: false\n"
    "      prompts: false\n"
    "    env:\n"
    "      OKENGINE_WRITE_ACTOR: chat:api_server\n"
    "      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml\n"
    "      OKENGINE_WRITE_TOOLS: create_entity,update_entity,append_to_section\n\n"
))
path.write_text("".join(lines), encoding="utf-8")
print(f"reconciled: {path} (added the actor-bound Agent Chat writer okengine-write-chat)")
PY

# Register one dedicated writer per lane of THIS deployment, and remove the engine-generated
# writers of lanes it does not run (okengine#1029). The lane set is this pack's own fleet, built in
# memory from its crons + enabled extensions, plus its deploy-materialized policy's cron:
# capabilities. It is NEVER read from the checkout's shared config/cron-plus-jobs.json: that file
# holds whichever pack was split last in this checkout, so every host gained the previous host's
# lanes. Baseline writers and operator-added servers are never removed; see lane_writers.py.
# $ENGINE_DIR/src first, so the pack-name reader resolves this checkout's okengine package.
PYTHONPATH="$ENGINE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$ENGINE_DIR/scripts/lane_writers.py" --pack "$PACK" --config "$CFG"

# Hermes intentionally gives stdio MCP servers only their declared `env:` mapping. Image-level
# ENV therefore reaches the gateway shell but not these child processes. Every governed writer
# imports policy_plane and must receive the baked catalog path explicitly; otherwise an installed-
# wheel import derives the nonexistent site-packages/config/policy/catalog.yaml path (#544).
# Reconcile old generated entries as well as the baseline writer, without replacing any operator
# value that is already explicit.
CFG="$CFG" python3 - <<'PY'
import os
import re
from pathlib import Path

import yaml

path = Path(os.environ["CFG"])
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
starts = [i for i, line in enumerate(lines)
          if re.match(r"^  okengine-write(?:-[A-Za-z0-9_.-]+)?:\s*(?:#.*)?$",
                      line.rstrip("\n"))]
changed = 0
for start in reversed(starts):
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip()
                and len(lines[i]) - len(lines[i].lstrip()) <= 2), len(lines))
    block = lines[start:end]
    policy_line = next((i for i in range(start + 1, end)
                        if re.match(r"^      OKENGINE_POLICY_CATALOG:\s*", lines[i])), None)
    if policy_line is not None:
        expected = "      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml\n"
        if lines[policy_line] != expected:
            lines[policy_line] = expected
            changed += 1
        continue
    env_line = next((i for i in range(start + 1, end)
                     if re.match(r"^    env:\s*(?:#.*)?$", lines[i].rstrip("\n"))), None)
    if env_line is None:
        inline_env = next((i for i in range(start + 1, end)
                           if re.match(r"^    env:\s*\S", lines[i].rstrip("\n"))), None)
        if inline_env is None:
            lines[end:end] = [
                "    env:\n",
                "      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml\n",
            ]
        else:
            raw = lines[inline_env].split(":", 1)[1].strip()
            try:
                values = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise SystemExit(f"ERROR: invalid inline env for {lines[start].strip()}: {exc}")
            if not isinstance(values, dict) or not all(
                    isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                    and (value is None or isinstance(value, (str, int, float, bool)))
                    for key, value in values.items()):
                raise SystemExit(f"ERROR: inline env for {lines[start].strip()} must be a scalar mapping")
            rendered = ["    env:\n"]
            rendered.extend(
                f"      {key}: {yaml.safe_dump(value, default_flow_style=True).splitlines()[0]}\n"
                for key, value in values.items()
            )
            rendered.append(
                "      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml\n")
            lines[inline_env:inline_env + 1] = rendered
    else:
        lines.insert(env_line + 1,
                     "      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml\n")
    changed += 1
if changed:
    path.write_text("".join(lines), encoding="utf-8")
    print(f"reconciled: {path} (bound policy catalog into {changed} stdio writer env(s))")
PY

# okengine#1027: the same filtered environment means the gateway's OKENGINE_PACK never reaches a
# writer. write_server derives its pack from the vault's pack.yaml and refuses to start when an env
# value disagrees, so every writer's env: map states the deployment pack explicitly, and a stale
# value from a renamed pack is corrected rather than left to stop the writer. A pack.yaml without a
# name is left for the write-principal check to FAIL; nothing is guessed here.
CFG="$CFG" PACK="$PACK" python3 - <<'PY'
import os
import re
from pathlib import Path

import yaml

path = Path(os.environ["CFG"])
try:
    meta = yaml.safe_load((Path(os.environ["PACK"]) / "pack.yaml").read_text(encoding="utf-8"))
except (OSError, yaml.YAMLError):
    meta = None
name = meta.get("name") if isinstance(meta, dict) else None
name = name.strip() if isinstance(name, str) else ""
if not name:
    raise SystemExit(0)
value = name if re.fullmatch(r"[A-Za-z0-9_.-]+", name) else yaml.safe_dump(
    name, default_style='"').strip()
expected = f"      OKENGINE_PACK: {value}\n"
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
starts = [i for i, line in enumerate(lines)
          if re.match(r"^  okengine-write(?:-[A-Za-z0-9_.-]+)?:\s*(?:#.*)?$", line.rstrip("\n"))]
changed = 0
for start in reversed(starts):
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip()
                and len(lines[i]) - len(lines[i].lstrip()) <= 2), len(lines))
    env_line = next((i for i in range(start + 1, end)
                     if re.match(r"^    env:\s*(?:#.*)?$", lines[i].rstrip("\n"))), None)
    if env_line is None:
        continue
    pack_line = next((i for i in range(env_line + 1, end)
                      if re.match(r"^      OKENGINE_PACK:", lines[i])), None)
    if pack_line is None:
        lines.insert(env_line + 1, expected)
    elif yaml.safe_load(lines[pack_line].split(":", 1)[1]) != name:
        lines[pack_line] = expected
    else:
        continue
    changed += 1
if changed:
    path.write_text("".join(lines), encoding="utf-8")
    print(f"reconciled: {path} (bound pack {name} into {changed} writer env(s))")
PY

# okengine#1042 [36]: the same filtered environment drops TZ (Hermes passes a stdio server only
# PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR, XDG_* and its own `env:` map). A stdio
# writer then dated its wiki/log.md ledger lines in UTC, while the in-process writers,
# review_reconcile and rotate_wiki_log use the deployment's local day: one ledger, two
# conventions. Every writer's
# `env:` map states the zone the gateway gets from compose (`TZ=${TZ:-UTC}`: the invoking
# environment, then the pack .env, then UTC; dotenv_lib.compose_default), and a stale value is
# corrected.
CFG="$CFG" ENVF="$ENVF" ENGINE_DIR="$ENGINE_DIR" python3 - <<'PY'
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(os.environ["ENGINE_DIR"]) / "scripts" / "cron"))
from dotenv_lib import compose_default

path = Path(os.environ["CFG"])
zone = compose_default("TZ", os.environ, Path(os.environ["ENVF"]), "UTC").strip() or "UTC"
quoted = yaml.safe_dump(zone, default_style='"').strip()
expected = f"      TZ: {quoted}\n"
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
starts = [i for i, line in enumerate(lines)
          if re.match(r"^  okengine-write(?:-[A-Za-z0-9_.-]+)?:\s*(?:#.*)?$", line.rstrip("\n"))]
changed = 0
for start in reversed(starts):
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip()
                and len(lines[i]) - len(lines[i].lstrip()) <= 2), len(lines))
    env_line = next((i for i in range(start + 1, end)
                     if re.match(r"^    env:\s*(?:#.*)?$", lines[i].rstrip("\n"))), None)
    if env_line is None:
        continue
    tz_line = next((i for i in range(env_line + 1, end) if re.match(r"^      TZ:", lines[i])), None)
    held = None if tz_line is None else str(yaml.safe_load(lines[tz_line].split(":", 1)[1]) or "")
    if zone == "UTC":
        # A writer with no TZ already runs in UTC: leave a UTC deployment's config as seeded, and
        # drop only a stale zone left from an earlier one.
        if held is None or held.upper() in ("", "UTC"):
            continue
        del lines[tz_line]
    elif held is None:
        lines.insert(env_line + 1, expected)
    elif held != zone:
        lines[tz_line] = expected
    else:
        continue
    changed += 1
if changed:
    path.write_text("".join(lines), encoding="utf-8")
    print(f"reconciled: {path} (bound timezone {zone} into {changed} writer env(s))"
          if zone != "UTC" else
          f"reconciled: {path} (dropped a stale timezone from {changed} writer env(s); UTC)")
PY

# Actor-scoped write servers are tools-only. Hiding MCP resource/prompt utility
# wrappers removes an unrelated action surface that local models repeatedly
# explored instead of completing their bounded write/receipt task.
CFG="$CFG" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["CFG"])
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
starts = [i for i, line in enumerate(lines)
          if re.match(r"^  okengine-write-[A-Za-z0-9_.-]+:\s*(?:#.*)?$", line.rstrip("\n"))]
changed = 0
for start in reversed(starts):
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip()
                and len(lines[i]) - len(lines[i].lstrip()) <= 2), len(lines))
    block = lines[start:end]
    if not any("OKENGINE_WRITE_ACTOR:" in line for line in block):
        continue
    if any(re.match(r"^    tools:\s*(?:#.*)?$", line.rstrip("\n")) for line in block):
        continue
    env_line = next((i for i in range(start + 1, end)
                     if re.match(r"^    env:\s*(?:#.*)?$", lines[i].rstrip("\n"))), None)
    if env_line is None:
        continue
    lines[env_line:env_line] = [
        "    tools:\n",
        "      resources: false\n",
        "      prompts: false\n",
    ]
    changed += 1
if changed:
    path.write_text("".join(lines), encoding="utf-8")
    print(f"reconciled: {path} (disabled MCP resource/prompt utilities on {changed} actor writer(s))")
PY

# Domain contracts live in the deploy-generated cron store rather than the
# engine-only image catalog. Bind all CRON actor writers to the live store, including
# entries created by an older deployment that predate this setting. A non-cron actor (the
# `chat:` writer, okengine#983) has no lane contract and is governed by its catalog capability.
CFG="$CFG" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["CFG"])
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
starts = [i for i, line in enumerate(lines)
          if re.match(r"^  okengine-write-[A-Za-z0-9_.-]+:\s*(?:#.*)?$", line.rstrip("\n"))]
changed = 0
for start in reversed(starts):
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip()
                and len(lines[i]) - len(lines[i].lstrip()) <= 2), len(lines))
    block = lines[start:end]
    actor_line = next((i for i in range(start + 1, end)
                       if "OKENGINE_WRITE_ACTOR:" in lines[i]), None)
    if actor_line is None or any("OKENGINE_CRON_JOBS:" in line for line in block):
        continue
    if not re.search(r"OKENGINE_WRITE_ACTOR:\s*['\"]?cron:", lines[actor_line]):
        continue
    lines.insert(actor_line + 1, "      OKENGINE_CRON_JOBS: /opt/data/cron-plus/jobs.json\n")
    changed += 1
if changed:
    path.write_text("".join(lines), encoding="utf-8")
    print(f"reconciled: {path} (bound {changed} actor writer(s) to live cron contracts)")
PY

# okengine#257: OKENGINE_EDITING is the UI-editing switch. The reader Chat writes back to the vault
# via the okengine-write-chat MCP in the api_server toolset, so editing OFF must DROP it (read-only
# chat: wiki Q&A still works). Unlike the baseline above (which never overwrites), this is an
# AUTHORITATIVE security control: it manages exactly the chat writer to match the flag.
# okengine#983: it also ALWAYS removes the generic, actor-less okengine-write from api_server (the
# write path treats it as admin). A pre-#983 config that listed it is migrated to the chat writer
# when editing is on, or simply loses it when editing is off. Block lists (items may carry trailing
# comments) and flow lists (single- or multi-line) are handled, and the parsed result is verified:
# any spelling the edit could not reach FAILS the deploy rather than leaving the admin writer bound.
# Default ON for back-compat (unset -> editing on). Takes effect on the next gateway recreate.
# okengine#991: the switch is read from the pack .env FIRST. The gateway gets it from that file
# (compose env_file) and the reader from the same file (compose interpolation, skeleton compose),
# and nothing on the deploy path exports it into this process, so a process-only read left the
# writer bound while the gateway reported editing off. A process value applies only when .env does not set the key
# (back-compat for callers that export it), with a warning that the containers will not see it; one
# that disagrees with .env is ignored, loudly, because following it would split them the other way.
# Captured before the command line that re-assigns OKENGINE_EDITING (an expansion on that line
# would not see its own prefix assignment: shellcheck SC2097/SC2098).
_editing_set="${OKENGINE_EDITING+set}"
_editing_value="${OKENGINE_EDITING:-}"
CFG="$CFG" ENVF="$ENVF" ENGINE_DIR="$ENGINE_DIR" OKENGINE_EDITING_SET="$_editing_set" \
    OKENGINE_EDITING="$_editing_value" python3 - <<'PY'
import os, re, sys
from pathlib import Path
import yaml
path = Path(os.environ["CFG"])
OFF = ("0", "false", "no", "off")
# The ONE .env reader, shared with framework validate and the in-gateway checks: it parses the file
# the way compose env_file does (comments, `export`, quotes, CRLF), so the switch decided here is
# the value the containers get. A second, simpler parser here read `OKENGINE_EDITING=0 # note` as
# editing ON (okengine#991).
sys.path.insert(0, str(Path(os.environ["ENGINE_DIR"]) / "scripts" / "cron"))
from dotenv_lib import read_compose_env


def _on(value):
    return str(value or "").strip().lower() not in OFF


process_value = os.environ.get("OKENGINE_EDITING") if os.environ.get("OKENGINE_EDITING_SET") else None
# A bare `OKENGINE_EDITING` line inherits the invoking environment, as compose does.
lookup = {k: v for k, v in os.environ.items() if k != "OKENGINE_EDITING"}
if process_value is not None:
    lookup["OKENGINE_EDITING"] = process_value
# The pack .env is both compose's project .env and the gateway's env_file, so `${X}` resolves from
# the process, then the whole file, as compose does (okengine#993 R1).
file_value = read_compose_env(Path(os.environ["ENVF"]), lookup).get("OKENGINE_EDITING")
if file_value is not None:
    editing = _on(file_value)
    if process_value is not None and _on(process_value) != editing:
        print(f"WARN: OKENGINE_EDITING={process_value!r} in the environment is ignored: the pack .env "
              f"sets {file_value!r}, and the containers take it from .env.", file=sys.stderr)
else:
    editing = _on(process_value)
    if process_value is not None:
        print(f"WARN: OKENGINE_EDITING={process_value!r} comes from the environment, not in the pack "
              ".env; the containers take it from .env, so set it there to make them agree.",
              file=sys.stderr)
CHAT, ACTORLESS = "okengine-write-chat", "okengine-write"
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
pt = next((i for i, l in enumerate(lines) if re.match(r"^platform_toolsets\s*:", l)), None)
api = None
end = len(lines)
if pt is not None:
    for i in range(pt + 1, len(lines)):
        if lines[i].strip() and not lines[i].lstrip().startswith("#") and not lines[i].startswith((" ", "\t")):
            end = i; break
    api = next((i for i in range(pt + 1, end) if re.match(r"^[ \t]+api_server\s*:", lines[i])), None)

ITEM = r"['\"]?([A-Za-z0-9_.-]+)['\"]?"


def flow_items(text):
    return [x.strip().strip("'\"") for x in text.split(",") if x.strip()]


changed = False
if api is not None:
    head = lines[api]
    opening = re.match(r"^([ \t]+api_server\s*:\s*)\[(.*)$", head.rstrip("\n"))
    if opening:
        # A flow list, possibly over several lines: gather it up to its closing bracket.
        stop = next((i for i in range(api, end) if "]" in lines[i].split("#", 1)[0]), None)
        if stop is None:
            sys.exit(f"ERROR: {path}: unterminated platform_toolsets.api_server flow list; "
                     "fix it by hand (okengine#983)")
        # Comment-aware: strip every `# ...` before matching, so a bracket inside the trailing
        # comment (`[a, b]  # [x]`) can never be taken for the list's closing bracket.
        flow = " ".join(line.split("#", 1)[0].rstrip("\n") for line in lines[api:stop + 1])
        comment = lines[stop].rstrip("\n").partition("#")
        tail = f"  #{comment[2]}" if comment[1] else ""
        inline = re.match(r"^([ \t]+api_server\s*:\s*)\[([^\]]*)\]\s*$", flow.rstrip())
        if inline is None:
            sys.exit(f"ERROR: {path}: platform_toolsets.api_server flow list is not a plain "
                     "`[a, b]` list; fix it by hand (okengine#983)")
        items = flow_items(inline.group(2))
        # An operator-formatted list: only migrate the actor-less writer out of it.
        if ACTORLESS in items or (not editing and CHAT in items):
            new = []
            for x in items:
                if x == ACTORLESS:
                    x = CHAT if editing and CHAT not in items else None
                elif x == CHAT and not editing:
                    x = None
                if x and x not in new:
                    new.append(x)
            lines[api:stop + 1] = [f"{inline.group(1)}[{', '.join(new)}]{tail}\n"]
            changed = True
    else:
        api_ind = len(head) - len(head.lstrip())
        lend = end
        for i in range(api + 1, end):
            l = lines[i]
            if not l.strip() or l.lstrip().startswith("#"):
                continue
            ind = len(l) - len(l.lstrip())
            if not l.lstrip().startswith("-") and ind <= api_ind:
                lend = i; break

        def item(name):
            # An item may carry a trailing comment: `- okengine-write   # note`.
            return next((i for i in range(api + 1, lend)
                         if (m := re.match(rf"^[ \t]*-\s*{ITEM}\s*(?:#.*)?$", lines[i].rstrip("\n")))
                         and m.group(1) == name), None)

        actorless, chat, ok = item(ACTORLESS), item(CHAT), item("okengine")
        if actorless is not None:
            if editing and chat is None:
                lines[actorless] = re.sub(rf"(-\s*['\"]?){re.escape(ACTORLESS)}(?![A-Za-z0-9_.-])",
                                          rf"\g<1>{CHAT}", lines[actorless], count=1)
                chat = actorless
            else:
                del lines[actorless]
            changed = True
        elif editing and chat is None and ok is not None:
            indent = re.match(r"^([ \t]*-\s*)", lines[ok]).group(1)
            lines.insert(ok + 1, f"{indent}{CHAT}\n")
            changed = True
        if not editing and chat is not None:
            del lines[item(CHAT)]
            changed = True
if changed:
    path.write_text("".join(lines), encoding="utf-8")
    state = ("UI chat can edit, through the actor-bound okengine-write-chat" if editing
             else "UI chat is now READ-ONLY")
    print(f"okengine#257/#983: reconciled the api_server toolset in {path} ({state}; the "
          "actor-less okengine-write is never bound)")

# Verify the RESULT, whatever spelling the operator used: the line edits above are only trusted
# when the parsed file agrees. Anything they could not reach fails the deploy loudly.
try:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except yaml.YAMLError as exc:
    sys.exit(f"ERROR: {path} does not parse after the api_server reconcile: {exc}")
toolsets = parsed.get("platform_toolsets") if isinstance(parsed, dict) else None
listed = toolsets.get("api_server") if isinstance(toolsets, dict) else None
if isinstance(listed, list):
    names = [str(x).strip() for x in listed]
    if ACTORLESS in names:
        sys.exit(f"ERROR: {path}: platform_toolsets.api_server still binds the actor-less "
                 f"{ACTORLESS} (an unrecognized list spelling); replace it with {CHAT} by hand "
                 "(okengine#983)")
    if not editing and CHAT in names:
        sys.exit(f"ERROR: {path}: OKENGINE_EDITING is off but platform_toolsets.api_server still "
                 f"lists {CHAT}; remove it by hand (okengine#257)")
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

# No IWE binary is staged anywhere. Backlinks-refresh builds the graph with the bounded in-process
# scanner and all serving paths consume its artifact.

# --- MCP auth: keep the gateway's read-MCP client header in sync with the token ---
# The read server requires `Bearer <OKENGINE_MCP_TOKEN>`, falling back to the
# built-in "okengine-local" when that env var is unset. The seeded config.yaml
# ships "okengine-local"; if the operator set a real OKENGINE_MCP_TOKEN in .env,
# rewrite the header to match or every read MCP call 401s (okengine#32). Runs even
# when CFG already existed, so a token added after the first deploy is picked up.
if [ -f "$CFG" ]; then
    # okengine#994 [17]: read the token the way compose hands it to the MCP containers (the ONE
    # dotenv parser: last assignment wins, inline comments, quotes and `export`). A grep/cut read
    # took `OKENGINE_MCP_TOKEN=abc # rotated` as "abc # rotated", so the header carried a token the
    # MCP server never saw and every read-MCP call 401'd.
    MTOK="$(python3 "$ENGINE_DIR/scripts/cron/dotenv_lib.py" get "$PACK/.env" OKENGINE_MCP_TOKEN || true)"
    # The read server REFUSES to serve the built-in "okengine-local" default on its
    # (container) 0.0.0.0 bind — it's public/well-known — so a fresh deploy with no real
    # token leaves the mcp crash-looping. Generate a secret and persist it to .env so the
    # mcp boots out of the box; the header rewrite below matches it (okengine#105).
    if [ -z "$MTOK" ] || [ "$MTOK" = "okengine-local" ]; then
        MTOK="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
        # Every assignment of the key is rewritten, in any spelling compose reads (`export KEY=`,
        # `KEY: v`), or the new line is appended; then the file is read back through the same parser
        # and the deploy FAILS unless compose would now hand the containers the new token. A rewrite
        # that missed an `export` line left that line's old value winning (last assignment wins).
        MTOK="$MTOK" ENVF="$PACK/.env" ENGINE_DIR="$ENGINE_DIR" python3 - <<'PY'
import os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["ENGINE_DIR"]) / "scripts" / "cron"))
from dotenv_lib import read_compose_env

f, tok = Path(os.environ["ENVF"]), os.environ["MTOK"]
text = f.read_text(encoding="utf-8") if f.is_file() else ""
text, n = re.subn(r"(?m)^[ \t]*(?:export[ \t]+)?OKENGINE_MCP_TOKEN[ \t]*[=:].*$",
                  lambda _m: "OKENGINE_MCP_TOKEN=" + tok, text)
if not n:
    text += ("" if not text or text.endswith("\n") else "\n") + "OKENGINE_MCP_TOKEN=" + tok + "\n"
f.write_text(text, encoding="utf-8")
if read_compose_env(f, os.environ).get("OKENGINE_MCP_TOKEN") != tok:
    sys.exit(f"ERROR: wrote OKENGINE_MCP_TOKEN to {f}, but compose would not hand the containers "
             "that value (a multi-line or bare assignment of the key?); fix the .env by hand")
PY
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
        _env_append "$ENVF" "OKENGINE_TRUST=$PTRUST"
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
    # Only the trees the CONTAINER writes are opened (okengine#665): the runtime dir, the corpus,
    # raw captures and engine state. Pack sources (pack.yaml, schema.yaml, crons/) stay as they
    # are, and .env is host-only so it never opens. `chmod -R a+rwX "$PACK"` used to make .env and
    # the Bearer token in config.yaml world-readable and say only "world-writable".
    for _t in "$RT" "$PACK/wiki" "$PACK/raw" "$PACK/.okengine"; do
        [ -e "$_t" ] && chmod -R a+rwX "$_t"
    done
    chmod 600 "$ENVF" 2>/dev/null || true
    echo "fix-perms: made $RT, wiki/, raw/ and .okengine/ group/other-writable so uid $HUID can write them"
    echo "  WARNING: $CFG carries the read-MCP Bearer token and is now world-readable, because uid"
    echo "           $HUID must read it and is not its owner. .env stays 0600 (host-only)."
    echo "  (local-deploy convenience only; for a shared host prefer running the whole stack as"
    echo "   your own uid: export HERMES_UID=\$(id -u) HERMES_GID=\$(id -g) — avoids world-write,"
    echo "   the world-readable token and the chown-vs-deploy.sh conflict, see okengine#33)"
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
