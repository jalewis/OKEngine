# okengine.events

Deterministic **domain event ledger + scoring** (okengine#155). Compiles the pack's dated
event-typed pages into a scored, newest-first ledger dashboard. No LLM (zero model budget).

**Generic mechanism, pack-config domain.** "A dated thing happened, here's its weight" is
sector-agnostic; the event TYPES + weights are pack config, read from `schema.yaml`:

```yaml
event_types: [deal, incident]           # page types that are events
event_date_field: date                  # frontmatter field with the event date (default: date)
event_score_weights: {deal: 2, incident: 1}   # per-type weight (default 1)
```

A pack that declares no `event_types` is a clean no-op. Writes `dashboards/event-ledger.md` (a
derived L1 view — **no new page type**, per the #148 convention) + an
`operational/event-ledger-snapshots.md` size trend.

**Built on the #63 cron drop-in model** — one `no_agent` lane in `crons/event-ledger.cron.json`.
A separate re-`score` lane (re-weight on config change) is a possible follow-up.
