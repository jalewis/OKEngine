#!/usr/bin/env python3
"""tier_lib — derived hot / warm / cold tiering (OKF conformance G4, engine).

Tier is COMPUTED, never stored — matching build_hot_set's philosophy: a page's
tier follows its recency, so it self-promotes/demotes as content ages with ZERO
corpus churn (no frontmatter writes across a 46k-file corpus). The `sources/`
by-date hierarchy already encodes recency; predictions carry open/resolved status.

Config: the domain-pack `schema.yaml` `tier:` block (thresholds + per-namespace
recency source). Consumers:
  - tier_refresh.py  — the no_agent cron that reports the live distribution
  - kb_search.py / okengine-mcp `search`  — the `--tier` / `tier=` retrieval filter

A page's namespace is its first wiki-relative path segment. Namespaces absent
from the config are UNTIERED (tier_of -> None): the filter leaves them in.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# Fallback if the pack schema.yaml has no `tier:` block.
# MUST mirror config/base-schema.yaml's `tier:` block — it is only the schema_lib-UNAVAILABLE
# fallback (load_cfg composes the real one). It used to list extra open_values (proposed/pending) and
# only 4 namespaces, disagreeing with the base⊕pack composer everyone else consumes (pred_lib /
# select_daily_brief / the write path / the cockpit): a `proposed` prediction read OPEN here but not
# there (invariant-audit #51). Keep it byte-for-byte aligned with base-schema.
_DEFAULT_TIER = {
    "hot_days": 30,
    "warm_days": 365,
    "namespaces": {
        "sources": {"date_field": "published", "from_path": True},
        "entities": {"date_field": "updated"},
        "concepts": {"date_field": "updated"},
        "predictions": {"date_field": "resolves_by", "status_field": "status",
                        "open_values": ["open", "active"], "open_floor": "hot"},
        "findings": {"date_field": "updated"},
        "briefings": {"date_field": "published"},
        "trends": {"date_field": "updated"},
    },
}

_TIERS = ("hot", "warm", "cold")


def load_cfg(vault: Path) -> dict:
    """The COMPOSED `tier:` block (engine base-schema ⊕ pack schema.yaml), read through the SAME
    composer select_daily_brief / pred_lib.OPEN_VALUES / the write path / the cockpit consume
    (schema_lib.merged_schema) — so a pack that OMITS `tier:` inherits the engine-core tier instead of
    a divergent hardcoded default (a page 'open'/tiered here but not there, invariant-audit #51). Falls
    back to reading the raw pack `tier:` (then _DEFAULT_TIER) only if schema_lib is unavailable."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import schema_lib
        tier = (schema_lib.merged_schema(Path(vault)) or {}).get("tier")
        if isinstance(tier, dict) and tier:
            return tier
    except Exception:
        pass
    if yaml is not None:
        sp = Path(vault) / "schema.yaml"
        if sp.is_file():
            try:
                sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
                if isinstance(sch.get("tier"), dict):
                    return sch["tier"]
            except Exception:
                pass
    return _DEFAULT_TIER


def _parse_date(v) -> Optional[date]:
    if not v:
        return None
    m = _DATE_RE.match(str(v))
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def _date_from_path(parts: list[str]) -> Optional[date]:
    """Derive a date from a by-date path: <ns>/<YYYY>/<MM>[/<DD>]/<slug>.md."""
    nums = []
    for seg in parts[1:]:
        if seg.isdigit():
            nums.append(int(seg))
        else:
            break
    if len(nums) < 2:
        return None
    y, mo = nums[0], nums[1]
    d = nums[2] if len(nums) >= 3 else 1
    for day in (d, 1):
        try:
            return date(y, mo, day)
        except ValueError:
            continue
    return None


def fm_of(abs_path: Path) -> dict:
    """Parse a page's frontmatter (best-effort; {} on any failure)."""
    if yaml is None:
        return {}
    try:
        m = _FM_RE.match(Path(abs_path).read_text(encoding="utf-8", errors="replace")[:4000])
    except OSError:
        return {}
    if not m:
        return {}
    try:
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def tier_of(rel_path: str, fm: Optional[dict], cfg: dict, today: date) -> Optional[str]:
    """Return 'hot'|'warm'|'cold' for a wiki-relative page path, or None if the
    page's namespace is not tiered. `fm` may be None for from_path namespaces."""
    parts = rel_path.split("/")
    if len(parts) < 2:
        return None
    nscfg = (cfg.get("namespaces") or {}).get(parts[0])
    if not nscfg:
        return None
    fm = fm or {}
    sf = nscfg.get("status_field")
    if sf:
        ov = {str(v).lower() for v in (nscfg.get("open_values") or [])}
        if str(fm.get(sf) or "").lower() in ov:
            return nscfg.get("open_floor", "hot")
    if nscfg.get("from_path"):
        d = _date_from_path(parts)
    else:
        # configured date_field, falling back to the OKF envelope last_updated/created when
        # absent — entities/concepts carry only the envelope date, so a `date_field: updated`
        # would otherwise tier every page cold though it's freshly written (okengine#116).
        d = None
        for _k in (nscfg.get("date_field", "updated"), "last_updated", "created"):
            if _k:
                d = _parse_date(fm.get(_k))
                if d:
                    break
    if d is None:
        return "cold"
    age = (today - d).days
    if age <= int(cfg.get("hot_days", 30)):
        return "hot"
    if age <= int(cfg.get("warm_days", 365)):
        return "warm"
    return "cold"


def tier_of_file(abs_path: Path, wiki: Path, cfg: dict, today: date) -> Optional[str]:
    """tier_of for an absolute page path under `wiki` (reads fm only when needed)."""
    try:
        rel = Path(abs_path).resolve().relative_to(Path(wiki).resolve()).as_posix()
    except (ValueError, OSError):
        return None
    parts = rel.split("/")
    nscfg = (cfg.get("namespaces") or {}).get(parts[0] if parts else "")
    if not nscfg:
        return None
    fm = {} if nscfg.get("from_path") and not nscfg.get("status_field") else fm_of(abs_path)
    return tier_of(rel, fm, cfg, today)
