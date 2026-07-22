Frontier brief (frontier-watch). Summarize the vault's OPEN capability frontier — the
`frontier/` whitespace-theses — into one short brief for a strategy reader. FIRST read the
`frontier/` namespace via the okengine read tools (each page is a `whitespace-thesis`: a
capability the market wants but few supply).

Produce a brief that:
- leads with the 3–5 highest-confidence, highest-demand whitespace theses (link each
  `[[frontier/<slug>]]`), one plain-language line each (the capability + why it's open);
- groups them if a theme is obvious (no forced taxonomy);
- flags which theses carry `lacuna_candidate: true` (a nameable force — the sharpest gaps);
- is honest about confidence (a thin-signal thesis is a lead, not a finding).

Write the brief to `frontier/brief-<YYYY-MM-DD>` (`type: report`) via
`mcp_okengine_write_create_entity`. Open with a one-line caveat that these are market
inferences, not verified claims. If there are no whitespace-theses yet, write nothing and say so.
LOCAL-ONLY. End with a one-line summary (how many theses briefed).
# Model-write boundary

Use only current `frontier/` pages as evidence. Mutate only through okengine-write within the lane contract and never edit logs directly. Finish with a run receipt naming the written path or the grounded reason no brief was written.
