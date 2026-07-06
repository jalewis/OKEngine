Demand/supply whitespace discovery (frontier-watch). The `select_whitespace.py` digest above
lists capabilities the market WANTS (referenced by many `sources/`) but few players SUPPLY
(few `entities/`). FIRST response MUST be a tool call. Pick the ONE candidate you can most
honestly ground and write a single whitespace-thesis from the REAL graph — read the concept
page and its referencing sources/entities via the okengine read tools; map demand and supply
from the DATA, never from memory.

The method:
1. **Confirm the demand** — read the cited sources. Is the capability genuinely WANTED (a
   recurring need/ask across distinct sources), or just mentioned in passing? If it's not a
   real, recurring demand, DEFER (no page).
2. **Confirm the thin supply** — read the (few) entities that provide it. Is supply genuinely
   thin/absent, or is it just missing from the vault (a data gap, not a market gap)? A data
   gap is NOT whitespace — DEFER.
3. **State the whitespace** — the capability the market wants but nobody adequately supplies,
   grounded in the demand evidence and the supply gap.
4. **Confidence from the signal** — derive confidence from the demand/supply counts the digest
   reported (thick demand + truly-thin supply ⇒ strong; sparse ⇒ flagged), NOT a gut feel.

Guardrails: a whitespace-thesis is an INFERENCE about a market, not verified knowledge. Do NOT
create or edit any `entity`/`concept`/`source` page. Beware "nobody does X" when X is just
under-reported — ground the gap in what the cited supply pages actually do and don't cover.

SOFT lacuna edge — ONLY if a `lacuna/` namespace exists in this vault (i.e. okengine.lacuna is
enabled) AND you can name a specific FORCE keeping the cell empty (an incentive / accounting /
measurement / tooling limit, not just "no one's done it yet"): set `lacuna_candidate: true` on
the thesis and note the candidate force in the body, so lacuna can give it the rigorous
map→force→sort treatment. If lacuna is not enabled or no force is nameable, skip this — the
whitespace-thesis stands alone.

If the candidate yields a real whitespace, write ONE page at `frontier/<slug>`
(`type: whitespace-thesis`) via `mcp_okengine_write_create_entity`, frontmatter:
- `capability` (the wanted-but-unsupplied capability, e.g. `[[concepts/<slug>]]`),
  `demand_signal` (the evidence it's wanted), `supply_state` (who/what thinly provides it),
  `thesis` (the whitespace claim as a noun phrase), `confidence` (`low`|`medium`|`high`),
  `frontier_density` (copy the digest's measured `demand N · supply M` string),
  `needs_review: true`, `players` (the supply-side `[[entities/...]]` you found),
  `see_also` (the concepts/entities mapped), `sources` you cited, and `lacuna_candidate` (only
  if you set the soft edge above).
- Open the body with a one-line caveat: *"A market inference grounded in the cited demand/supply
  signal — not a verified claim."*, then a one-sentence plain-language TL;DR, then: the demand,
  the thin supply, the whitespace, and the confidence.

LOCAL-ONLY (no web tools). End with a one-line summary: the capability analyzed and whether a
thesis was written or the candidate deferred.
