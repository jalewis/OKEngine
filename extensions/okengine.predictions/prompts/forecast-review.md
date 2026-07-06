The select_forecast_review.py wake-gate above surfaced this week's resolved predictions,
open predictions re-evaluated by regrade, and the current calibration / date-audit / schema-
audit dashboards. Synthesize a weekly forecasting-discipline review — every claim MUST trace to
a specific prediction page or dashboard number above; no invented trends, no generalizing
beyond the evidence shown.

**Trust the digest above — it already has what you need.** The dashboards and prediction lists
are already fetched and printed; do not re-fetch each prediction page individually unless you
need to read a specific page's `## Evidence log` to explain what changed on it this week.

Write via mcp_okengine_write_create_entity to the wiki-relative path the wake-gate specified,
frontmatter `type: dashboard, title: "Forecast review — <date>", updated: <date>`. Body:

```
# Forecast review — <date>

## Net portfolio motion
<1 paragraph: Brier trend if calibration.md shows a prior value to compare, resolution count
this week, schema/date-audit issue count trend if known>

## Resolutions this week
<for each resolved prediction: verdict + a one-line lesson. If a high-confidence prediction
(>=0.7) was refuted or a low-confidence one (<=0.3) confirmed, call it out explicitly as a
calibration red flag — don't soften it>

## Notable re-evaluations
<for open predictions re-evaluated this week: read the page's Evidence log, summarize what
changed and in which direction. Cap at 5 — pick the most consequential>

## Predictions to watch
<near-due open predictions, or ones whose evidence has gone quiet for a long stretch —
cite specifically, don't generalize>

## Hygiene
<summarize the date-audit + schema-audit flagged counts above; note if either grew or shrank
since you'd reasonably expect (no prior-value tracking, so describe the current count plainly)>
```

Cap each section at 5 entries — pick the most consequential, not the most recent. If a
prediction was a high-confidence miss, say so plainly; do not soften past predictions to make
the portfolio look better calibrated than it is. If the week was genuinely thin (few
resolutions, no notable re-evaluations), say so rather than padding.

Append a `wiki/log.md` entry: `## [YYYY-MM-DD HH:MM UTC] forecast-review | resolved=<N>
reevaluated=<M>`.

DO NOT use file_write/terminal/file_read to create the page — the okengine MCP write path is
the enforced contract; file_write is for the wiki/log.md line only.
