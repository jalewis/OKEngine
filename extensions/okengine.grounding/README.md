# okengine.grounding — semantic grounding (Tier-2)

The LLM layer of the grounding trust system. `grounding_audit` (engine, Tier-1) checks a claim cites
a source that *exists*; this lane reads each **grounded** entity/concept + its **cited sources** and
flags claims the sources don't *support* — appending a `## Grounding check` (supported / unsupported
/ not-found-in-source) and setting `grounding_checked`. Conservative (clear gaps only).

Wake-gated to grounded + recently-written + not-recently-checked pages (`select_grounding_check.py`).
Opt-in (model budget); domain-agnostic; no schema fragment. Route to a strong model (`@deepseek`).
