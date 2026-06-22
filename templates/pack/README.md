# okpack template — scaffold a new OKEngine domain pack

A starting skeleton + generator for a new **okpack-`<domain>`** — a domain pack the
OKEngine framework ingests feeds into and curates as a compounding knowledge vault.

> **Where this lives:** the OKEngine repo (`templates/pack/`) — the engine owns pack
> scaffolding, no individual pack does. It was distilled from the `okpack-sec`
> reference pack and moved here.
>
> **`skeleton/` is the single template.** Two front-ends render it, and only it:
> this bash generator (`new-pack.sh`, standalone) and the Python `framework init`
> (`scripts/framework_init.py`, wired into the `framework` CLI — also lays down the
> deploy-only `.hermes-data/config.yaml`). Neither carries its own copy of the pack
> files, so they can't drift. Edit the template here.

## What's here

```
pack-template/
  README.md               this file
  BUILDING-A-PACK.md      the authoring guide — the decisions a pack author makes
  PLACEHOLDERS.md         the {{TOKEN}} reference the skeleton + generator share
  new-pack.sh             generator: renders skeleton/ into a new pack dir
  skeleton/               the templated pack (every file uses {{TOKEN}} markers)
```

## Quickstart

```sh
# from wherever the template lives:
./new-pack.sh okpack-fin "finance threat & fraud vault" --offset 400 --engine v0.2.0
#            ^pack name   ^human title                   ^host port offset ^engine pin
```

That renders `./okpack-fin/` in your current directory (pass `--out` to choose where;
run it from where you want the pack — it refuses to render inside the template dir).
It fills `skeleton/`, substituting every
`{{TOKEN}}` (see `PLACEHOLDERS.md`). Then:

1. **Edit `schema.yaml`** — replace the example domain types with yours (this is the
   one genuinely domain-specific file). See `BUILDING-A-PACK.md` §Taxonomy.
2. **Edit `CLAUDE.md`** — write the domain voice + ingest rules (the `TODO:` markers).
3. **Edit `feeds/feeds.opml.example`** — your suggested sources (the active
   `feeds/feeds.opml` ships empty; operators copy entries in to opt in).
4. **`python3 validate.py`** — confirms the pack parses and is self-consistent.
5. **Deploy** — `git init`, then follow the new pack's `README.md` §Deploy.

## Manual (no generator)

Copy `skeleton/` to your new pack dir and replace the `{{TOKEN}}`s by hand (the table
in `PLACEHOLDERS.md` lists each). The generator is just sugar over that.
