#!/usr/bin/env python3
"""deployment_validate.py — daily in-gateway deployment self-validation. no_agent.

The gap it closes: pack-side validators (`framework validate`, validate_merged) run at
authoring/deploy time on the HOST — nothing re-checked the LIVE deployment as it mutated
(pin bumps, co-installs, rule merges). Two real drifts shipped that way in one week: a
stale engine.version pin, and a type_alias shadowing a co-installed pack's type.

Checks (deterministic, against the deployed/staged state the gateway actually runs):
  1. version pins    engine.version (vault) vs the runtime stamp (/opt/data/engine-runtime.yaml)
  2. schema contract composed via the STAGED schema_lib: parses; type_aliases never shadow
                     canonical types; alias targets exist; partitioning namespaces exist
  3. sub-domains     every wiki/**/schema.yaml parses and declares types
  4. cron fleet      deployed jobs.json parses; script refs exist; no duplicate ids/names
  5. rules files     config/*rules*.yaml parse; rule ids unique
  6. extensions      .okengine/extensions.yaml parses; enabled extensions have staged scripts
  7. auth posture    trust: private + non-loopback bind -> password must be set; the Agent Chat
                     (api_server) toolset lockdown; and, when OKENGINE_HARDENED=1, the full
                     fail-closed safe profile (real MCP token, reader auth-or-public, rate limits
                     on, exports off if public) — okengine#78

Writes wiki/operational/deployment-validation.md. EXITS 1 when any FAIL exists — the lane
shows ERRORED in fleet health, which is the attention mechanism (a failed validation that
scrolls by silently is worse than none).

Env: WIKI_PATH (/opt/vault) · OKENGINE_DATA (/opt/data)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Sibling cron lib (staged into DATA/scripts alongside this script; scripts/cron/ in the repo).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import deployment_checks as C  # noqa: E402  (the shared checks library, okengine#405)


# ── backward-compat facade (okengine#405) ─────────────────────────────────────────────────────────
# okpacks-library/scripts/bundle-compose-check.sh loads THIS module, sets `.VAULT`/`.DATA`, and calls
# `.check_schema()`/`.check_partition_dups()` over `.F`. okengine#405 moved the checks into
# deployment_checks; re-export that historical surface — delegating with the caller-set VAULT/DATA —
# so the cross-repo parity contract keeps working. New code imports deployment_checks directly.
VAULT, DATA, HERMES = C.VAULT, C.DATA, C.HERMES
F = C.F                                       # the SAME findings list the checks append to


def check_schema():
    C.VAULT, C.DATA, C.HERMES = VAULT, DATA, HERMES
    return C.check_schema()


def check_partition_dups():
    C.VAULT, C.DATA, C.HERMES = VAULT, DATA, HERMES
    return C.check_partition_dups()


def main() -> int:
    F = C.run()                                   # shared checks library (okengine#405)
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    F.sort(key=lambda x: (order[x[0]], x[1]))
    fails = sum(1 for l, _, _ in F if l == "FAIL")
    warns = sum(1 for l, _, _ in F if l == "WARN")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Deployment validation"', f"updated: {now}", "---",
         "", f"# Deployment validation — {now}", "",
         f"_Daily in-gateway self-check of the LIVE deployment (pins, composed schema, "
         f"sub-domains, cron fleet, rules, extensions, auth). A FAIL marks this lane ERRORED "
         f"in fleet health on purpose._", "",
         f"**{'FAIL' if fails else 'PASS'}** — {fails} fail · {warns} warn", ""]
    if F:
        L += ["| Level | Area | Finding |", "|---|---|---|"]
        L += [f"| {l} | {a} | {m} |" for l, a, m in F]
    L.append("")
    out = C.VAULT / "wiki" / "operational" / "deployment-validation.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(L), encoding="utf-8")
        dest = "-> wiki/operational/deployment-validation.md"
    except OSError as e:
        # The report file (or its dir) is foreign-owned (root, from a bare `docker exec` write), so
        # the lane uid can't overwrite it — the exact uid-desync condition check_ownership exists to
        # catch. A raw PermissionError here would crash ON THE VALIDATOR'S OWN OUTPUT, swallowing the
        # FAIL diagnosis it just computed (which names that very file). Fail loud with the remedy but
        # STILL print the findings below, so the diagnosis is never lost (okengine#178 peer pattern).
        print(f"deployment-validate: ERROR cannot write {out}: {e} — likely a foreign-owned (root) "
              "report file. Repair: scripts/fix-vault-ownership.sh <deployment-dir>", file=sys.stderr)
        dest = "(report unwritable — see stderr)"
    for l, a, m in F:
        print(f"  {l:<4} [{a}] {m}")
    print(f"deployment-validate: {'FAIL' if fails else 'PASS'} ({fails} fail, {warns} warn) {dest}")
    print(json.dumps({"wakeAgent": False}))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
