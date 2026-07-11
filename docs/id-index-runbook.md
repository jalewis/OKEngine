# Runbook — the MCP write-path id-index

The **id-index** is an `id → wiki-relative-path` map the enforced MCP write path uses to dedup on
create: `okengine-write/create_entity` → `write_server._create` → `_dedup_on_create` → `_registry()`
→ `id_index`. It's how a cosmetic duplicate of an entity routes to the existing canonical instead of
minting a second one (okengine#98/#99/#100).

This runbook covers the one operational failure mode it has and how to keep it healthy.

## Symptom → reach for this runbook

- `okengine-write/create_entity` (and sometimes `okengine/search`) **time out at exactly 300.0s**
  (`MCP call timed out after 300.0s`) in the agent log, **clustered right after a gateway/MCP restart**.
- Fleet-health (`fleet_health.py`) shows agent lanes 🔴 **ERRORED** with an MCP tool timeout in the
  log tail, on a **large vault** (tens of thousands of pages).

## Root cause

`_registry()` is a lazy singleton; on a cold process it called `id_index.build(vault)`, which
**`rglob`s every `*.md` in `wiki/` and reads + YAML-parses each one**. On a 60k+ page vault that is
tens of seconds (measured ~42s at 64k pages, worse when the files are cold in the page cache). It ran
**synchronously in the write server's event loop on the first write after every restart**, so
concurrent tool calls queued behind it and blew the 300s client timeout. Every gateway recreate
(deploys) re-armed it.

## How it works now (the fix)

`id_index.build(vault, force=False)` — the write-path call site, unchanged — **loads a persisted
artifact instead of scanning**, and kicks a one-shot background refresh:

- **`force=False`** (write path): load `/opt/data/state/id-index.json` (~milliseconds) → return
  immediately → a daemon thread full-scans, updates the live index **in place**, and re-persists.
  Falls back to a live scan only when no artifact exists yet (first deploy).
- **`force=True`** (the `id-index-refresh` engine cron, every 6h): full-scan + persist. Keeps the
  artifact warm for the next restart.
- Write-synchronous updates (`_registry().by_id[id] = path` on each create) keep this process's own
  writes tracked between refreshes.

**Freshness bound:** an id created by a *direct* (non-MCP) file writer isn't in the index for dedup
until the next refresh (cron, or the per-restart background scan). The MCP — the enforced write path —
tracks its own writes live, and `_dedup_on_create` re-checks existence on disk, so a stale entry is
harmless.

## The deploy fact that bites (READ THIS)

`write_server` runs **in the gateway** (it needs RW vault; the read-MCP container is vault-RO) and
imports id_index from **`/opt/hermes/scripts/cron/id_index.py` — the BAKED image copy**:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "cron"))
import id_index
```

So a change to `scripts/cron/id_index.py` (or the other libs write_server imports: `id_lib`,
`schema_lib`, `converge`) **does NOT go live by `deploy-cron-scripts.sh`** — that stages to
`/opt/data/scripts` + `<vault>/.hermes-data/scripts`, which feed the **cron**, not the write server.
It needs the **gateway image**. See [`engine-domain-boundary.md`](engine-domain-boundary.md) deploy
surfaces and [`sharded-scan-discipline.md`](sharded-scan-discipline.md) for the rglob convention.

## Operations

All commands run from the engine repo root; `$D` = a deployment dir; `$GW` = its gateway container.

**Rebuild the artifact now** (creates/refreshes `/opt/data/state/id-index.json`):

```bash
ID=$(python3 -c "import json;print(next(j['id'] for j in json.load(open('$D/.hermes-data/cron-plus/jobs.json'))['jobs'] if j.get('name')=='id-index-refresh'))")
CRON_PACK_DIR="$D" bash scripts/cron-plus.sh run "$ID"     # ~one full scan; writes the artifact
```

**Verify the write path is fast** (times the exact call `write_server` makes, via its baked import):

```bash
docker exec "$GW" /opt/hermes/.venv/bin/python - <<'PY'
import time, sys, os
sys.path.insert(0, "/opt/hermes/scripts/cron")
import id_index; from pathlib import Path
v = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
t = time.monotonic(); idx = id_index.build(v)          # force=False — the write-path call
print(f"write-path build(): {time.monotonic()-t:.3f}s  ids={len(idx.by_id)}")
PY
```

Expect **< 1s** (a load). Tens of seconds means the artifact is missing (run the refresh) or the
gateway image predates the fix (see below).

**Verify end-to-end with a real create** (probe an entity, then delete it — note the write path
**reshards by the slug's first letter**, so the file may not land at the path you passed):

```bash
docker exec -i "$GW" /opt/hermes/.venv/bin/python - <<'PY'
import time, sys, os
sys.path[:0] = ["/opt/hermes", "/opt/hermes/okengine-mcp", "/opt/hermes/scripts/cron"]
os.environ.setdefault("WIKI_PATH", "/opt/vault"); os.environ.setdefault("HERMES_DATA", "/opt/data")
import write_server as ws
t = time.monotonic()
print(ws._create("entities/z/zzz-probe.md", {"type": "vendor", "name": "ZZZ Probe"}, "# ZZZ Probe\n\nprobe\n"),
      f"in {time.monotonic()-t:.3f}s")
PY
# cleanup (find the resharded file + strip its write-log line):
find "$D"/wiki -name 'zzz-probe*.md' -delete
sed -i '/zzz-probe/d' "$D"/wiki/log.md
```

**Deploy a change to `scripts/cron/*.py` that write_server imports** — two paths:

- *Canonical:* `bash scripts/build-engine-image.sh` (re-clones Hermes, ~10–20 min), then
  `(cd $D && docker compose up -d --force-recreate --no-deps gateway)`.
- *Fast hotfix* (no Hermes re-clone): the committed overlay
  [`scripts/gateway-idindex-overlay.Dockerfile`](../scripts/gateway-idindex-overlay.Dockerfile):

  ```bash
  docker tag hermes-agent:latest hermes-agent:pre-idindex-hotfix                       # backup, reversible
  docker build -f scripts/gateway-idindex-overlay.Dockerfile -t hermes-agent:latest .
  (cd "$D" && docker compose up -d --force-recreate --no-deps gateway)                 # per deployment
  ```

  Confirm the baked copy took: `docker exec "$GW" grep -c 'force: bool' /opt/hermes/scripts/cron/id_index.py` → `1`.
  The next `build-engine-image.sh` release bakes it canonically, so there's no drift to track.

## Troubleshooting

| Check | If… | Do |
|---|---|---|
| `stat /opt/data/state/id-index.json` in the gateway | missing | run the `id-index-refresh` cron |
| `write-path build()` timing (above) | tens of seconds despite the artifact | the gateway image predates the fix → rebuild/overlay + recreate |
| `docker exec "$GW" grep -c 'force: bool' /opt/hermes/scripts/cron/id_index.py` | `0` | the baked id_index is old → rebuild/overlay + recreate |
| fleet-health lane still ERRORED after a fix | timeout was on a *different* MCP tool (`search`, `find_references`) | those are separate live-vault-scan paths — see the backlinks artifact fix + engine issues #198/#199 |

## Related

- `scripts/cron/id_index.py` — build/load/refresh + the `main()` the cron runs.
- `okengine-mcp/write_server.py` — `_registry()` / `_dedup_on_create` (the write path).
- `config/engine-crons.json` — the `id-index-refresh` cron (`40 */6 * * *`, `no_agent`).
- [`sharded-scan-discipline.md`](sharded-scan-discipline.md) — why the scan must `rglob`, and why it's expensive at scale.
