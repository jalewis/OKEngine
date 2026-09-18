# Incremental metadata projection

PostgreSQL is a derived metadata plane; Markdown remains canonical. Each successful projection run
stores both its database run epoch and the corpus epoch it represents.

The projector reads `.okengine/corpus/journal.jsonl` after its last successful corpus checkpoint.
When every epoch through the current corpus epoch is present, only affected live Markdown pages are
read and parsed. Unchanged rows and outgoing links come from PostgreSQL, deleted journal paths are
removed, and the normal projection transaction republishes a semantically complete epoch. Metrics
record the mode, pages parsed, projection hits, bytes parsed, links, and elapsed time.

The journal is an optimization, never the correctness authority. A first run, missing checkpoint,
truncated/non-contiguous journal, malformed record, or corpus movement during a filesystem scan
selects the full reconciliation path. Reconciliation retains the deletion guard and reproducibility
checks. The service continues to perform it periodically as the correctness backstop.

Read queries compare `v_projection_health.corpus_epoch` with the live corpus epoch. They fail closed
while a mutation marker is active, when the epoch file is unreadable, or whenever PostgreSQL lags.
Successful query envelopes expose corpus lag, projection hits, pages scanned, bytes parsed, and
query latency. A consumer is migrated only with semantic-equivalence coverage for its filesystem
and projected results; the existing typed MCP count/page/link consumers are the first tranche.
