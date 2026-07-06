# okengine-cockpit

A standalone, **read-only** "intelligence cockpit" web reader for an OKEngine/OKF
vault. Where [`okengine-reader`](../okengine-reader/) gives you a generic browse
rail over the whole vault, the cockpit gives you a **function-oriented** surface:
a left **STREAM rail** of dated briefings plus function **TABS** —
briefings / dashboards / predictions, and an **optional** competitors / watchlist
tracker.

It is **domain-agnostic**. Every domain-specific surface — the display title, the
streams, the watchlist's tracked entity types / field names / labels, the curated
dashboard index, the competitor views — is driven by an **optional `cockpit:`
block** in the pack's `<vault>/schema.yaml`. On any OKF vault with **no `cockpit:`
block** it falls back to generic defaults (a "Recent briefings" stream over
`wiki/briefings/` plus the briefings / predictions / dashboards tabs); the
watchlist + competitors tabs stay hidden until a `watchlist:` config lights them
up.

Like the reader, it is deliberately **separate from Hermes**: it imports no hermes
modules, makes no gateway/dashboard calls, reads `<vault>/schema.yaml` directly
(only `yaml`), and serves only from a **read-only** mount of the vault — so it
keeps working even if the rest of the stack is down.

## What it does

- **Briefings** — a rail of dated streams; click a date to render the page
  (Obsidian `![[embeds]]` inlined, `[[wikilinks]]` click-through). PDF-companion
  streams (e.g. a weekly deck) render the PDF inline.
- **Dashboards** — a grid of the vault's `dashboards/` pages (auto-listed, or a
  pack-curated grouped reading order). Each opens in the page overlay.
- **Predictions** — a ledger computed from `prediction` frontmatter: status,
  confidence trajectory (sparkline), evidence direction, due-soon and idle flags,
  with filtering/sorting.
- **Watchlist** *(optional)* — a tracker over a configured entity namespace:
  a rating × tier matrix, recently-moved / gone-quiet tables, acquirer
  candidates, plus a concept "trends" tracker.
- **Competitors** *(optional)* — renders a pack-named set of generated dashboard
  pages.
- **Search** — ripgrep across the vault. **Backlinks** — "what links here" from
  the IWE wikilink graph. **Export** — any page as `md` / `docx` / `pdf`.

## The `cockpit:` config block

All keys are optional. Add this block to the pack's `schema.yaml`:

```yaml
cockpit:
  # Display title (topbar + browser tab). Default: titleized vault dir name.
  title: "Acme Intelligence"

  # The STREAM rail. Each stream selects dated pages by frontmatter `type`
  # (the okengine-normalized way) OR by filename `glob`; with neither, every
  # *.md in `dir` is used. `pdf: true` serves a same-stem <name>.pdf companion
  # (rendered inline). Default: one "Recent briefings" stream over briefings/.
  streams:
    - {key: pdb,    label: "Daily brief",   dir: briefings, type: daily-brief}
    - {key: weekly, label: "Weekly review", dir: weekly,    glob: "*-week-in-review.md"}
    - {key: deck,   label: "Weekly deck",   dir: briefings, glob: "weekly-deck-2*.md", pdf: true}

  # OPTIONAL tracker tab. If this block is ABSENT, the watchlist AND competitors
  # tabs are hidden no matter what `tabs:` says. All field names + labels are
  # supplied here — the engine hardcodes none.
  watchlist:
    entity_dir: entities              # namespace scanned (default: entities)
    entity_types: [vendor, product]   # which `type`s are tracked (empty/omit => all)
    tier_field: competitor_tier       # frontmatter field holding the tier
    rating_field: threat_level        # OPTIONAL high/medium/low rating field (adds a matrix + column)
    moved_field: last_material_move   # date field for "last move" (default: updated)
    acquirer_field: acquirer_candidate# OPTIONAL truthy flag => "Acquirer candidates" table
    labels:                           # all OPTIONAL, generic defaults shown in parens
      section: "Competitive watchlist"#   (Watchlist)
      entity:  "Competitor"           #   (Entity)
      tier:    "Tier"                 #   (Tier)
      rating:  "Threat"               #   (Rating)
      acquirers: "Acquirer candidates"#   (Acquirer candidates)
    trends:                           # concept tracker; default ON (concepts/type=trend).
      concept_dir: concepts           #   set `trends: false` to disable.
      type: trend

  # Pages rendered in the competitors tab (generated dashboards).
  competitors:
    - {key: movement, path: "dashboards/latest-competitor-movement-ledger.md"}

  # Prediction source namespace(s). Default: [predictions].
  predictions: [predictions]

  # Tab order. Default: [briefings, predictions, dashboards]. `watchlist` and
  # `competitors` here are dropped unless a `watchlist:` block exists.
  tabs: [briefings, watchlist, predictions, competitors, dashboards]

  # OPTIONAL curated dashboards grid (grouped reading order). Omit to auto-list
  # every page under wiki/dashboards/.
  dashboards:
    - group: "Today — read every morning"
      items:
        - {path: "dashboards/latest-pdb", title: "Daily PDB", desc: "what changed today"}
```

The config loader is `load_cockpit_config(vault: Path) -> dict` in `app.py` — a
pure function (no globals), unit-tested in
[`tests/test_cockpit_config.py`](../tests/test_cockpit_config.py). The running app
exposes the resolved title + visible tabs at `GET /api/config`, which the
frontend reads to build the shell.

## Run

The vault must be mounted **read-only** at `/vault` (wiki at `/vault/wiki`,
schema at `/vault/schema.yaml`).

```bash
docker build -t okengine-cockpit okengine-cockpit/

docker run --rm \
  -e VAULT_DIR=/vault \
  -v /path/to/vault:/vault:ro \
  -p 9200:9200 \
  okengine-cockpit
# open http://localhost:9200
```

For local dev without Docker:

```bash
pip install -r okengine-cockpit/requirements.txt   # + pandoc, ripgrep, iwe on PATH
VAULT_DIR=/path/to/vault uvicorn app:app --app-dir okengine-cockpit --port 9200
```

### Env

| var | default | meaning |
|-----|---------|---------|
| `VAULT_DIR` | `/vault` | read-only vault root (wiki at `$VAULT_DIR/wiki`) |
| `PORT` | `9200` | listen port |
| `IWE_BIN` | `iwe` | IWE binary for the backlink graph |

### Optional runtime tools

`pandoc` + `weasyprint` (docx/pdf export), `ripgrep` (search), and `iwe`
(backlinks) are baked into the image. Each endpoint degrades gracefully (503 /
empty) if its tool is missing, so the core read paths work without them.

## Domain-agnostic guarantee

The component ships **no domain knowledge** — no vendor/product names, no
deployment names, no domain vocabulary in its logic. Field names like
`competitor_tier` appear **only** in the test fixture as example **config
values**, never as hardcoded logic. Verify there are no domain literals in the
component logic (substitute your own vendor/product/deployment tokens):

```bash
grep -rinE 'competitor_tier|<your-product>|<your-deployment>' okengine-cockpit/app.py   # -> no matches
```
