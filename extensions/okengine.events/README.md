# okengine.events

Deterministic **domain event ledger + scoring substrate** (okengine#155, #220). Compiles the
pack's dated event pages into dashboards, a machine-readable eight-score vector, and optional
typed-event partitions. No LLM (zero model budget).

**Generic mechanism, pack-config domain.** "A dated thing happened, here's its weight" is
sector-agnostic; the event TYPES + weights are pack config, read from `schema.yaml`:

```yaml
event_types: [deal, incident]           # page types that are events
event_date_field: date                  # frontmatter field with the event date (default: date)
event_score_weights: {deal: 2, incident: 1}   # per-type weight (default 1)
event_scoring:
  source_kind_weights: {filing: 1.0, article: 0.7}
  evidence_phrases: [definitive agreement, customer count]
  watchlist_tier_weights: {priority: 1.0, monitor: 0.5}
  typed_extractors: {deal: m-and-a}  # optional semantic extractor per pack type
```

A pack that declares no `event_types` is a clean no-op. Writes `dashboards/event-ledger.md` (a
derived L1 view — **no new page type**, per the #148 convention) + an
`operational/event-ledger-snapshots.md` size trend. The scoring lane additionally writes:

- `dashboards/event-scoring.md`, ranked by materiality × recency × (1 + relevance)
- `$HERMES_DATA/state/okengine.events/event-scores.jsonl`, deterministic eight-score rows for
  pack-declared events plus source-intrinsic rows for every canonical `type: source` page. Source
  rows carry `score_scope: source` and key `source` to their own wiki path, allowing evidence
  consumers to join sources that are not attached to any event page.
- `$HERMES_DATA/state/okengine.events/typed-events/*.jsonl`, regex-extracted typed partitions

**Built on the #63 cron drop-in model** — two `no_agent` lanes: the ledger at 05:40 and scoring
at 05:45. Re-running scoring after a config change atomically replaces all derived rows.
