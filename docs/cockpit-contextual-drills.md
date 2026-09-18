# Contextual cockpit drills

Cockpit aggregate and truncated-table drilldowns are investigation surfaces, not file pickers.
Every result keeps the stable `path`, `title`, and `type` keys and may also carry:

- `summary`: a concise explanation drawn from the record's summary, description, claim, reason,
  judgment, rationale, or consequence;
- `facts`: ordered `{label, value}` pairs explaining why the result is useful in this panel.

For `table` and `cards` panels, the configured `columns` are the source of truth for facts. A
column's label, value labels, list cap, date/percentage formatting, defanging, publisher repair,
and assessment semantics are retained in the drill. Title-link columns are omitted because the
card heading already supplies that information. This means improving the compact panel also
improves its complete-list drill without adding a second schema.

Aggregate panels (`bars`, `chips`, `coverage`, and `bignums`) usually have no columns. Their
results receive non-empty common context when available: publication/activity dates, lifecycle
status, confidence, severity, publisher, source kind, and sector. The serializer does not invent
values or stringify nested objects.

The client renders result cards with a summary, labeled facts, an in-overlay text filter, a live
visible-result count, and explicit Open record / Copy path actions. Filtering covers titles,
types, summaries, paths, fact labels, and fact values. Configured ordering, result caps, semantic
sections, and the legacy flat `pages` array remain unchanged.

Pack authors should put the fields that answer the panel's analyst question in `columns`. For
example, a recently-active actor panel should include the activity date and evidence counts; a
remediation panel should include vendor, product, EPSS, severity, and due date. Empty fields are
suppressed in drill cards rather than rendered as claimed context.
