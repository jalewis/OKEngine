# Engine-shipped extensions (tier-1 discovery root)

This directory is the **tier-1 extension root** (`docs/design/discovery-spec.md` §3.1):
first-party `okengine.*` operations the engine ships, discovered without copying engine
code into every vault.

Layout — one directory per extension, keyed by its manifest `id`:

```
extensions/
  okengine.contradictions/
    extension.yaml          # the manifest (docs/design/extension-system.md §6)
    ...                     # the operation's code / schema fragment
```

Discovery is **presence-based** (an `extension.yaml` makes a dir an extension) and
**discovered ≠ enabled** — nothing here runs until an operator enables it in a pack's
`<pack>/.okengine/extensions.yaml`. The other two tiers are `<pack>/extensions/` (pack)
and `<pack>/.okengine/extensions/` (operator/private).

Rules enforced by the scanner (`scripts/extension_discovery.py`):

- an `id` may appear in **at most one tier** — a duplicate across tiers is a hard FAIL
  (no shadowing);
- the `okengine.*` namespace is **reserved to this tier**.

Inspect with `framework extensions list|inspect|validate <pack>`. The tier now
ships a fleet of first-party extensions (see the sibling directories);
`okengine.contradictions` (the §11 first slice) landed first.
