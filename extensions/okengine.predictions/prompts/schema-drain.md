Normalize prediction schema drift. The select_prediction_schema_drain.py digest above lists
prediction pages with fixable VALUE drift (missing required fields, non-canonical status/horizon,
unparseable confidence). Your job: for EACH page in the batch, fix the listed issues via the write
path — derive the correct values from the page's OWN content, never fabricate. FIRST response MUST
be a tool call (`file_read` the first page). LOCAL-ONLY — no web tools.

Canonical values for this vault:
- `status`: one of open, active, confirmed, refuted, partial, expired-ungraded. (`active` is a
  valid open synonym — do NOT "fix" it.)
- `horizon`: one of short (≤90d), medium (≤365d), long (≤1825d), strategic (>1825d) — pick the one
  matching the made_on→resolves_by span.
- `confidence`: a number 0.0–1.0 OR a qualitative label (low/medium/high etc.) — both are valid;
  only replace genuine garbage.

For each page in the batch, IN ORDER:

1. `file_read` the page. Read its frontmatter + claim/reasoning body.
2. Determine the correct value for each flagged field FROM THE PAGE ITSELF:
   - **missing `made_on`** → use the frontmatter `created:` date (or the filing date stated in the
     body). **missing `resolves_by`** → the explicit deadline in the claim; if the claim gives a
     horizon but no date, compute resolves_by = made_on + the horizon's span. **missing `subject`**
     → the primary `[[entities/...]]` or `[[concepts/...]]` the claim is about (from the body).
     **missing `horizon`** → classify from the made_on→resolves_by span using the rubric above.
     **missing `confidence`** → only if the body states one; else flag (see below).
   - **horizon drift** (e.g. `medium-term`) → the canonical bucket for the actual day-span.
   - **status drift** → the canonical status the body supports (a suggestion may be in the digest).
   - **unparseable confidence** → the numeric or qualitative value the body's certainty language
     supports.
3. Apply with a SINGLE merge write per page:
   `mcp_okengine_write_update_entity(path="predictions/<slug>", frontmatter_yaml="<only the
   corrected/added fields>")` — this MERGES the given keys and leaves the body untouched.

HARD rules:
- **Never fabricate.** If a required field genuinely cannot be derived from the page (no date, no
  subject anywhere), do NOT invent one — leave it, and list that page under "needs human review" in
  your summary with what's missing and why.
- Only write the fields the digest flagged. Do NOT rewrite the body, add sections, or touch
  unrelated frontmatter (`sources`, other fields). `update_entity` bumps version/last_updated on
  its own.
- **Batch-container files** (flagged with ⚑ in the digest) are NOT yours to fix — splitting or
  re-typing them is a human decision. Do not edit them; just restate them under "needs human
  review" so the operator sees them.

End with a per-page summary: what you set on each, and any page left for human review (with the
reason).
