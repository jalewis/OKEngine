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

A tombstoned page (any type) is never listed in either kind: any tombstone is cold.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/cron, for sibling libs
import tz_lib  # noqa: E402
import schema_lib  # noqa: E402
from active_view_lib import read_frontmatter  # noqa: E402
# The engine-wide retirement predicate (`status: tombstoned`, any case) that tier_lib.tier_of
# pins its copy to. HOT.md excludes ANY tombstoned page, not only retired actors: tier_of calls
# every tombstone cold, so a retired source or concept listed as "recent" here contradicted
# `kb_search --tier` and the tier counts (okengine#994 [11]).
from okf_migrate import is_tombstoned  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"

# Fallback if the domain schema has no `hot_set` block. Its `open` section's open_values are
# replaced at run time by the COMPOSED tier block's (`tier.namespaces.<ns>.open_values`, see
# _hot_set_cfg), so HOT.md and tier_of agree about which pages are open. The literal here is used
# only when no tier block names the namespace, and mirrors config/base-schema.yaml. It used to add
# `proposed`/`pending`, which tier_of tiers by date: HOT.md listed a page `kb_search --tier hot`
# could call cold (okengine#1042 [37]).
_DEFAULT_HOT_SET = {
    "days": 30,
    "cap": 300,
    "sections": [
        {"kind": "open", "namespace": "predictions", "status_field": "status",
         "open_values": ["open", "active"],
         "secondary_field": "resolves_by", "title": "Open / active predictions"},
        {"kind": "recent", "namespace": "sources", "date_field": "published",
         "layout": "by-date", "title": "Recent sources"},
        {"kind": "recent", "namespace": "entities", "date_field": "updated",
         "show_type": True, "title": "Recently-updated entities"},
    ],
}


def _utc_today() -> date:
    """The run's UTC date: the ONE clock read that sets every window in HOT.md (okengine#1005).
    main() reads it once and threads it through, so a run straddling midnight cannot cut `recent`
    at one day and the `open` due-soon horizon at the next."""
    return datetime.now(timezone.utc).date()


def _hot_set_cfg() -> dict:
    sch = schema_lib.merged_schema(VAULT)
    cfg = sch["hot_set"] if isinstance(sch.get("hot_set"), dict) else _DEFAULT_HOT_SET
    return _with_tier_open_values(cfg, sch.get("tier"))


def _with_tier_open_values(cfg: dict, tier) -> dict:
    """`cfg` with each `open` section's status field and open values taken from the composed tier
    block for its namespace, when that block declares them (okengine#1042 [37]). One definition of
    "open" for HOT.md and tier_of, for the engine default AND a pack's own `hot_set`: a pack section
    used verbatim disagreed with tier_of as soon as either side was edited (okengine#1044 [7]/[12]).
    A pack section that declared another definition is reported on stderr, not silently replaced;
    its other keys (title, secondary field, due-soon horizon) are kept as written."""
    namespaces = tier.get("namespaces") if isinstance(tier, dict) else None
    sections = []
    # A pack's own hot_set may omit `sections`, a section's `kind`, or not be a mapping at all:
    # v0.15.13 ran those, and main() reports such a section DROPPED (okengine#326 [19]). They pass
    # through unaligned; bare indexing here crashed the cron before main() and HOT.md went stale.
    for sec in cfg.get("sections") or []:
        nscfg = (namespaces.get(sec.get("namespace"))
                 if isinstance(namespaces, dict) and isinstance(sec, dict) else None)
        if isinstance(nscfg, dict) and sec.get("kind") == "open" and nscfg.get("open_values"):
            aligned = {**sec, "status_field": nscfg.get("status_field", sec.get("status_field", "status")),
                       "open_values": _values(nscfg["open_values"])}
            _report_disagreement(sec, aligned, nscfg)
            sec = aligned
        sections.append(sec)
    return {**cfg, "sections": sections}


def _values(raw) -> list:
    """`open_values` as a list: a scalar (`open_values: active`) is ONE value. list() of a string
    split it into characters, so no page was open."""
    return list(raw) if isinstance(raw, (list, tuple, set, frozenset)) else [raw]


def _open_set(sec: dict) -> tuple[str, frozenset[str]]:
    """A section's definition of open as tier_of compares it: the field, lower-cased values."""
    raw = sec.get("open_values")
    return (str(sec.get("status_field", "status")),
            frozenset(str(v).lower() for v in (_values(raw) if raw else [])))


def _report_disagreement(declared: dict, aligned: dict, nscfg: dict) -> None:
    """Name an open section whose declared definition of open the tier block replaced, and a tier
    `open_floor` below hot: tier_of then calls the open pages HOT.md lists warm or cold. What HOT.md
    should show in that case is unsettled (okengine#1044), so it is reported, never silent."""
    ns = declared["namespace"]
    label = declared.get("title") or ns
    if "open_values" in declared and _open_set(declared) != _open_set(aligned):
        sys.stderr.write(
            f"build_hot_set: section {label!r} declares open as {_open_set(declared)[0]} in "
            f"{list(declared.get('open_values') or [])}, but tier.namespaces.{ns} (what tier_of and "
            f"kb_search --tier use) says {aligned['status_field']} in {aligned['open_values']}; "
            "HOT.md uses the tier block. Make the schema's hot_set agree with it.\n")
    floor = nscfg.get("open_floor", "hot")
    if floor != "hot":
        sys.stderr.write(
            f"build_hot_set: tier.namespaces.{ns} sets open_floor: {floor}, so tier_of does not "
            f"call the open pages section {label!r} lists hot.\n")


