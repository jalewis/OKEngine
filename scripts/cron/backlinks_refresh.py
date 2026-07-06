#!/usr/bin/env python3
"""backlinks-refresh — precompute the backlink graph into wiki/.backlinks.json.

Engine cron, no_agent (okengine#168). Builds the inverted, filtered, titled
{target: [{key,title}]} map ONCE per deployment per day and writes it as a static
artifact; the reader and cockpit serve it directly (instant dict load on their :ro
vault mount) instead of rebuilding the graph inside the UI containers. UIs fall back
to a live build only if this artifact is absent or older than their acceptance
ceiling (default 48h), so a deployment without this cron — or one whose cron stopped
— still works.

The graph is built by backlink_lib.scan_forward_refs — a direct markdown link scan
over the NON-excluded tree (okengine#179). It REPLACES the former `iwe find -f json
-l 0` full-graph dump, which parsed the whole vault (~4GB RSS / ~550s on a 52k-file
vault, right at this lane's old 600s timeout, and OOM-prone at the 3GB gateway cap).
The scan is ~38x faster / ~40x lighter at 99.99% edge parity — see backlink_lib.

Vault resolution: WIKI_PATH env (the standard every other cron script uses, default
/opt/vault) — NOT cwd, because cron-plus runs no_agent scripts from the scripts dir,
not the declared workdir (okengine#168 follow-up: this lane used to resolve from
os.getcwd() and so failed 'no wiki/' on every scheduled run while passing manual
`-w /opt/vault` tests). VAULT_DIR still overrides for ad-hoc runs; cwd is the
last-resort fallback. Pure script: prints a one-line summary, exits non-zero on
failure so the run log shows red instead of silently serving a stale graph.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlink_lib  # noqa: E402


def _resolve_vault() -> Path:
    """Vault root, in precedence order: VAULT_DIR (explicit override) → WIKI_PATH (the
    standard cron env, default /opt/vault) → cwd (last-resort for a manual run from the
    vault root). Deliberately NOT cwd-first: cron-plus invokes no_agent scripts from the
    scripts dir, so a cwd-first resolution never finds the vault under the scheduler."""
    return Path(os.environ.get("VAULT_DIR")
                or os.environ.get("WIKI_PATH")
                or os.getcwd()).resolve()


def main() -> int:
    vault = _resolve_vault()
    wiki = vault / "wiki"
    if not wiki.is_dir():
        print(f"ERROR: no wiki/ under {vault} (set VAULT_DIR or run from the vault root)",
              file=sys.stderr)
        return 2

    t0 = time.time()
    excluded = backlink_lib.excluded_top_dirs(vault)
    docs = backlink_lib.scan_forward_refs(wiki, excluded)
    if not docs:
        # no readable non-excluded docs over a populated vault is a malfunction, not a
        # result — refuse to clobber a good artifact with an empty one.
        print("ERROR: scan found no documents; keeping the existing artifact", file=sys.stderr)
        return 2

    artifact = backlink_lib.build_artifact(docs, wiki, vault, built_at=time.time())
    out = backlink_lib.write_artifact(artifact, wiki)
    print(f"backlinks-refresh: {artifact['pages']} pages -> "
          f"{artifact['targets']} targets / {artifact['edges']} edges "
          f"-> {out} ({time.time() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
