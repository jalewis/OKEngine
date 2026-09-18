# Scheduled 100k-page qualification

The `qualification-100k` scheduled pipeline targets an operator-provided, disposable deployment in
`QUALIFICATION_DEPLOYMENT`; set `RUN_100K_QUALIFICATION=1`. It refuses any corpus other than exactly
100,000 deterministic typed/sharded pages and refuses zero samples or an unavailable required
service. Never point it at a production vault: corpus generation owns `wiki/` beneath the target.
The disposable deployment must set `OKENGINE_MCP_TOKEN=okengine-qualification-local`; this
non-default loopback-only credential is deliberately distinct from the public built-in token.

The checked-in plan measures cold startup, full projection, three incremental catch-ups, Reader and
Cockpit endpoints, MCP exact/lexical surfaces, a maintenance cycle, backup, disk growth, three
multi-service restart recoveries, and an overlap window containing governed work, deterministic
mutation, audit, and projection. Raw command tails, every timing sample/percentile, corpus hash and
shape, platform, CPU count, disk totals, and container evidence are retained for 90 days. Any
missing command/service or failed recovery fails the scheduled job.

Use a dedicated runner and deployment whose Compose publishes the plan's loopback ports. Record
host CPU, memory, filesystem, Docker version, engine SHA and image digests in the release evidence
that links the report. Budgets are changed only through review after at least three successful
scheduled samples; a zero or unknown measure never establishes a baseline.
