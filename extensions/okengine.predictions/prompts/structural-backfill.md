Backfill missing falsification criteria. The select_prediction_structural_backfill.py digest above
lists gradable predictions (in priority order) that lack a `## What would refute this` section — the
falsifiability contract every prediction is supposed to carry. Your job: for EACH prediction in the
batch, author genuine, specific, observable refutation criteria and add the section via the write
path. FIRST response MUST be a tool call (`file_read` the first prediction). LOCAL-ONLY — no web
tools; the prediction's own claim + reasoning are your source.

For each prediction in the batch, IN ORDER:

1. `file_read` the prediction page. Read its claim, reasoning/basis, `subject`, `confidence`, and
   `resolves_by`.
2. Derive **what observable outcome, by `resolves_by`, would prove this prediction WRONG.** Good
   criteria are:
   - **Specific and observable** — a concrete event, threshold, or absence you could check against
     public reporting, not "if the trend reverses."
   - **Tied to the claim's own terms** — if the claim is "≥N of these signals by date D", refutation
     is "fewer than N by D"; if it names a threshold (≥50MW, a CVE added to KEV, an 8-K), refutation
     is that threshold not being met by the date.
   - **Genuinely falsifiable** — if you cannot state a concrete way it could fail, the prediction may
     be unfalsifiable as written; say so in the section (one line: "As written this is hard to
     falsify because …") rather than inventing a fake test. Do NOT pad.
3. Add the section with:
   `mcp_okengine_write_append_to_section(path="predictions/<slug>", heading="What would refute this", text="<criteria>")`
   — this creates `## What would refute this` at the end of the page, preserving everything else.
   Use a short bulleted list (2–4 bullets) of concrete refuters.

HARD rules:
- Do NOT edit frontmatter — no touching `confidence`, `status`, `resolves_by`, `updated`, or
  `sources`. `append_to_section` is the ONLY write you make per prediction; it bumps version and
  logs on its own.
- Do NOT invent evidence, sources, or events. Refutation criteria describe hypothetical *future*
  observations that would break the claim — they are not claims themselves.
- Do NOT re-grade, re-argue, or add any other section. One section per prediction, then move on.
- If a page already has the section (a race with a prior run), skip it and note that in your summary.

End with a one-line-per-prediction summary of the refutation criteria you added.
# Model-write boundary

Process only selector-named items. Ground claims in pages you read, use only okengine-write mutations allowed by the lane contract, and never edit logs directly. Finish with a receipt for every selected item: `path: written | deferred | rejected — reason`.
