# Verified model-run receipts

For a lane whose output contract declares `completion: per-selected-item`, transport success is not
completion. The selector writes a runner-owned JSON manifest containing `selected` item keys and an
optional `input_digest`; the generated job names it with `selection_manifest`. The agent must end its
response with one fenced `okengine-receipt` JSON object.

Each selected key must occur exactly once with `accepted`, `merged`, `updated`,
`rejected-out-of-scope`, `insufficient-evidence`, `duplicate`, `deferred-for-review`, or `failed`.
Accepted, merged, and updated records carry written paths and SHA-256 hashes, which the runner reads
back. Every non-write record requires a verifiable reason. Lane ID, contract digest, and input digest
must match runner-owned values. Insufficient-evidence, failed, and deferred-for-review keys form the
retry set. Legacy skipped/rejected/deferred receipts remain readable during migration and normalize
to the canonical vocabulary; new prompts and producers must use canonical values.

The canonical fence remains preferred. If a model adds prose or uses a `json`/unlabelled fence, the
runner may recover the receipt only when exactly one JSON object matches the runner-owned lane ID,
contract digest, input digest, and exact selected-key set. The recovered object still passes through
all normal validation and readback checks. Multiple candidates, stale identities, and partial or
extra item sets fail closed. Persisted receipt diagnostics record whether the source was `canonical`
or `recovered-json`.
If the canonical opening fence is present but its closing fence is omitted, a structurally complete
JSON object may likewise be recorded as `recovered-unterminated-fence`; truncated JSON or any
non-whitespace trailing payload fails closed.

`receipt_mode: report` persists and reports invalid receipts without changing legacy success.
`receipt_mode: enforce` makes a missing, malformed, contradictory, incomplete, or failed-readback
receipt fail the run. Deterministic `no_agent` jobs retain ordinary process completion.

Receipts live under `cron-plus/receipts/<lane-id>/`; fleet status aggregates selected, accepted,
rejected, deferred, and undisposed counts.

For a whole-run writer (`completion: run`), transport completion is likewise insufficient. The
runner requires at least one execution-time write record and reads every reported target back from
the mounted wiki before marking the job successful. A lane that must publish one predictable
artifact may declare `required_write_path`, using `{date}` for the UTC run date (for example,
`briefings/daily-{date}.md`). Iteration exhaustion, fallback prose, and a stale prior-day artifact
therefore fail closed. These lanes must set `max_iterations` to at least 6 so ordinary
read/synthesize/write work retains a bounded write and recovery budget.
