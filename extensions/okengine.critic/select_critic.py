#!/usr/bin/env python3
"""okengine.critic wake-gate — decide whether the flagship deliverable warrants a critique (#157).

COST LEVER (the whole point): this gate wakes the agent ONLY when it finds hard, structural flags
on the pack's flagship deliverable — staleness, thin body, or under-citation. No flags ⇒ silent,
zero spend. The agent (when woken) does the SUBJECTIVE critique the gate can't.

The flagship target is PACK config — `critic_flagship` in schema.yaml, a path or glob (e.g.
`briefings/**` or `dashboards/strategy.md`). No target declared ⇒ a clean no-op.

Prints a human digest of the flags found, then a final `{"wakeAgent": bool}` line (the cron-plus
wake-gate protocol). Self-contained (stdlib + yaml only).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
STALE_DAYS = int(os.environ.get("CRITIC_STALE_DAYS", "14"))
MIN_WORDS = int(os.environ.get("CRITIC_MIN_WORDS", "200"))
MIN_SOURCES = int(os.environ.get("CRITIC_MIN_SOURCES", "3"))

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", re.S)
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_SOURCE_LINK = re.compile(r"\[\[sources/[^\]]+\]\]")
_WORD = re.compile(r"[A-Za-z0-9']+")


def _is_generated(name: str) -> bool:
    """Generated index pages aren't authored deliverables — never critique them."""
    return name in ("INDEX.md", "index.md") or name.startswith(("INDEX-", "index-"))


def _schema() -> dict:
    for p in (VAULT / ".okengine" / "composed-schema.yaml", VAULT / "schema.yaml",
              WIKI / "schema.yaml"):
        if p.is_file():
            try:
                import yaml
                d = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    return {}


def _today() -> date:
    return date.fromisoformat(os.environ.get("OKENGINE_MCP_WRITE_DATE") or date.today().isoformat())


def _split(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        import yaml
        fm = yaml.safe_load(m.group(1))
    except Exception:
        fm = None
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _flags(md: Path) -> list[str]:
    try:
        text = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    fm, body = _split(text)
    flags = []
    # stale
    upd = fm.get("updated") or fm.get("created")
    m = _DATE.search(str(upd)) if upd else None
    if m:
        try:
            age = (_today() - date.fromisoformat(m.group(1))).days
            if age > STALE_DAYS:
                flags.append(f"stale ({age}d > {STALE_DAYS}d)")
        except ValueError:
            pass
    # thin
    words = len(_WORD.findall(body))
    if words < MIN_WORDS:
        flags.append(f"thin ({words} words < {MIN_WORDS})")
    # under-cited
    nsrc = len(set(_SOURCE_LINK.findall(body)))
    if nsrc < MIN_SOURCES:
        flags.append(f"under-cited ({nsrc} sources < {MIN_SOURCES})")
    return flags


def main() -> int:
    pattern = str(_schema().get("critic_flagship") or "").strip()
    if not pattern or not WIKI.is_dir():
        print("critic: no critic_flagship declared (or no vault) — nothing to critique")
        print(json.dumps({"wakeAgent": False}))
        return 0
    if pattern.endswith("**"):
        # Path.glob('**') matched DIRECTORIES ONLY before Python 3.13, so a pack pattern
        # like 'briefings/**' silently selected 0 pages on 3.11/3.12 runtimes and the gate
        # never woke. '**/*' matches files on every supported version.
        pattern += "/*"

    targets = sorted(p for p in WIKI.glob(pattern) if p.is_file() and p.suffix == ".md"
                     and not any(part.startswith((".", "_")) for part in p.parts)
                     and not _is_generated(p.name))
    print("=== critic flagship wake-gate ===")
    print(f"  flagship pattern: {pattern!r}  ·  matched: {len(targets)} page(s)")

    flagged = [(p, f) for p in targets for f in [_flags(p)] if f]
    for p, f in flagged:
        print(f"  ⚑ {p.relative_to(WIKI)}: {', '.join(f)}")

    if not flagged:
        print("  → SKIP: no hard flags on the flagship (cost lever — staying silent)")
        print(json.dumps({"wakeAgent": False}))
        return 0

    print(f"\n=== {len(flagged)} flagged flagship page(s) — critique them ===")
    print("Critique each flagged page below: assess claim support, coverage gaps, and the flagged "
          "structural issues. Write a critic report and flag weak pages for review.\n")
    for p, f in flagged:
        print(f"## {p.relative_to(WIKI).with_suffix('')}  ({', '.join(f)})")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