def _fm(p: Path) -> dict:
    """The page's whole frontmatter (active_view_lib.read_frontmatter: no leading-window cap)."""
    return read_frontmatter(p)


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
    # Optional status filter (e.g. recently *resolved* predictions = recent-by-graded-date
    # AND status in confirmed/refuted/partial). Absent -> all recent pages (unchanged).
    sf = sec.get("status_field")
    svals = {str(v).lower() for v in (sec.get("status_values") or [])}
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
        if is_tombstoned(fm):
            continue
        dv = _resolve_date(fm, df)
        if dv and dv >= cutoff:
            if svals and str(fm.get(sf) or "").lower() not in svals:
                continue
            rows.append((dv, p, fm))
    # Newest first, ties broken by path. The date alone left equal-date pages in readdir order, so
    # the cap kept a different set after a restore, rsync or sync (okengine#1042 [11]).
    rows.sort(key=lambda x: (-x[0].toordinal(), x[1].as_posix()))
    return rows


def _select_open(sec: dict, today: date | None = None) -> list[tuple[Path, dict]]:
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
        if is_tombstoned(fm):
            continue
        if str(fm.get(sf) or "").lower() in vals:
            rows.append((p, fm))
    # Every open page, soonest `secondary_field` date first (undated last), ties by path. Walk
    # order made the cap drop an arbitrary tail that changed with readdir order (okengine#1042
    # [11]); soonest-first keeps the most urgent pages when the section is capped.
    # Optional "due soon": keep only open pages whose `secondary_field` date is on/before
    # today+due_within_days — INCLUDES overdue (open past its date = needs grading).
    sec_field = sec.get("secondary_field")
    dated = sorted(((_d(fm.get(sec_field)) if sec_field else None, p, fm) for p, fm in rows),
                   key=lambda r: (r[0] is None, r[0] or date.min, r[1].as_posix()))
    due_within = sec.get("due_within_days")
    if due_within is not None and sec_field:
        horizon = (today or _utc_today()) + timedelta(days=int(due_within))
        dated = [r for r in dated if r[0] is not None and r[0] <= horizon]
    return [(p, fm) for _dv, p, fm in dated]


def _truncation_note(total: int, cap: int) -> list[str]:
    """The line that says a capped table is not the whole section. The header counts every row,
    so without it a 400-row section rendered 300 rows that read as complete (okengine#1042 [11])."""
    if total <= cap:
        return []
    return ["", f"_Showing {cap} of {total} (cap {cap}); {total - cap} not listed._"]


def main(today: date | None = None) -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    cfg = _hot_set_cfg()
    days = int(os.environ.get("HOT_DAYS") or cfg.get("days", 30))
    cap = int(cfg.get("cap", 300))
    today = today or _utc_today()   # injectable: a caller (or test) pins the run's date
    cutoff = today - timedelta(days=days)
    now = tz_lib.deployment_now().strftime("%Y-%m-%d %H:%M %Z")  # okengine#301: deployment zone, not "UTC"

    def rel(p: Path) -> str:
        return p.relative_to(WIKI).as_posix()[:-3]

    rendered: list[tuple[dict, list]] = []   # (section, selected rows) for counts + body
    for sec in cfg.get("sections", []):
        if not isinstance(sec, dict):
            sys.stderr.write(f"build_hot_set: section {sec!r} is not a mapping — DROPPED\n")
            continue
        if sec.get("kind") == "recent":
            rendered.append((sec, _select_recent(sec, cutoff)))
        elif sec.get("kind") == "open":
            rendered.append((sec, _select_open(sec, today)))
        else:
            # An unrecognized `kind` was SILENTLY DROPPED: the whole section (heading + rows) just
            # vanished from HOT.md — a typo'd kind, or a future kind that shipped ahead of the code,
            # degraded the load-first set with no signal. Fail loud on the cron's stderr (okengine#326
            # [19]); the run still completes (best-effort HOT.md), but the drop is now visible.
            sys.stderr.write(
                f"build_hot_set: section {sec.get('title') or sec.get('namespace') or sec!r} has "
                f"unrecognized kind {sec.get('kind')!r} (expected 'recent' or 'open') — DROPPED\n")

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
            L += _truncation_note(len(rows), cap)
        else:  # recent
            show_type = sec.get("show_type")
            show_status = sec.get("show_status")
            hdr_t = (" Type |" if show_type else "") + (" Status |" if show_status else "")
            L += ["", f"## {title} (≤{days}d)", "", f"| Date | {sec['namespace'].title()} |{hdr_t}",
                  "|---|---|" + ("---|" if show_type else "") + ("---|" if show_status else "")]
            for dv, p, fm in rows[:cap]:
                t = str(fm.get("title") or p.stem).replace("|", "\\|")[:80]
                extra = (f" {fm.get('type','')} |" if show_type else "") + \
                        (f" {fm.get(sec.get('status_field', 'status'), '')} |" if show_status else "")
                L.append(f"| {dv} | [{t}]({rel(p)}.md) |{extra}")
            L += _truncation_note(len(rows), cap)
    L.append("")
    (WIKI / "HOT.md").write_text("\n".join(L), encoding="utf-8")

    summary = ", ".join(f"{len(rows)} {s.get('title', s['namespace']).lower()}"
                        for s, rows in rendered)
    print(f"build-hot-set: {summary} -> wiki/HOT.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
