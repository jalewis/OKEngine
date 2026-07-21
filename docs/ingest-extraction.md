# Raw document extraction (binary/markup → text companion → ingest)

For opt-in revision-aware capture of HTML and plain-text links discovered by
feeds, see [Revision-aware web capture](web-capture.md). The hard-format
extraction described here remains a separate downstream concern.

How non-plain-text source files (PDF, HTML, Word, PowerPoint) get turned into
agent-ingestible text. This is an
**engine-level** subsystem: domain-agnostic, knows nothing but "document in, text
out". Swapping the pack changes nothing here.

## The problem

The ingest agent reads **text**. Binary office formats (`.pdf`, `.docx`, `.pptx`)
aren't readable by the agent's `file_read` tools, and raw HTML is mostly
nav/ads/boilerplate. Handing either to an LLM is wasteful and lossy. So each
source is **pre-extracted to a plain-text `.txt` companion** that sits next to it,
and the raw selector prefers the companion.

## Pipeline

```
raw/**/<doc>.pdf|.html|.docx|.pptx        (source lands in raw/, any origin)
        │
        ▼  extractor  (host cron, every 15 min — scripts/extract-raw.sh)
raw/**/<doc>.<ext>.txt                     (text companion, written next to the source)
        │
        ▼  select_raw_batch.py             (wake-gate: prefers the companion, skips the source)
"## Files to ingest" digest
        │
        ▼  raw-backfill agent              (reads the .txt, writes a curated source page)
wiki/sources/<…>/<slug>.md
```

## The companion convention (the stable contract)

- Companion path = **`<source-name>.txt`** in the same directory
  (`report.pdf` → `report.pdf.txt`, `page.html` → `page.html.txt`).
- **Idempotent**: an extractor skips a file whose companion exists with an mtime
  **≥** the source's. Touch the source to force re-extraction.
- **Thin/empty output ⇒ no companion.** A scanned PDF with no text layer, or an
  HTML page that is boilerplate-only / JS-rendered, yields too little text; the
  extractor warns and writes nothing, so the selector never ingests an empty
  placeholder. (`extract-html.py` thresholds on `--min-chars`, default 200.)
- The companion suffix is `.txt`, already an ingestable leaf type, so it flows
  through the normal selector/ingest path with no special-casing.

## Extractors (what the engine ships)

| Format | Tool | Script | Status |
|---|---|---|---|
| `.pdf` | `pdftotext` (poppler-utils) | `scripts/extract-pdfs.sh` | shipped |
| `.html` `.htm` | trafilatura → readability → stdlib heuristic | `scripts/extract-html.py` | shipped |
| `.docx` `.pptx` | `python-docx` / `python-pptx` | `scripts/extract-docs.py` | shipped |
| `.xlsx` | `openpyxl` (non-empty cells per row/sheet) | `scripts/extract-docs.py` | shipped |
| `.rtf` | `striprtf` | `scripts/extract-docs.py` | shipped |
| `.doc` (legacy) | `antiword` / `catdoc` (host tool — no pure-python reader) | `scripts/extract-docs.py` | shipped |

`scripts/extract-raw.sh` runs every extractor under a single `flock` (the first
full pass can be long; runs never overlap). Each is idempotent, dry-runnable, and
corpus-safe (per-file failures are logged and skipped, never abort the batch); a
missing host tool/library (e.g. `pdftotext`, or `python-docx`/`python-pptx`) is a
warning, not a hard stop — that format is simply skipped this run.

<a name="docx--pptx"></a>
### Why python-docx/pptx (not pandoc/soffice)

`extract-docs.py` uses the Python OOXML libraries, **not** `pandoc` or `soffice`:
pandoc's plain/markdown writer drops table and text-box content, and
`soffice --headless` is unreliable when a LibreOffice GUI holds the single-instance
lock (empty conversions / `Io … Write` errors). `python-docx`/`python-pptx` read
the OOXML directly — no external process, no lock — and pull tables + speaker
notes. The libraries are an **optional dependency**: imported per-format, so a box
without them just skips `.docx`/`.pptx` (the script is a clean no-op if neither is
installed) rather than failing the wrapper.

## Selector integration

