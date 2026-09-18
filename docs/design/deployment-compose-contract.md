# Deployment Compose contract v1

The engine owns the complete base at `templates/pack/skeleton/docker-compose.yml`; its
`x-okengine-compose-contract.version` is the format version. Packs declare identity and
`port_offset` in `pack.yaml` and may carry only `deployment.compose.yaml` with `api: 1` and
per-service `environment`, `ports`, `profiles`, `deploy`, or `image` overrides. Unknown services
and keys fail closed. Literal secret values are rejected; operator secrets remain in the untouched
`.env` file and overrides reference them as `${NAME}`.

`framework deployment render <pack>` deterministically writes `docker-compose.yml`.
`framework deployment diff <pack>` compares the checked-in/effective file with a fresh render and
returns nonzero on drift. `framework deployment migrate <pack>` extracts supported differences
from a legacy Compose snapshot into the small override and refuses differences outside the v1
contract. Migration never changes the legacy Compose or `.env`; review the override, render, run
`docker compose config`, and only then replace the snapshot.

`framework deployment parity <catalog-or-pack> [...]` recursively discovers packs, requires
contract adoption, validates each effective service model, and fails when a checked-in Compose
file differs byte-for-byte from the deterministic engine render. It is read-only and suitable for
catalog and live-fleet parity gates.

Rollback is `git revert` plus restoring the prior `docker-compose.yml`; `.env`, runtime data,
volumes, and deployment identity are not mutated by render or migration. Pack parity CI should run
`deployment diff` for every catalog pack after it adopts v1. The existing validator-content gate
continues to reject drifted `validate.py` copies; adopted packs should invoke the engine-pinned
`framework validate` as the authoritative verdict; the vendored validator is an offline
compatibility shim and its content is pinned by the engine validator-vintage gate.
