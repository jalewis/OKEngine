# Changelog

Notable changes to the OKEngine layer. Versions track `engine_release` in
`engine-manifest.yaml` (and `pyproject.toml`). Issue refs are `okengine#NN`.

## v0.3.3

Closes the recurring **split-brain vault** class, adds the cold-start **kickstart**, and makes
the **reader** surface the vault's synthesized value after install.

### Fixed
- **Split-brain vault (root cause).** `terminal.cwd` was the vault root, not the page tree, so a
  stray relative `file_write` (e.g. `source/x`) forked content out of the canonical
  `<vault>/wiki` tree that the MCP write path + every dashboard use; `WIKI_PATH=/opt/wiki` also
  doubled the tree to `/opt/wiki/wiki`. `cwd` now points at the page tree, and `framework
  validate` fails any pack whose `WIKI_PATH` ends in `wiki`. (#110)
- **Write path enforces `type → namespace`.** `create_entity` rejects a write to a namespace not
  declared in `schema.yaml` (with the closest declared namespace as a hint), so a `type: source`
  page can no longer drift into a stray `source/` instead of `sources/`. (#115)
- **Hot-set / tier date resolution** falls back from the configured `date_field` to the OKF
  envelope `last_updated`, then `created` — entities/concepts (which carry only `last_updated`)
  no longer vanish from the hot set or tier cold. (#116)
- **Kickstart completion** polls `last_run_success`, not `last_run_at` (which advances when the
  selector fires), so agent lanes aren't reported done while their compile is still running. (#114)
- **Engine-version pin check** is patch-tolerant — a `v0.3.0`-pinned pack is satisfied by a
  `v0.3.x` engine. (#104)

### Added
- **Kickstart** — opt-in `deploy.sh --kickstart` populates a freshly-deployed vault now, walking
  the full build/maintenance fleet in dependency order (ingest → compile → score → entities →
  schema/repair → graph → concepts → canonical → predictions → quality → index/dashboards →
  brief) instead of waiting hours/days for the schedule. (#109)
- **Reader surfaces synthesized value.** `dashboards/` (the brief + digests) is now browsable —
  schema `exclude:` scopes conformance, not reader visibility — and the reader lands on the
  curated HOT set instead of an empty rail (brand = Home). `operational/` stays hidden. (#117)

### Deploy / cron
- Generate a real `OKENGINE_MCP_TOKEN` on fresh deploy. (#105)
- Post-deploy verify reads the runtime config at `/opt/data`. (#106)
- Expand engine-cron `@jitter` sentinels at deploy (cron-plus can't parse a raw sentinel). (#107)
- Scope the gateway to the pack's compose project on multi-pack hosts. (#108)
- `$(id -u)` default-uid model propagated across the deploy guides. (#102)

## v0.3.2
- Cron jitter never picks minute 0 — no herd-prone `:00` schedule that fails validation. (#103)

## v0.3.1
- `framework budget --resume` (manual spend-cap recovery) (#97); default image tag tracks
  `engine_release` rather than a hardcoded literal (#101); default `HERMES_UID`/`GID` to the
  invoking user's uid (#102).

## v0.3.0
- Identity layer (id-aware create/converge) + reader/permissions fixes + pullable
  (squashed-continuous) public-snapshot publishing; skeleton entity-path fix.

## v0.2.0
- Normative OKF conformance profile + strict fail-closed validator (#22/#27); cron-plus promoted
  to a first-class pinned dependency.

## v0.1.0
- Initial OKEngine layer extracted from the pinned Hermes-Agent.
