#!/usr/bin/env python3
"""build-hot-set — the agent's "load-first" working set (OKF Phase 5 / S4).

The guide's hot/warm/cold tiering keeps the agent's working set small at scale.
Tier is DERIVED, not stored: the sources/{year}/{month}/ hierarchy is already a
recency axis and predictions carry open/resolved status, so storing a tier tag
would churn the corpus as content ages for no gain. Instead this generates a
single always-fresh HOT.md listing the current working set.

Config-driven (M2): the sections come from the domain-pack `schema.yaml`
`hot_set` block (which namespaces / fields are "hot"), not hardcoded. A domain
declares its own; absent -> the engine defaults below. Section kinds:
  - recent : pages whose `date_field` is within `days`. Found by a recursive walk
             keyed on the frontmatter date, so ANY source layout works (flat,
             `<yr>/<mo>/`, or feed packs' `<publisher>/<yr>/<mo>/<day>/`); a cheap
             path-date check skips wholly-old date shards. `layout` is now ignored.
  - open   : pages whose `status_field` value is in `open_values`

An agent loads HOT.md first to get the live picture without scanning the corpus.
Regenerated daily; gitignored (deterministic). Pure script / no_agent.

Env: WIKI_PATH (default /opt/vault) · HOT_DAYS (override schema `days`)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)

# Fallback if the domain schema has no `hot_set` block (preserves prior behavior).
_DEFAULT_HOT_SET = {
    "days": 30,
    "cap": 300,
    "sections": [
        {"kind": "open", "namespace": "predictions", "status_field": "status",
         "open_values": ["open", "active", "proposed", "pending"],
         "secondary_field": "resolves_by", "title": "Open / active predictions"},
        {"kind": "recent", "namespace": "sources", "date_field": "published",
         "layout": "by-date", "title": "Recent sources"},
        {"kind": "recent", "namespace": "entities", "date_field": "updated",
         "show_type": True, "title": "Recently-updated entities"},
    ],
}


def _hot_set_cfg() -> dict:
    sp = VAULT / "schema.yaml"
    if sp.is_file():
        try:
            sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            if isinstance(sch.get("hot_set"), dict):
                return sch["hot_set"]
        except Exception:
            pass
    return _DEFAULT_HOT_SET


def _fm(p: Path) -> dict:
    try:
        m = _FM_RE.match(p.read_text(encoding="utf-8", errors="replace")[:4000])
    except OSError:
        return {}
    if not m:
        return {}
    try:
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _d(v) -> date | None:
    if not v:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def _resolve_date(fm: dict, df: str) -> date | None:
    """The configured `date_field`, falling back to the OKF envelope `last_updated` then
    `created` when it's absent/unparseable. Entities & concepts usually carry only the
    envelope `last_updated` (the agent never sets a domain `updated`), so a schema
    `date_field: updated` would otherwise silently drop EVERY page from the hot set —
    the dashboard reads 0 though the namespace is full (okengine#116)."""
    for key in (df, "last_updated", "created"):
        if key:
            d = _d(fm.get(key))
            if d:
                return d
    return None


def _path_upper_date(rel: str) -> date | None:
    """Latest date a date-sharded PATH could represent (`…/YYYY/MM[/DD]/…`), so an
    old shard can be skipped WITHOUT reading frontmatter. Directory components only
    — filename numbers are never treated as dates. Returns the *upper bound* (end of
    month when no day) so a recent file is never wrongly skipped. None when the path
    carries no date hierarchy (publisher/<slug>, flat, …) — then frontmatter decides.

    Layout-agnostic: handles `sources/<yr>/<mo>/`, `sources/<pub>/<yr>/<mo>/<day>/`
    (feed packs, #24), and anything else."""
    parts = rel.split("/")[:-1]   # directories only, excluding the filename
    for i in range(len(parts) - 1):
        if (re.fullmatch(r"\d{4}", parts[i]) and 2000 <= int(parts[i]) <= 2100
                and re.fullmatch(r"\d{2}", parts[i + 1]) and 1 <= int(parts[i + 1]) <= 12):
            y, mo = int(parts[i]), int(parts[i + 1])
            if (i + 2 < len(parts) and re.fullmatch(r"\d{2}", parts[i + 2])
                    and 1 <= int(parts[i + 2]) <= 31):
                try:
                    return date(y, mo, int(parts[i + 2]))      # day granularity: exact
                except ValueError:
                    pass
            nxt = date(y + (mo == 12), (mo % 12) + 1, 1)        # month granularity:
            return nxt - timedelta(days=1)                      #   last day of month
    return None


def _select_recent(sec: dict, cutoff: date) -> list[tuple[date, Path, dict]]:
    """Recent pages by their frontmatter `date_field` (authoritative), found via a
    recursive walk so any source layout works — flat, `<yr>/<mo>/`, or the
    `<publisher>/<yr>/<mo>/<day>/` nesting feed packs use (#24)."""
    ns = sec["namespace"]
    df = sec.get("date_field", "published")
    base = WIKI / ns
    if not base.is_dir():
        return []
    rows = []
    for p in base.rglob("*.md"):
        if p.name == "INDEX.md" or p.name.startswith(("_", "INDEX-")):
            continue
        up = _path_upper_date(p.relative_to(base).as_posix())
        if up is not None and up < cutoff:
            continue   # date-sharded into a shard wholly before the cutoff — skip cheap
        fm = _fm(p)
        dv = _resolve_date(fm, df)
        if dv and dv >= cutoff:
            rows.append((dv, p, fm))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows


def _select_open(sec: dict) -> list[tuple[Path, dict]]:
    base = WIKI / sec["namespace"]
    if not base.is_dir():
        return []
    sf = sec.get("status_field", "status")
    vals = {str(v).lower() for v in (sec.get("open_values") or [])}
    rows = []
    for p in base.rglob("*.md"):
        if p.name == "INDEX.md":
            continue
        fm = _fm(p)
        if str(fm.get(sf) or "").lower() in vals:
            rows.append((p, fm))
    return rows


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    cfg = _hot_set_cfg()
    days = int(os.environ.get("HOT_DAYS") or cfg.get("days", 30))
    cap = int(cfg.get("cap", 300))
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def rel(p: Path) -> str:
        return p.relative_to(WIKI).as_posix()[:-3]

    rendered: list[tuple[dict, list]] = []   # (section, selected rows) for counts + body
    for sec in cfg.get("sections", []):
        if sec.get("kind") == "recent":
            rendered.append((sec, _select_recent(sec, cutoff)))
        elif sec.get("kind") == "open":
            rendered.append((sec, _select_open(sec)))

    counts = "  ·  ".join(f"{s.get('title', s['namespace'])}: **{len(rows)}**"
                          for s, rows in rendered)
    L = ["---", "type: dashboard", 'title: "Hot Set — current working set"', "---", "",
         f"# Hot Set — {now}", "",
         f"_Derived working set (last {days}d + live). Load this first._", "",
         f"- {counts}", ""]

    for sec, rows in rendered:
        title = sec.get("title", sec["namespace"])
        if sec.get("kind") == "open":
            sf2 = sec.get("secondary_field")
            hdr2 = f" {sf2.replace('_', ' ').title()} |" if sf2 else ""
            L += ["", f"## {title}", "", f"| {sec['namespace'].title()} | Status |{hdr2}",
                  "|---|---|" + ("---|" if sf2 else "")]
            for p, fm in rows[:cap]:
                extra = f" {fm.get(sf2, '')} |" if sf2 else ""
                L.append(f"| [{p.stem}]({rel(p)}.md) | {fm.get(sec.get('status_field','status'),'')} |{extra}")
        else:  # recent
            show_type = sec.get("show_type")
            hdr_t = " Type |" if show_type else ""
            L += ["", f"## {title} (≤{days}d)", "", f"| Date | {sec['namespace'].title()} |{hdr_t}",
                  "|---|---|" + ("---|" if show_type else "")]
            for dv, p, fm in rows[:cap]:
                t = str(fm.get("title") or p.stem).replace("|", "\\|")[:80]
                extra = f" {fm.get('type','')} |" if show_type else ""
                L.append(f"| {dv} | [{t}]({rel(p)}.md) |{extra}")
    L.append("")
    (WIKI / "HOT.md").write_text("\n".join(L), encoding="utf-8")

    summary = ", ".join(f"{len(rows)} {s.get('title', s['namespace']).lower()}"
                        for s, rows in rendered)
    print(f"build-hot-set: {summary} -> wiki/HOT.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
