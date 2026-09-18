# Python package migration and rollback

OKEngine revision `0.13.7` establishes `src/okengine` as the stable Python
boundary. A release builds exactly one `okengine-<version>-py3-none-any.whl`;
all engine-owned runtime images install a wheel built from the same revision.
The pinned Hermes gateway assembly also builds and installs that revision's
wheel before producing its immutable release-plus-git-SHA image; installation
never occurs at container startup.

## Migration order

1. Call framework automation through `okengine-framework` and cron programs
   through `okengine-cron <module>`.
2. Move high-risk shared state, schema, and transaction modules into
   `src/okengine`, preserving console names and tests.
3. Replace each service's reviewed compatibility import with a package import,
   then delete its entry from `scripts/audit/import_boundary.py`.
4. Remove `okengine.compat` after the final legacy command migrates.

The operations framework, runner, and projection builder are canonical package
modules; their old `scripts/` paths are import aliases for command compatibility.
The MCP write service resolves its validator, policy, transaction, identity,
schema, and convergence libraries from the wheel rather than manipulating
`sys.path` to choose between baked and staged copies. The import-boundary audit
now rejects every `sys.path`, file-location, or run-path loader in runtime code.

## Rollback

Pin the prior engine revision and rebuild the deployment images. Never mix a
wheel from one revision with source or images from another. During the bridge
period, `OKENGINE_SOURCE_ROOT` may point console entry points at the matching
checked-out revision; unset it once the invoked command is packaged. Verify a
rollback with `okengine-framework validate <deployment>` and the normal smoke
suite before restoring traffic.
