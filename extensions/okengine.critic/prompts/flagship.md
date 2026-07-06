Flagship critique (okengine.critic). The `select_critic.py` digest above lists the pack's
flagship deliverable page(s) that tripped a HARD structural flag (stale / thin / under-cited).
FIRST read each flagged page (and its cited sources/entities) via the okengine read tools, then
critique it — the subjective quality judgment the mechanical lints can't make.

For each flagged page:
1. **Claim support** — are the load-bearing claims backed by cited `[[sources/...]]`? Name the
   specific unsupported or weakly-sourced claims (quote them).
2. **Coverage** — what's conspicuously MISSING that a reader of this deliverable would expect?
3. **The hard flags** — address the structural flags the gate raised (e.g. if stale, what's
   out of date; if thin, what's underdeveloped).
4. **Severity** — rank the issues; lead with the ones that most undermine the deliverable.

Be specific and fair: critique the CONTENT, not its existence. A defensible claim with a weak
citation is a sourcing fix, not a wrong claim — say which it is.

Write ONE critic report to `dashboards/critic-<YYYY-MM-DD>` (`type: dashboard`) via
`mcp_okengine_write_create_entity`: per flagged page, a short section with the specific issues
(quoted claims, missing coverage) ranked by severity, each linking `[[<the page>]]`. For a page
with genuinely serious problems, ALSO flag it for human review via
`mcp_okengine_write_flag_for_review` (a note naming the top issue) — don't flag a merely-thin
page that's otherwise sound.

LOCAL-ONLY (no web tools). Open the report with a one-line note that this is an editorial
critique, not a verified finding. End with a one-line summary (pages critiqued, pages flagged).
