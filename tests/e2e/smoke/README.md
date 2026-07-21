# Render-surface smoke harness

The unit suite validates each surface with **idealized fixtures**. Almost every bug that has
reached production was a *render / integration* bug on a **populated vault** — leaked wikilink
markup, a fact panel rendering above the body, a nested dashboard vanishing from the grid, a weekly
deck 404, a mis-resolved cross-namespace embed. A liveness probe returns a green `200` for every one
of those. Only assertions on the **actual rendered output** catch them.

This harness stands up the three read/render surfaces — **reader + cockpit + mcp** — over a small,
**frozen seeded vault** and asserts on what they actually produce. The surfaces are standalone
read-only over the vault, so there is **no gateway, no Hermes, no model, no feeds, no API keys** —
the run is deterministic and fast.

## Run it

```bash
make smoke-e2e                                   # build → up → assert → teardown
SMOKE_PYTHON=/path/to/venv/bin/python make smoke-e2e
bash tests/e2e/smoke/smoke-e2e.sh --keep         # leave the stack up (9880 reader / 9881 cockpit)
bash tests/e2e/smoke/smoke-e2e.sh --no-build     # reuse existing images
```

## Use it as the contributor sandbox

The same frozen, dependency-light stack is the one-command local sandbox:

```bash
make sandbox-start   # build, verify, then leave the stack running
make sandbox-stop    # stop it and remove the disposable qmd index
make sandbox-reset   # clean reset, rebuild, verify, and leave it running
```

After `sandbox-start`:

- reader: http://127.0.0.1:9880
- cockpit: http://127.0.0.1:9881
- read MCP: `http://127.0.0.1:8880/mcp`, token `okengine-local`

The fixture vault is intentionally read-only and tracked in Git, so every start uses known sample
content and `sandbox-reset` cannot destroy contributor data. The qmd index is the only persistent
sandbox state and is disposable. This is a surface-development/evaluation sandbox, not a miniature
production agent: it deliberately omits Hermes, models, feeds, credentials, delivery, and cron.
Use a scaffolded domain pack when testing write paths or scheduled agent behavior.

The venv needs **`pytest`**, and for the rendered-DOM layer **`playwright`** plus a system Chrome
(`channel="chrome"` — no browser download). Without playwright the DOM layer **skips** and the
HTTP/content layer still gates. It's a pre-release step (`docs/release-checklist.md` §2) and runs
on demand; it is intentionally *not* in the every-push `make check`.

For a release, use `SMOKE_REQUIRE_DOM=1 make smoke-e2e`; missing Playwright/Chrome is then a hard
failure. The harness tears down through an `EXIT` trap. If the host or terminal dies first, recover
before retrying:

```bash
docker compose -f tests/e2e/smoke/docker-compose.smoke.yml down -v --remove-orphans
```

## Layout

```
vault/                     the frozen seeded OKF vault (schema.yaml + wiki/) — the whole fixture
docker-compose.smoke.yml   reader + cockpit + mcp over ./vault:ro (loopback ports; no gateway)
smoke-e2e.sh               orchestrator: build, up, wait-healthy, run pytest, teardown
test_smoke_curl.py         HTTP/content layer — asserts on the returned HTML / JSON / PDF bytes
test_smoke_render.py       rendered-DOM layer — playwright + system Chrome (DOM/visual order)
```

## What each fixture reproduces (bug class → assertion)

| Fixture | Regression it guards | Layer |
|---|---|---|
| `entities/a/apt-smoke.md` (profiled actor + body) | fact panel must not render above the body ("scattered spider") | DOM |
| same, wikilink edge cases | resolvable/backtick-wrapped wikilinks → links with no `[[…]]`/backtick/`<a class=wl>` residue; a genuine code span stays literal | content + DOM |
| `dashboards/competitive/nested-smoke.md` | a nested, un-curated dashboard must appear in the grid's "Other" (M7) | content |
| `predictions/smoke-prediction.md` | a filed prediction renders a description, not a bare count | content |
| `briefings/weekly-deck-2026-01-01.md` | the pdf-enabled deck stream renders a real PDF on demand, not a 404 | content |
| `entities/e/etherrat.md` + `security-incidents/etherrat.md` | a bare-name embed present in two namespaces resolves to `entities/` (#16) | content |
| `lacuna/gap-{old,mid,new}.md` (`sorted` tab, date box) | a date-desc box shows NEWEST first — dates live in the sort's non-numeric bucket, direction must apply within it (Knowledge-gaps regression) ; mixes quoted-string + unquoted YAML date | content |
| `entities/j/junk-count-actor.md` (`sorted` tab, numeric box) | a legacy list-where-a-count-belongs value ranks BELOW every real number, never #1 (Most-active regression) | content |

## Companion: real-vault lints (idealized fixtures → real data)

This harness asserts on *fixtures*. The recurring lesson is that bugs hide in *real data*, so two
sibling lints point the same philosophy at a live deployment's actual vault:

- **`make render-lint READER_URL=…`** (`scripts/cron/render_lint.py`) — crawls every page through the
  reader's render path and flags rendered-output defects (leaked builder markup, literal `[[…]]`,
  broken embeds). Catches what a clean-fixture render test can't: a defect on stored content.
- **`make content-lint VAULT=…`** (`scripts/cron/content_lint.py`) — reads the source markdown and flags
  *degenerate generations* (repetition-loop word-salad (comma/wikilink-aware, precise on multilingual vaults)) that render a
  clean `200` and so slip past every render/HTTP check. Tuned for precision — verbose-but-coherent
  prose is not flagged.

Both run on demand and are wireable as crons (`--write-vault` → `wiki/operational/{render,content}-lint.md`,
non-zero exit on regression). The smoke suite runs each over the seeded vault as an always-green
fixture-integrity check.

## Adding a case

When a render/integration bug is found and fixed, add its shape here so it can't silently return:

1. Author the smallest page(s) under `vault/wiki/` that reproduce the shape, with a distinctive
   `SENTINEL` string in the body.
2. Add an assertion — `test_smoke_curl.py` if it's visible in the returned HTML/JSON/PDF, or
   `test_smoke_render.py` if it only manifests once the browser assembles the page (DOM order,
   visual position, SPA-assembled markup).
3. Confirm it's **red before the fix, green after** — the same discipline as a unit regression test.

The vault is bind-mounted read-only, so edits to `vault/` are picked up live against a running
stack (`--keep`) with no rebuild.
