# Module decomposition boundaries

The five large entrypoints currently mix transport, rendering, domain policy, authorization,
validation, and storage. Decomposition proceeds leaf-first and keeps every HTTP, MCP, and CLI
contract stable. The dependency direction is:

`transport -> rendering/domain/authorization/validation -> storage`

Rendering may depend on domain view models but never transport. Domain services may depend on
authorization, validation, and storage ports, but those leaf layers never import domain or
transport. Storage does not import another application layer. Transaction coordination belongs in
storage; review workflow belongs in domain; token/scope decisions belong in authorization.

Each extraction must move a cohesive responsibility with its tests, reduce the source entrypoint's
line ratchet, add no dependency cycle, and preserve public response/CLI snapshots. Moving lines into
an unbounded miscellaneous module does not qualify. New modules should remain below 800 lines and
prefer one public service or repository protocol. The completion target is 800 lines for cockpit
and 600/400 for the other entrypoints, as recorded in
`config/architecture-boundaries.json`; CI prevents any current monolith from growing while the
ratchets descend.

The migration order is storage and validation leaves, authorization, domain services, rendering,
then thin transport registration. This keeps every intermediate commit deployable and avoids a
flag-day rewrite.

Cockpit now implements that boundary as bounded modules under `src/okengine/cockpit_services/`.
`app.py` owns environment/exposure setup, process-local state installation, compatibility binding,
and FastAPI route registration only. Service functions are rebound to the facade namespace during
the migration so existing extension/test injection points retain their public names; the service
files own their implementations and are independently capped at 800 lines. The facade itself is
capped at 580 lines, below its original 800-line completion target.

The MCP write surface follows the same migration contract under
`src/okengine/write_services/`. Its 599-line facade owns tool registration and
process startup; bounded services own authorization, validation, review, policy,
deduplication, integrity, transaction, patch, convergence, and HTTP transport
logic. A small binding adapter preserves the module-level helper seams required by
existing callers while every implementation module is capped below 800 lines.