`scripts/cron/select_raw_batch.py` (the `raw-backfill` wake-gate) skips a raw
source when its companion exists:

```python
if p.suffix.lower() in (".pdf", ".html", ".htm", ".docx", ".pptx",
                        ".xlsx", ".rtf", ".doc") and \
        (p.parent / (p.name + ".txt")).is_file():
    continue
```

A source **without** a companion stays queued — the next extractor run creates
it; until then the agent would produce a low-value placeholder, so keeping
extraction ahead of ingest matters. Regression: `tests/cron/test_select_raw_batch.py`.

## Ingest is bounded and self-draining (first-run)

The first feed-fetch can drop a lot of files (e.g. 120). Ingestion is **bounded
per run and self-draining** — you don't babysit or kill it:

- **Bounded.** `select_raw_batch.py` selects at most **`BATCH_SIZE`** files per run
  (default 30; set it lower to take smaller bites). The digest states the bound
  explicitly: *"N of M unprocessed (bounded by `BATCH_SIZE=N`)"*.
- **Self-draining.** The `raw-backfill` cron processes the next batch each run and
  **stops waking the agent once 0 remain** (it prints "Backfill complete → pause
  the cron"). A large backlog just takes a few runs; nothing to stop manually.
- **Sources first, by design.** `raw-backfill` creates **source** pages only.
  Entities, concepts and predictions come from their **own** lanes
  (`entity-backfill` / `concept-backfill` / `prediction-*`), which run after
  sources exist — so a sources-only vault right after first ingest is *expected*.

To run **one bounded pass on demand** (e.g. first-run "ingest a batch, inspect,
repeat") instead of waiting for the schedule:

```sh
bash $ENGINE_DIR/scripts/cron-plus.sh list                  # find the raw-backfill job id
bash $ENGINE_DIR/scripts/cron-plus.sh run <raw-backfill-id> # fires once on the next tick (~60s)
```

## Scheduling: host cron, **not** cron-plus

The extractors run as a **host crontab** job, not a Hermes cron-plus job, for two
hard reasons rooted in the deployment topology:

1. **Ownership.** `raw/` is owned by the **host user**. The gateway container
   runs as a different (unprivileged) uid and **cannot write companions** into
   `raw/`. The job has to run as the host user that owns the tree.
2. **Tooling.** `pdftotext` (and the DOCX/PPTX libraries) live on the **host**;
   the gateway image ships none of them.

**The decision rule, generally:** *anything that writes to `raw/` or needs
host-only tools is a host-cron job; everything the gateway agent does in-vault is
cron-plus.* Host concerns keep their definition **in the repo** (not hand-typed on
the host), matching the engine's cron-as-code principle:

```
*/15 * * * * /…/okengine/scripts/extract-raw.sh >> ~/.okengine-extract-raw.log 2>&1
```

Install idempotently:

```bash
WIKI_PATH=/path/to/vault bash scripts/install-extract-cron.sh
```

The installer carries `WIKI_PATH` / `EXTRACT_PYTHON` into the cron environment so
the scheduled run resolves the same raw/ root and interpreter, greps for an
existing `extract-raw.sh` entry before appending (re-runnable, never duplicated),
and honours `SCHEDULE` to override the cadence. Default cadence is every 15 min —
cheap once caught up (companion mtime-skip) and stays ahead of the raw-backfill
selector.

## Dependencies (host)

```bash
# system tools:
apt-get install poppler-utils antiword        # pdftotext (.pdf) + legacy .doc (or catdoc)
# python deps (all OPTIONAL — each format is skipped if its dep is absent):
python3 -m pip install -r requirements-extract.txt
```

`requirements-extract.txt` is the host-side extractor dep list (python-docx,
python-pptx, openpyxl, striprtf, and optional trafilatura for better HTML).

## Boundary

Engine-level (this subsystem): the extractors, the wrapper, the installer, the
companion convention, and the companion-skip block in the raw selector.
Deployment-level (a pack author runs once on the host): `install-extract-cron.sh`
+ the host dependency list above. Pack-level: per-publisher/format extraction
tuning (e.g. an HTML `--selector` for a site the generic pass gets wrong) — see
[`docs/engine-domain-boundary.md`](engine-domain-boundary.md).
