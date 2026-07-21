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

## Enabling a pack operation

The pack installer adds its operation name to `OKENGINE_OPERATION_ALLOW` without modifying tokens.
Start the operator services with:

```bash
docker compose --profile review up -d --build
```

Do not expose the operation runner port. Add an operation to the allowlist only after its manifest,
plan behavior, locks, receipts, and recovery path have passed conformance and integration tests.
