# okengine.predictions

First-party extension: **falsifiable, dated forecasts**. The design's canonical example of
an extension (extension-system.md §1) — now actually one, migrated out of the engine cron
fleet so predictions ships and is enabled independently of the base wiki.

## What it does

Three wake-gated **agent** lanes (each: a deterministic selector script gates whether the
agent wakes, then the agent runs with the okengine write tools):

| operation | job | schedule | selector | does |
|-----------|-----|----------|----------|------|
| `candidate-watch` | `okengine.predictions:candidate-watch` | `17 6 * * *` | `select_prediction_candidates.py` | file a prediction for a recently-active entity with no open one — only when a specific dated claim is warranted |
| `grade` | `okengine.predictions:grade` | `23 6 * * *` | `select_predictions_for_grading.py` | resolve predictions whose `resolves_by` has passed (postmortem + flip `status:`) |
| `regrade` | `okengine.predictions:regrade` | `29 */6 * * *` | `select_regrade_batch.py` | reassess only predictions whose cited sources changed; dispose deterministic confidence recommendations |

The deterministic `confidence-recommender` runs at `27 */6 * * *`, before regrade. It joins
pending structured evidence records to `okengine.events` scores, writes a recommendation-only
JSONL sidecar plus dashboard, and applies the constrained origin rule: ±0.15 per event, ±0.20
per cycle, and final confidence in [0.05, 0.95]. Driver inputs remain verbatim in the record.
The agent retains the pen and must record why it deviates. If scored inputs are absent, the
fallback discipline forbids another confidence rise without a skeptic/falsification pass.

## Forecasting-discipline lanes (okengine#159)

Deterministic **no_agent** measurement lanes (pure computation, zero model cost) plus one
**agent** meta-layer that synthesizes them weekly:

| operation | schedule | writes | measures |
|-----------|----------|--------|----------|
| `calibration-refresh` | `40 6 * * *` | `dashboards/calibration.md` + state history | Brier score/trend and calibration by confidence, horizon, and basis signal class; open-by-horizon, near-due/stale forecasts, recent resolutions, high-confidence misses, subject/watchlist coverage, and evidence-direction bias telemetry |
| `prediction-date-audit` | `45 6 * * *` | `dashboards/prediction-date-audit.md` | `resolves_by` sanity — missing, unparseable, overdue-but-open, or unfalsifiably-distant |
| `prediction-schema-audit` | `50 6 * * *` | `dashboards/prediction-schema-audit.md` | field hygiene the date-audit doesn't cover: missing `made_on`/`confidence`/`subject`, unparseable `confidence`, `horizon` mismatched against the computed `made_on`→`resolves_by` day-count, missing `## What would refute this` |
| `prediction-schema-drain` | `30 21 * * 0` (Sun) | `predictions/**` (frontmatter) | agent op — DRAINS frontmatter VALUE drift the audit flags: missing required fields (derived from the page's own claim/`created:`), non-canonical `status`/`horizon`, unparseable `confidence`. Merge writes via `update_entity`; never fabricates; flags batch-container files for human review. Structural-frontmatter repair is the engine's generic `repair-*` lanes' job |
| `prediction-structural-backfill` | `45 */6 * * *` | `predictions/**` (adds a section) | agent op — DRAINS what `prediction-schema-audit` flags: gradable predictions missing `## What would refute this`. Authors real per-prediction falsification criteria via `append_to_section` (5/run, soonest-`resolves_by` first). Resolved/archived predictions out of scope |
| `forecast-review` | `0 16 * * 6` (Sat) | `briefings/forecast-review-<date>.md` | agent op — weekly meta-layer: net portfolio motion, this week's resolutions (calling out high-confidence misses plainly), notable re-evaluations, predictions to watch, hygiene summary. Wake-gated on the other lanes producing something new to say |

`base-rates` and `output-outcome-eval` are hybrid lanes: their selectors always compute numeric
JSONL + `base-rate-metrics.md` / `output-outcome-metrics.md` dashboards at zero model cost, then
wake an agent only when enough history exists to interpret the numbers. Base rates include event
frequency/materiality, entity frequency, event-to-prediction coverage, resolved outcome rates by
horizon/basis class/event type, and publisher mix/sole-basis dependence. Output-outcome metrics
join surfaced sources to later prediction basis and briefed entities to later material events.
`prediction-falsification-search` remains a separate judgment lane.

## Schema

Reuses the pack-owned `prediction` type (`write: predictions/**`) and contributes only the
shared `evidence:` item contract; it does not own the type. A pack that enables this must
declare a `prediction` type in its `schema.yaml` with at least `status`, `confidence`,
`subject`, `resolves_by`, and a `## What would refute this` section convention.

## Prompts

Bundled generic prompts (`prompts/*.md`) defer domain specifics to `$WIKI_PATH/CLAUDE.md`.
Override per-deployment without forking the extension: add
`<pack>/.okengine/extension-prompts.json` mapping the namespaced job name to a prompt, e.g.

```json
{ "okengine.predictions:grade": "…your tuned grading prompt…" }
```

## Enable

```
framework extensions enable <pack> okengine.predictions
# redeploy: regen folds okengine.predictions:{candidate-watch,grade,regrade} into the fleet
```
