# Hermes upgrade records

OKEngine pins a specific Hermes-Agent tag (`engine-manifest.yaml` → `runtime.pinned_*`) and
applies `patches/` as an overlay — we **consume** Hermes, we don't fork or merge it. Every pin
bump gets a record here: what changed upstream, how the carried patches fared, any behavior
change that touches a deployment, and the build/smoke results.

The bump procedure (clone the new tag → re-check `patches/` apply → bump the pin everywhere →
`make check` → `build-engine-image.sh` → smoke-test) is captured as a reusable checklist in the
latest record's **§2**, and the stock-Hermes → engine path is in [`INSTALL.md`](../../INSTALL.md).
What each carried patch does is in [`patches/README.md`](../../patches/README.md).

| Pin (tag) | Hermes | Engine | Record |
|---|---|---|---|
| `v2026.6.19` | v0.17.0 | v0.3.4 | [v2026.6.19-v0.17.0.md](v2026.6.19-v0.17.0.md) — 6/6 patches clean; dropped 07 (upstreamed) + 08 (declined); cron-plus unchanged |
| `v2026.6.5` | v0.16.0 | v0.2.0–v0.3.3 | _(pre-dates this log)_ |

**Guiding rule** (from the v0.17.0 bump): carry a patch only for a **domain-specific** fact
upstream will never adopt (e.g. OKF compiling security intel that quotes attacker commands).
Decline divergence on **generic infrastructure** upstream owns (image perms, build layout) —
measure the real cost and mitigate on our side instead. Divergence on generic infra compounds
every release.
