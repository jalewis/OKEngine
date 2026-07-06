# Reader UI extension points (okengine#160)

How an extension gives its pages a richer reader view than generic markdown — **without shipping
renderer code into the reader**.

## The constraint that shapes the design

The reader (`okengine-reader/`) is a SEPARATE, domain-agnostic image: it ships no knowledge of any
pack or extension, sanitizes all markdown (nh3), and keeps working if the rest of the stack is down.
Letting a third-party extension inject arbitrary JS into it would break all three properties and
open an XSS/exfil surface (the #124 sandbox concern). So:

> **Decision: declarative panel KINDS, not extension code.** The reader ships a small library of
> generic, audited panel *kinds*. An extension only *binds* a page type to a kind and maps its
> frontmatter fields. No extension code runs in the reader → no sandbox needed for this path.

This mirrors how the rest of OKEngine works: extensions declare (schema fragments, capabilities,
cron ops); the engine/reader interpret. It's the same trade we made for schema and conformance.

## The contract (Phase 1 — shipped)

An extension declares bindings in its `extension.yaml`:

```yaml
reader_panels:
  - type: whitespace-thesis      # the page type this panel renders for
    kind: two-axis               # a built-in reader kind (fields | two-axis | timeline)
    x: demand_axis               # kind-specific field bindings
    y: maturity_axis
    label: title
  - type: prediction
    kind: fields                 # the simplest kind: a highlighted card of named fields
    fields: [confidence, status, resolves_by]
    title: Forecast
```

- **Validated** by `extension_manifest.validate_manifest` (`reader_panels` is a list of
  `{type, kind, …}`; `kind` must be a built-in; `fields` must be field names). Unknown kind-specific
  keys (`x`/`y`/`label`) are allowed.
- **Composed** by `extension_compose.collect_reader_panels(resolved)` → `{page_type: binding}` over
  the enabled extensions, **fail-loud** if two extensions bind the same type (no silent ambiguity).

## The flow (Phase 2 — implementation)

1. **Stage** the composed `{type: binding}` to `<vault>/.okengine/reader-panels.json` at deploy
   (alongside extension-script staging), so the reader picks it up from the vault it already mounts.
2. **Reader loads** the bindings (cached) and, in `/api/page`, attaches the matching binding +
   the page's field values: `panel = {kind, title, items:[{label,value}]}` (server extracts values
   from frontmatter — the client renders text/SVG, never extension code).
3. **Reader renders** the bound *kind* above the page body. Kind library, in order of need:
   - `fields` — a highlighted card of named frontmatter fields (reuses the existing meta-card style).
   - `two-axis` — an SVG scatter/positioning map (x/y from numeric or ordinal fields). **This is
     what #156 (`okengine.viz`, Wardley) needs** — build the kind there, against this contract.
   - `timeline` — events along a date axis.

A binding to an unknown kind degrades gracefully to plain markdown (forward-compatible: a newer
extension can name a kind an older reader lacks).

## Security

No third-party code in the reader (the whole point). Field *values* are extracted server-side and
rendered as text/known-SVG by audited kind renderers — same trust level as today's markdown render.
A future `custom` kind (arbitrary extension assets) would be a real sandbox decision and is **out of
scope**; it would gate on #124. The declarative kinds cover the near-term needs (viz, calibration,
timeline) without it.

## Status

- **Phase 1 (shipped):** the `reader_panels` manifest contract (validated) + the composer
  (`collect_reader_panels`). Extensions can declare bindings now; they're checked at validate time.
- **Phase 2:** staging + the reader loader + the kind renderers. Best built with **#156**, whose
  `two-axis` kind is the first concrete consumer.
