#!/usr/bin/env python3
"""grounding_audit.py — source-grounding: the trust floor for an LLM-maintained KB (the trust
backbone). Conformance checks page STRUCTURE; this checks whether a synthesized claim page is
actually GROUNDED — does each entity/concept cite a source PAGE THAT EXISTS?

Deterministic (no_agent). For each in-scope page (entities + concepts by default; config
`GROUNDING_NAMESPACES`), excluding reference-catalog imports (CVE/ATT&CK etc. via the pack's
reference_policy — reference data, not claims needing a citation), classify:

  grounded   — >=1 `sources:` entry is a page-path AND resolves to an existing sources/ page
  dangling   — has source page-refs but NONE resolve (citation to a missing page)
  ungrounded — no resolving source at all (an unsupported assertion)

Writes wiki/dashboards/source-grounding.md (grounded % + ungrounded/dangling worklists). The deeper
claim↔source support check (does the source actually back the claim) is an LLM tier on top.

Env: WIKI_PATH (/opt/vault) · GROUNDING_NAMESPACES (entities,concepts) · GROUNDING_SAMPLES (30)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
DASH = WIKI / "dashboards" / "source-grounding.md"
NAMESPACES = [s.strip() for s in os.environ.get("GROUNDING_NAMESPACES", "entities,concepts").split(",") if s.strip()]
SAMPLES = int(os.environ.get("GROUNDING_SAMPLES", "30"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)


def _fm(p: Path) -> dict:
    try:
        import yaml
        m = _FM.match(p.read_text(encoding="utf-8", errors="replace")[:8000])
        return (schema_lib.fast_load(m.group(1)) or {}) if m else {}
    except Exception:
        return {}


def _stem(ref) -> str:
    return str(ref).strip().strip("[]").strip("/").split("/")[-1].lower().removesuffix(".md")


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    refpol = schema_lib.reference_policy(schema_lib.governing_schema(VAULT))
    sdir = WIKI / "sources"
    src_stems = set()
    if sdir.is_dir():
        for p in sdir.rglob("*.md"):
            if not (p.name.startswith(("_", "INDEX")) or p.name == "INDEX.md"):
                src_stems.add(p.stem.lower())

    in_scope = grounded = n_ung = n_dang = 0
    ung, dang = [], []
    for ns in NAMESPACES:
        base = WIKI / ns
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            if p.name.startswith(("_", ".")) or p.name == "INDEX.md" or p.name.startswith("INDEX-"):
                continue
            fm = _fm(p)
            if not fm or schema_lib.is_reference_page(fm, refpol):
                continue                       # catalog imports aren't claims needing a citation
            in_scope += 1
            rel = p.relative_to(WIKI).as_posix()[:-3]
            srcs = fm.get("sources")
            entries = srcs if isinstance(srcs, list) else ([srcs] if srcs else [])
            pagerefs = [e for e in entries if e is not None and schema_lib.is_page_ref(e)]
            if any(_stem(e) in src_stems for e in pagerefs):
                grounded += 1
            elif pagerefs:
                n_dang += 1
                if len(dang) < SAMPLES:
                    dang.append((rel, ", ".join(str(e)[:50] for e in pagerefs[:3])))
            else:
                n_ung += 1
                if len(ung) < SAMPLES:
                    prose = [str(e)[:40] for e in entries if e is not None and not schema_lib.is_page_ref(e)]
                    ung.append((rel, ("prose-only: " + ", ".join(prose[:3])) if prose else "no sources"))

    pct = (grounded / in_scope * 100) if in_scope else 100.0
    overall = "🟢 grounded" if pct >= 80 else ("🟡 partial" if pct >= 50 else "🔴 weak grounding")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Source grounding"', f"updated: {now}", "---", "",
         f"# Source grounding — {now}", "", f"**{overall} — {pct:.0f}%**", "",
         "_Does each synthesized entity/concept cite a source PAGE that exists? (Reference-catalog "
         "imports excluded — they're reference data, not claims.) The claim↔source support check is "
         "an LLM tier on top._", "",
         f"- in scope: **{in_scope}**  ·  🟢 grounded: **{grounded}** ({pct:.0f}%)  ·  "
         f"🔴 ungrounded: **{n_ung}**  ·  🟡 dangling: **{n_dang}**", ""]
    if ung:
        L += [f"## Ungrounded — no resolving source (showing {len(ung)} of {n_ung})", "",
              "| Page | Sources |", "|---|---|"] + [f"| [{r}]({r}.md) | {d} |" for r, d in ung] + [""]
    if dang:
        L += [f"## Dangling — cites a missing source page (showing {len(dang)} of {n_dang})", "",
              "| Page | Cited |", "|---|---|"] + [f"| [{r}]({r}.md) | {d} |" for r, d in dang] + [""]
    DASH.parent.mkdir(parents=True, exist_ok=True)
    DASH.write_text("\n".join(L), encoding="utf-8")
    print(f"grounding-audit: {grounded}/{in_scope} grounded ({pct:.0f}%), {n_ung} ungrounded, "
          f"{n_dang} dangling -> wiki/dashboards/source-grounding.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
