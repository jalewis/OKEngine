# okengine.contradictions

The first-party reference extension — the [extension-system](../../docs/design/extension-system.md)
§11 **first slice**. **`core: true` → default-ON** (#142): a contradictions dashboard is cheap
(deterministic `no_agent`) and useful on any OKF vault, so it's part of the house baseline —
active unless explicitly disabled (`framework extensions disable <pack> okengine.contradictions`).

It proves the whole in-gateway lifecycle end to end:

```
discover → enable → cron writes a dashboard → reader nav appears → disable stops it (page preserved)
```

## What it does

A deterministic, domain-agnostic operation: it walks the vault for `## Contradictions`
sections (and variants), classifies each as ACTIVE / EMPTY / RESOLVED, and renders
`wiki/dashboards/contradictions.md` — a ranked view of where the wiki disagrees with
itself. Pure script (`wakeAgent=false`); idempotent on a given day.

## Why it's an extension (not an engine cron)

It used to be the `contradictions-refresh` engine cron. It is exactly the kind of
**optional operation** the extension system is for — generic, dashboard-shaped, and
not every deployment wants it. Migrating it dogfoods the system: it now ships with the
engine (tier-1 `okengine.*`) but is **inert until enabled** (`present ≠ enabled`).

## Enable

```bash
python scripts/framework.py extensions enable <pack> okengine.contradictions
# redeploy: regen folds its cron job into cron-plus-jobs.json and stages this script
#           into the gateway at /opt/data/scripts/okengine.contradictions/
```

Disable with `extensions disable <pack> okengine.contradictions` — its cron job drops
from the fleet and the dashboard stops refreshing, but the page it last wrote is
preserved. The reader surfaces `wiki/dashboards/` automatically (okengine#117), so the
nav entry appears on enable and goes stale (then can be cleaned up) on disable.

## Write path

It writes the dashboard **directly** to the vault — the established pattern for engine
dashboard generators, and correct for a trusted first-party in-gateway op. The
`okengine-write` MCP path (§4) is the agent / third-party write contract, enforced for
sidecars once scoped MCP lands (okengine#132); a deterministic first-party dashboard
generator does not route through it.
