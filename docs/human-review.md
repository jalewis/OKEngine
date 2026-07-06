# Human-in-the-loop review (#69)

An autonomous, LLM-maintained vault still needs a human on the high-stakes and low-trust pages.
This is the **queue + sign-off** loop (not just the `needs_review` flag).

## The queue
`review-queue` (a no_agent cron, `review_queue.py`) builds `wiki/dashboards/review-queue.md` — one
prioritized list of what needs a human, from:
1. **GROUNDING** — a page whose `## Grounding check` flagged an *unsupported* claim (Tier-2 found a
   possible falsehood). Highest priority.
2. **NEEDS-REVIEW** — `needs_review: true` (lacuna, write-path flags).
3. **UNVETTED** — a pack-declared high-stakes type (`schema.yaml review_required_types`, e.g.
   `briefing`, `prediction`) with no current sign-off.

## Sign-off
```
framework review <pack>                                  # show the queue
framework review <pack> --approve entities/a/acme --by "Jane"
```
Approval sets `reviewed_by` / `reviewed_on` **through the enforced MCP write path**
(`write_server._update` — validates, bumps version, logs) — never a bypass. A page is **vetted**
(off the queue) when `reviewed_on` ≥ its `last_updated`: signed off at the current version. Edit the
page later (the agent revises it) and `last_updated` moves past `reviewed_on`, so it **returns to the
queue for re-review** — sign-off is version-scoped, not permanent.

## Opt in
A pack declares which types require human sign-off:
```yaml
# <pack>/schema.yaml
review_required_types: [briefing, prediction, lacuna]
```
The GROUNDING and NEEDS-REVIEW sources are universal (no config).
