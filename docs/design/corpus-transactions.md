# Corpus transaction protocol

Canonical knowledge remains Markdown under `wiki/`. The transaction layer provides consistency,
not a replacement datastore.

Every governed mutation holds `.okengine/corpus/lock` exclusively. Deterministic operations take
the fence in the engine-owned operation runner, including asynchronous cockpit runs; MCP tools take
it at tool registration, so a newly registered write tool is fenced automatically. Existing
operation resource locks remain the narrower conflict/ownership contract inside this corpus fence.

Before invoking a writer, the engine records an `active.json` marker containing the transaction
identity, writer, operation, start time, and SHA-256 identity of each canonical Markdown file. It
never stores page content. On completion it compares identities, advances `epoch` once for the whole
batch, appends `journal.jsonl`, fsyncs the durable state, and removes the marker. Journal records
contain writer, operation, affected paths, before/after identities, epoch, and disposition.

If a process dies, the OS releases its fence but leaves `active.json`. The next writer or stable
reader acquires the exclusive fence, compares the recovered corpus to the recorded identities,
appends a `recovered` journal entry, advances the epoch when necessary, and only then proceeds.
Thus a crash is detectable and auditable without copying secrets into the journal.

Audits use `stable_corpus()` for their entire traversal. It first performs recovery and then
downgrades to a shared fence. Writers wait until the audit finishes; overlapping writers serialize.
Callers that cannot retain the fence for a complete traversal must read the epoch before and after
and retry if it changed.
