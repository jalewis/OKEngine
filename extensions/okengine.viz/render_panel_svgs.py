#!/usr/bin/env python3
"""render_panel_svgs.py — okengine.viz: embed server-rendered SVG into any dashboards
page carrying `panel: {kind: two-axis}` frontmatter. no_agent, deterministic, idempotent.

The bridge that lets AGENT lanes (e.g. competitive-analytics quadrants) emit only DATA:
the agent writes the panel frontmatter; this drain renders the chart into the body
between `<!-- panel-svg v=<hash> -->` markers, so it shows anywhere markdown renders
(the origin-system wardley approach). Unchanged panel data (same hash) -> page untouched.

Env: WIKI_PATH (default /opt/vault)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel_svg    # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)


def main() -> int:
    base = WIKI / "dashboards"
    if not base.is_dir():
        print("render-panel-svgs: no dashboards/ — nothing to do")
        print(json.dumps({"wakeAgent": False}))
        return 0
    import yaml
    rendered = current = 0
    for p in sorted(base.rglob("*.md")):
        if p.name.startswith(("_", "INDEX")):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        m = _FM.match(text)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        panel = fm.get("panel")
        if not (isinstance(panel, dict) and panel.get("kind") == "two-axis"):
            continue
        body = text[m.end():]
        new_body = panel_svg.upsert_block(body, panel)
        if new_body is None:
            current += 1
            continue
        p.write_text(text[:m.end()] + new_body, encoding="utf-8")
        rendered += 1
        print(f"render-panel-svgs: embedded chart -> {p.relative_to(WIKI)}")
    print(f"render-panel-svgs: {rendered} rendered, {current} already current")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
