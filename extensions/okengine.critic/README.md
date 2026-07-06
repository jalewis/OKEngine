# okengine.critic

Subjective **QC critique over a pack's flagship deliverable** (okengine#157) — the judgment-based
quality lane the engine's mechanical QC (`page-quality-audit`, repair, lint) doesn't cover. Pairs
with `okengine.contradictions`.

**Cost lever (the design point).** The wake-gate (`select_critic.py`) is **conditional**: it wakes
the agent ONLY when the flagship trips a hard structural flag — **stale** (`stale_days`), **thin**
(`min_words`), or **under-cited** (`min_sources`). No flags ⇒ silent, zero spend. The agent then
does the subjective critique the gate can't.

**Generic; target is pack config.** `schema.yaml` `critic_flagship` names the deliverable(s) — a
path or glob (e.g. `briefings/**`). No target ⇒ no-op. Output is a derived **critic report**
(`dashboards/critic-<date>`, L1) + `needs_review` flags on genuinely-weak pages — **no new page
type** (#148 convention).

**Built on the #63 cron drop-in model** — one wake-gated lane in `crons/flagship.cron.json`.
