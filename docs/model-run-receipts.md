# Verified model-run receipts

For a lane whose output contract declares `completion: per-selected-item`, transport success is not
completion. The selector writes a runner-owned JSON manifest containing `selected` item keys and an
optional `input_digest`; the generated job names it with `selection_manifest`. The agent must end its
response with one fenced `okengine-receipt` JSON object.

Each selected key must occur exactly once with `accepted`, `duplicate`, `skipped`, `rejected`,
`failed`, or `deferred`. Accepted records carry written paths and SHA-256 hashes, which the runner
reads back. Duplicate and skipped records require a verifiable reason. Lane ID, contract digest, and
input digest must match runner-owned values. Rejected, failed, and deferred keys form the retry set.

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
