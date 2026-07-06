# Sidecar operation contract — implementation spec

**Issue:** okengine#135 · **Gate:** okengine#131 · **Parent design:**
[`extension-system.md`](extension-system.md) §6, §7, §11 · **Relates to:** #128
(in-gateway staging)
**Status:** design — **implemented (machinery); live execution operator-opt-in**
**Hard dependency:** #132 (scoped MCP) — landed; the networked write surface + scoped
tokens this contract injects now exist.

> **⚠ Networking superseded by okengine#138.** This spec was written when the gateway ran
> `network_mode: host` and reached the read MCP via `localhost:8730`. Since #138 the stack is on a
> per-pack **bridge**: the gateway reaches `okengine-mcp:8730` by service name, and a sidecar
> should **join that bridge** (not host net) and reach the read/write MCPs by service name too —
> which is what makes the #135 isolation story actually hold. Where this doc says "host net" /
> "`localhost:8730`" below, read "the per-pack bridge" / "`okengine-mcp:8730`".

A sidecar is the extension's own container: the *intended* isolation boundary (§7).

**Implemented:** manifest image-entrypoint validation (digest-pinned; `script` xor
`image` tied to `trust`); the composer emits a **trigger cron job** for a sidecar
(script = a generated `…/<id>/trigger.sh`); `extension_compose.sidecar_specs` /
`render_sidecar_service` / `render_trigger_wrapper` / `sidecar_compose_override`; and
`framework extensions sidecar-generate` writes `<pack>/.okengine/generated/
sidecars.compose.yml` (a compose override with each sidecar service — host network,
digest-pinned image, the #132 scoped token + MCP endpoints + `extension_id` injected as
env) plus the per-extension `trigger.sh`. The override holds tokens → written `0600`.
**Operator-opt-in / not auto-wired:** actually launching the sidecar needs (a) a real
sidecar image and (b) docker reachable from the host-net gateway (a socket mount — the
§3.4 trust note), so the deploy does **not** auto-launch sidecars. The operator brings
them up with `docker compose -f docker-compose.yml -f
.okengine/generated/sidecars.compose.yml …` and stages the wrappers into the gateway;
disable revokes the token (#132) and the sidecar is removed with `docker compose … rm
-f <id>-sidecar`. No first-party sidecar extension ships yet, so live execution is
unexercised — the in-gateway path (the first slice) is the proven one.

## 1. Current state

**Cron job schema — `config/engine-crons.json`.** A flat JSON object per job: `id` (12-hex),
`name`, `enabled`, `schedule` (`{kind: cron, expr}`, plus `@jitter:*` sentinels expanded at
deploy, `deploy-cron-plus-jobs.sh:51-64`), `workdir` (always `/opt/vault`), **`script`** — the
thing that runs, a basename resolved under `/opt/data/scripts/` or an absolute path — `prompt`
(may be `null`/`[SILENT]`), `deliver`, `no_agent`, and **`enabled_toolsets`** (e.g.
`["hermes-cron","okengine-write","okengine"]`, `:14-18` — how a job opts into the MCP
surfaces; jobs without it get `no_mcp`, `config.yaml.template:69-72`). **No `timeout` field
exists in any job** — §6's `operation.timeout` has no slot today. Run model is the two-phase
wake-gate: `script` runs as a deterministic gate emitting `{wakeAgent: bool}`; on `true` the
agent runs with `prompt` + `enabled_toolsets`.

**Assembly — `cron_pack_split.py`.** `compose`/`regen` concatenate engine + pack crons into
`config/cron-plus-jobs.json` (:226-269); `_dump_jobs` drops `enabled:false` and name-sorts
(:75-83). **A job specifies what to run purely via `script` — the generator never inspects or
rewrites it (opaque passthrough).** So a sidecar trigger must be expressible as a `script` (or
a new field cron-plus understands).

**Script runtime — `scripts/cron/`.** Scripts streamed into the gateway at `/opt/data/scripts/`
(`deploy-cron-scripts.sh:11-13`); env includes `WIKI_PATH=/opt/vault` (compose,
`docker-compose.yml:39`) + `.env` (model keys, delivery tokens); workdir `/opt/vault`; run as
`HERMES_UID`.

**Topology — `templates/pack/skeleton/docker-compose.yml`.** Three services; **`gateway` is
`network_mode: host`** (:19), mounts vault rw + `.hermes-data` → `/opt/data`, runs the cron
fleet + the stdio `okengine-write` subprocess. **okengine-mcp** (read) is HTTP streamable-http
on `8730`, published to `${OKENGINE_BIND:-127.0.0.1}` (:79-86), vault `:ro`. The gateway reaches
the **read** MCP over `http://localhost:8730/mcp` (host-net + loopback publish); it reaches
**write** MCP via the local stdio subprocess `args:[…/write_server.py]`
(`config.yaml.template:62-65`), `mcp.run(transport="stdio")` (`write_server.py:972`).
**Load-bearing gap: a separate sidecar container cannot reach `okengine-write` — there is no
network write surface, only a gateway-local stdio pipe** (§4).

**cron-plus.** An external pinned plugin (`install-cron-plus.sh:30-47`), runs **in-process
inside the gateway**, reads `/opt/data/cron-plus/jobs.json`, ticks ~60s under `.tick.lock`, and
on a due job runs the `script` as a **subprocess** (+ an in-process agent if `wakeAgent`).
**cron-plus has no concept of containers** — it spawns local subprocesses only. CLI:
`cron-plus.sh {list,run,tick,create}` via `docker exec` into the gateway.

## 2. Gap

1. No `image`/sidecar entrypoint in the cron schema — only a local `script`.
2. No `timeout` field anywhere; §6 mandates one.
3. No network-reachable write surface (stdio only, #132).
4. No per-extension scoped read/write token (one coarse bearer, §4) (#132).
5. cron-plus can't launch a container; it runs in-process subprocesses.
6. No env/secret injection scoped to one operation's container (§7).
7. No stop/remove path for a disabled sidecar.

## 3. Design

### 3.1 Manifest: script (in-gateway) vs image (sidecar)

Make `operation.entrypoint` a **discriminated union** — exactly one of `script` / `image`:

```yaml
operation:
  schedule: {kind: cron, expr: "17 5 * * *"}
  entrypoint:
    image:
      registry: registry.example.com/okengine.predictions
      tag: "0.1.0"                       # human-readable, informational
      digest: "sha256:abc123…"           # REQUIRED — pinned; reject tag-only for sidecar
      command: ["python", "/app/run.py"] # optional override
  timeout: 1800
```

vs. the in-gateway form (today's model): `entrypoint: {script: run.py}` (resolved under
`/opt/data/scripts/`, runs in the gateway). Rule: `trust: sidecar` ⇒ `image` required;
`trust: in-gateway` ⇒ `script` required. **Digest-pinned required** for sidecar
(supply-chain hygiene, matching the cron-plus pinned-SHA pattern, `install-cron-plus.sh:21-28`).
Unknown keys under `operation` already FAIL (§6).

### 3.1a OS sandbox (okengine#124)

The sidecar is the boundary for **untrusted third-party code**, so the generated compose service
runs confined (`render_sidecar_service`):

- **Image integrity:** digest-pinned (above) — `sidecar_specs` rejects a tag-only image, so the
  content is content-addressed and can't drift under a tag.
- **No host network** — joins the per-pack bridge (okengine#138) and reaches the MCP endpoints by
  service name; it does not share the host's network namespace.
- **`cap_drop: [ALL]`** + **`security_opt: [no-new-privileges:true]`** — no Linux capabilities, no
  privilege escalation.
- **`read_only: true`** rootfs with a `/tmp` **tmpfs** — no persistent host-filesystem writes; the
  only write surface is the scoped write MCP (#132).
- **Resource caps** — `pids_limit` / `mem_limit` / `cpus` (defaults 256 / 1024m / 1.0,
  overridable per-spec) so untrusted code can't exhaust the host.

**Still deferred** (the remaining #124 productization gate before running untrusted REMOTE
third-party extensions unattended): a **custom seccomp profile** (beyond docker's default),
**image signature verification** (cosign/sigstore — digest-pinning gives integrity, not
provenance), and **egress restriction** (an internal-only network / firewall so a sidecar without
a declared `network` capability can't reach the internet). The hardening above is the MVP floor;
these are the remaining wall.

### 3.2 Deploy → sidecar wiring

Because cron-plus can't launch containers and the gateway is host-net, **the framework CLI
generates a compose service per enabled `sidecar` extension** at enable/deploy, alongside
gateway/mcp/reader. The sidecar:
- joins the same host network (MVP) — see open Q2;
- gets vault `:ro` **only** if it declares `read: [wiki/**]` (normally it reads via the query
  MCP, not the FS — FS-blind by default);
- runs **one-shot, detached from its own schedule** (cron-plus is the only clock, 3.4).

### 3.3 Injected environment

- **No `WIKI_PATH`** unless a read capability is granted (FS-blind default).
- `OKENGINE_MCP_URL=http://localhost:8730/mcp` (read) and
  `OKENGINE_WRITE_MCP_URL=http://localhost:<write-port>/mcp` (the #132 write surface).
- `OKENGINE_EXTENSION_ID=<id>` — for attribution (but the **stamp is server-side**, 3.4/§5).
- `OKENGINE_CONFIG_*` — the §6 `config:` values (e.g. `horizon_days`).
- Declared `secrets:` injected **only** into this container's env (§7), never logged.
- `HERMES_UID/GID` for vault ownership parity if the FS is mounted.

### 3.4 Cron trigger — **cron-plus invokes the container via a thin wrapper**

Resolves §13 Q2. Two candidates: (A) cron-plus invokes the container each tick; (B) the
sidecar self-schedules. **Recommend (A) with a wrapper `script`:**

- cron-plus is the only scheduler and the audited control point (`cron-plus.sh list/run/tick`,
  jobs.json, `budget_guard` pause/resume by editing jobs.json, `budget_guard.py:19-23`).
  Self-scheduling (B) creates N uncoordinated clocks, defeats `budget_guard`, and means a
  disabled extension keeps firing until its own loop is killed.
- The generated job keeps `script: <id>-trigger.sh` (a deploy-generated wrapper under
  `/opt/data/scripts/`) whose body is
  `exec docker compose -f <pack>/docker-compose.yml run --rm -T <id>-sidecar`.
- The sidecar image stays **scheduleless, stateless, run-once-and-exit** (§11 "triggered by
  cron"). The wrapper can itself be the wake-gate (cheap "is there work?" check, exit 0 without
  launching the container when idle) — preserving the existing `wakeAgent` economics so an
  empty tick never starts a container.

**Caveat:** cron-plus runs *inside* the gateway, so `docker` must be reachable there — mount
the docker socket into the host-net gateway, scoped to `compose run` (acceptable for trusted
first-party, §7). If socket exposure is unacceptable, the fallback is a host-side runner
cron-plus signals (extra component). Recommend socket-for-`compose run` in MVP, documented as
a trust note.

### 3.5 Timeout

Wire `operation.timeout` (§6) into the generated job as a real field cron-plus enforces on the
`script` subprocess, **and** wrap `docker compose run` in `timeout <N>` so a runaway sidecar is
SIGKILLed and the container removed (`--rm`). This is the first consumer of a `timeout` field —
add it to the `engine-crons.json` job schema and the `cron_pack_split` passthrough (opaque, no
special handling, `cron_pack_split.py:108-147`).

### 3.6 Logs

`docker compose run -T` streams the sidecar's stdout/stderr to the cron-plus subprocess, landing
wherever cron-plus already routes script output (`scripts/cron-plus-logs.sh`). The sidecar should
log structured JSONL to stdout; deploy tags the service `com.docker.compose.project=<pack>` so
`cron-plus-logs.sh`/`docker logs` scope correctly (mirrors multi-pack scoping in
`cron-plus.sh:26-36`). Secrets never echoed (§7).

### 3.7 Restart / cleanup / disable

- One-shot (`--rm`): no long-lived container, nothing to crash-loop, so **no `restart:` policy**
  (sidesteps the project's `restart: always` ban).
- **Disable** (`framework extensions disable <id>`): (1) the generated cron job is removed from
  `cron-plus-jobs.json` (existing `enabled:false` drop, `cron_pack_split.py:82`); (2) the
  generated compose service stanza is removed and `docker compose rm -f <id>-sidecar` runs
  (optional `docker rmi`); (3) the scoped tokens (#132) are revoked; (4) pages it wrote are
  **preserved** (orphaned, not deleted — §9, #133). No `purge` in MVP (#127).
- **Enable** regenerates the compose service + trigger job + mints tokens, fail-loud before any
  deploy (§9).

## 4. Dependency on #132 (tight)

#135's **trigger, env, timeout, logs, cleanup are buildable on today's surfaces** (cron-plus +
`docker compose run` + the existing host-net read MCP). The **write path is hard-blocked on
#132**: there is no network-reachable `okengine-write` (stdio only, `write_server.py:972`) and
no per-extension scoped tokens (one coarse bearer, §4). Ship #135's plumbing against the read
MCP first; gate the enforced write + scoped-token injection (`OKENGINE_WRITE_TOKEN`,
`OKENGINE_READ_TOKEN`) behind #132. Without #132 a sidecar is wired and triggered but **cannot
conformantly write** — the isolation boundary is plumbed but not enforced (§7).

## 5. Test plan

- **`tests/cron/test_cron_pack_split.py`** — a job with `entrypoint.image` round-trips through
  split/merge losslessly; `timeout` passes through untouched.
- **`tests/cron/test_compose.py` / `test_merge_packs.py`** — composing a pack with a `sidecar`
  extension generates exactly one trigger job; asserts `image.digest` present (reject tag-only).
- **New `tests/test_extension_manifest.py`** — `trust: sidecar` requires `entrypoint.image`;
  `in-gateway` requires `script`; both/neither ⇒ FAIL; unknown `operation` keys ⇒ FAIL; digest
  required for sidecar.
- **New `tests/test_sidecar_trigger_gen.py`** — deploy generates the wrapper `script`
  (`docker compose run --rm`), the compose service stanza, and the env set
  (`OKENGINE_EXTENSION_ID`, `OKENGINE_*_MCP_URL`, scoped token names); secrets not written to the
  generated job/logs.
- **`tests/test_mcp_auth.py`** — once #132 lands: scoped write token authorizes only the
  manifest's `write:` namespaces; read token only `read:` paths. (Read half testable now.)
- **`tests/test_framework_validate.py`** — `extensions validate` rejects a sidecar manifest
  missing image/digest/timeout.
- **Cleanup regression** — disable removes the cron job AND the compose service AND revokes
  tokens, while leaving produced pages on disk.

## 6. Open questions

1. **Docker access from cron-plus** — socket into the host-net gateway (simplest, MVP) vs a
   host-side runner (no socket exposure, extra component).
2. **Sidecar network mode** — host net (reaches `localhost:8730` like the gateway) vs a
   dedicated bridge with the MCP republished. Host net is simplest; bridge is the cleaner
   isolation story once #132's scoped MCP exists.
3. **Trigger gate placement** — wake-gate in the wrapper `script` (in-gateway, fast;
   recommended) vs inside the sidecar (one container start per empty tick).
4. **Provenance stamp location** (§13 Q3) — OKF envelope field vs sidecar index; server-side
   stamp (write MCP derives owner from the scoped token) is more tamper-resistant. Decide with
   #132 — and #135 therefore treats `OKENGINE_EXTENSION_ID` as advisory, not the source of truth.

**Anchors:** job schema `config/engine-crons.json`; generator `scripts/cron_pack_split.py:75-83,
108-147,226-269`; MCP wiring `config/config.yaml.template:49-72`; topology
`templates/pack/skeleton/docker-compose.yml:19,79-99`; stdio gap
`okengine-mcp/write_server.py:972`; cron-plus `scripts/cron-plus.sh`, `install-cron-plus.sh`,
`deploy-cron-plus-jobs.sh`, `scripts/cron/budget_guard.py:19-23`.
