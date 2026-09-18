# Operation API and Cockpit controls

Declarative operations use one execution contract across the `framework` CLI, Cockpit, and
schedules. The engine owns discovery and execution mechanics; a pack owns its operation manifest
and domain behavior.

## Deployment boundary

`okengine-operation-runner` is an optional, bridge-only service in the `review` Compose profile.
It has a read/write vault mount because declared operations may mutate governed artifacts. It does
not publish a host port. Every non-health request requires the internal bearer token, and an empty
`OKENGINE_OPERATION_ALLOW` exposes no operations.

Cockpit retains a read-only vault mount. It proxies only plan, start, and request-status calls after
the same operator-authentication decision used by human review: configured Basic authentication or
explicit trusted-network mode with a named reviewer. Browser mutations additionally require the
same-origin `X-OKEngine-Operation: 1` header.

## Plan-before-start contract

An `operation-control` box declares an operation and its arguments:

```yaml
- title: Run full actor review
  view: operation-control
  operation: actor-review
  arguments: [--all]
```

Cockpit first calls the operation's dry-run implementation and displays its actor/resource count,
question/work count, dimensions, and snapshot digest. Start remains disabled until planning
succeeds. The runner recalculates the plan immediately before execution and rejects the request if
the supplied digest no longer matches. The operation then runs asynchronously; Cockpit polls a
durable request record and the operation's normal receipt. A web request never owns the workflow.

## API

- `GET /operations` — allowed, discovered operations.
- `POST /operations/{name}/plan` — execute the operation's non-mutating plan.
- `POST /operations/{name}/run` — revalidate the plan digest and start asynchronously.
- `GET /operations/requests/{request_id}` — request, run ID, receipt, and monotonic progress.

The service invokes the same command builder used by `framework operations run`, setting
`OKENGINE_OPERATION_SOURCE=cockpit`. Domain receipts and locks remain authoritative. Scheduler jobs
continue to call the same pack entrypoint and create the same receipt type.

## Operation manifest

A pack/extension contributes an operation as `operations/<dir>/operation.yaml`. The engine validates
the manifest **fail-closed** at discovery (`scripts/framework_operations.py`); an invalid manifest
removes the operation from the registry rather than running with an unenforced contract.

| field | required | validation |
|---|---|---|
| `operation_api` | yes | must be `1` |
| `name` | yes | `^[a-z][a-z0-9-]{1,79}$` |
| `owner` | yes | non-empty (contributing pack/extension) |
| `entrypoint` | yes | safe deployment-relative path to an existing file (no `..`, not absolute) |
| `execution` | no | one of `deterministic`, `model`, `mixed` |
| `mutates` | no | boolean |
| `supports` | no | mapping; `plan`/`resume`/`cancel` are booleans |
| `arguments` | no | mapping `{name: {type, repeatable, …}}`; `type` ∈ `boolean,string,int,float,page-ref,enum`; `repeatable` boolean |
| `locks` | no | list of resource ids `^[a-z0-9][a-z0-9/_.-]{0,120}$` — acquired before mutation (okengine#402) |
| `inputs` | no | list of safe deployment-relative globs — feed the engine snapshot digest (okengine#402) |
| `outputs` | no | list of safe deployment-relative globs — validated to exist before a run may report `succeeded` |
| `permissions.capability` | no | non-empty string — the authorization the runner checks before starting |
| `receipt_schema` | no | safe deployment-relative path |
| `timeout` | no | positive number of seconds |

Unknown top-level keys are preserved (forward compatibility); known fields are enforced strictly.

## Run lifecycle (engine-owned)

The **engine** owns the run — the entrypoint is a worker (`scripts/operation_run.py`, okengine#402):

- The engine allocates the run id (`<name>-<utcstamp>-<rand>`) and passes it as
  `OKENGINE_OPERATION_RUN_ID`. The entrypoint must NOT choose its own id or write the receipt.
- Before a mutating run, the engine acquires the declared `locks:` (flock under
  `.okengine/operations/locks/`); a conflicting run whose holder is alive is refused, a stale lock
  (dead holder) is recovered.
- The engine computes the input snapshot digest from the declared `inputs:` — a run cannot claim a
  plan digest it did not derive.
- The engine writes the authoritative receipt (`running` → `succeeded`|`degraded`|`failed`) under
  `.okengine/operations/runs/<op>/<run_id>.json`. A worker's own status can only DOWNGRADE the
  result, never upgrade it, and a `succeeded` claim with an absent declared `outputs:` is recorded as
  `degraded` — a partial result can never be reported as complete.

`plan` runs the entrypoint with `--dry-run`, writes no receipt, and mutates nothing.

## Enabling a pack operation

The pack installer adds its operation name to `OKENGINE_OPERATION_ALLOW` without modifying tokens.
Start the operator services with:

```bash
docker compose --profile review up -d --build
```

Do not expose the operation runner port. Add an operation to the allowlist only after its manifest,
plan behavior, locks, receipts, and recovery path have passed conformance and integration tests.
