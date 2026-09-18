from __future__ import annotations
# ruff: noqa: F821

import contextvars
import datetime
import difflib
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import fcntl
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional, Union, cast

import yaml
from starlette.requests import Request as StarletteRequest

from tools.schema_validator import (
    schema_reject_reason,
    governing_policy,
    drift_policy,
    canonicalize_enum_case,
)
from tools import policy_plane
from okengine.mcp import scope as _scope
import output_contract_enforce as _output_contract

import id_lib, schema_lib, id_index, converge, okf_migrate
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")


def _atomic_write_text(path: Path, content: str) -> None:
    """Durably publish UTF-8 text without exposing a partial destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            tmp = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _utf8_refusal(path: Path) -> str:
    """Return an explicit refusal and queue corrupt bytes for human repair."""
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        reason = (
            f"invalid UTF-8 at byte {exc.start}; write refused to preserve the original bytes"
        )
        _append_review_queue_once(path, reason)
        _append_log(f"- {_today()} mcp-write integrity-refusal {_rel(path)} — {reason}")
        return f"refused: {_rel(path)} contains {reason}"
    return ""


def _future_date_reject(fm: dict, fields=_RECORD_DATE_FIELDS) -> Optional[str]:
    try:
        today = datetime.date.fromisoformat(_today())
    except ValueError:  # unparseable test override — never block writes on guard plumbing
        return None
    limit = today + datetime.timedelta(days=1)
    for k in fields:
        v = fm.get(k)
        d = None
        if isinstance(v, datetime.datetime):  # before date: datetime IS a date subclass
            d = v.date()
        elif isinstance(v, datetime.date):
            d = v
        elif isinstance(v, str):
            m = re.match(r"(\d{4}-\d{2}-\d{2})", v.strip())
            if m:
                try:
                    d = datetime.date.fromisoformat(m.group(1))
                except ValueError:
                    d = None
        if d and d > limit:
            return (
                f"{k}: {d.isoformat()} is in the future (today is {today.isoformat()}) — "
                f"record-keeping dates must be the ACTUAL write date, never a guessed or "
                f"future one; use today's date"
            )
    return None


def _wikilink_resolves(t: str) -> bool:
    wiki = _wiki()
    if (wiki / f"{t}.md").is_file():
        return True  # literal path (incl. an already-sharded link)
    parts = t.split("/")
    if len(parts) >= 2 and parts[-1][:1].isalnum():
        ns, base = parts[0], parts[-1]
        b = base[0].lower()
        if (wiki / ns / b / f"{base}.md").is_file():
            return True  # first-letter shard (entities/qilin -> entities/q/qilin)
        if len(base) > 1 and (wiki / ns / b / base[1].lower() / f"{base}.md").is_file():
            return True  # second-letter reshard (oversized shard)
    return False


def _unresolvable_link_flags(p: Path, body: Optional[str]) -> list:
    """SOFT review flag (never a reject) for a curated-namespace page that INTRODUCES unresolvable
    wikilinks — a write-time backstop so broken links are attributable, without blocking the organic
    forward-refs that sources/importers rely on (link-audit 2026-07-09)."""
    if _namespace(p) not in _LINK_REVIEW_NS or not body:
        return []
    bad, seen = [], set()
    for m in _WIKILINK.finditer(body):
        t = (m.group(1) or "").strip().strip("/")
        if t.endswith(".md"):
            t = t[:-3]
        if not t or t in seen:
            continue
        seen.add(t)
        if "/" not in t:
            bad.append(f"[[{t}]] (bare name)")
        elif not _wikilink_resolves(t):
            bad.append(f"[[{t}]] (no such page)")
    if not bad:
        return []
    return [
        f"{len(bad)} unresolvable wikilink(s): "
        + "; ".join(bad[:5])
        + (" …" if len(bad) > 5 else "")
    ]


def _entity_sources(fm: dict) -> list:
    raw = fm.get("sources")
    if raw is None:
        raw = fm.get("source")
    return raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])


def _fabricated_source_reject(p: Path, fm: dict, prev_fm: Optional[dict] = None) -> Optional[str]:
    """HARD-reject an entity whose `sources:` uses the invalid SINGULAR `source/` namespace — the
    entity-backfill hallucination signature (okengine#348 follow-up). The schema's source namespace is
    plural `sources/`; a singular `source/<…>` page-ref is never valid, and every fabricated cohort page
    cited sources exactly this way. Plural `sources/…` refs are left alone (forward-refs are legitimate;
    corpus-audit catches plural dangling refs). On UPDATE, refs ALREADY on the page are grandfathered —
    the lane resends the full list, so block only NEWLY-introduced fabrication, never freeze a page that
    carries a legacy bad ref (e.g. apt29's pre-existing source/cisa/… stays editable)."""
    if _namespace(p) != "entities":
        return None
    grandfathered = {str(s).strip() for s in _entity_sources(prev_fm)} if prev_fm else set()
    bad = []
    for s in _entity_sources(fm):
        if not isinstance(s, str) or s.strip() in grandfathered:
            continue
        t = s.strip().strip("[]").removeprefix("wiki/")
        if t.endswith(".md"):
            t = t[:-3]
        if _SINGULAR_SOURCE_REF.match(t):  # singular `source/` — invalid namespace = fabrication
            bad.append(s)
    if not bad:
        return None
    return (
        "rejected: sources use the invalid singular `source/` namespace (the schema's source "
        "namespace is plural `sources/`): "
        + "; ".join(bad[:5])
        + (" …" if len(bad) > 5 else "")
        + " — this is the entity-backfill fabrication signature; cite an EXISTING `sources/<…>` "
        "page or a provenance label (e.g. 'MITRE ATT&CK')."
    )


def _missing_source_reject(p: Path, fm: dict, prev_fm: Optional[dict] = None) -> Optional[str]:
    """Reject NEW entity citations to nonexistent canonical source pages.

    Source pages may forward-reference entities, but the reverse is evidence provenance: an entity
    cannot cite a source that has not been compiled. Existing broken references are grandfathered
    so repair updates remain possible; only newly introduced values are blocked.
    """
    if _namespace(p) != "entities":
        return None
    grandfathered = {str(s).strip() for s in _entity_sources(prev_fm)} if prev_fm else set()
    bad = []
    for source in _entity_sources(fm):
        if not isinstance(source, str) or source.strip() in grandfathered:
            continue
        target = source.strip().strip("[]").strip("/").removeprefix("wiki/").removesuffix(".md")
        if target.startswith("sources/") and not (_wiki() / f"{target}.md").is_file():
            bad.append(target)
    if not bad:
        return None
    return (
        "rejected: entity `sources:` must cite existing canonical source pages; unresolved: "
        + "; ".join(bad[:5])
        + (" …" if len(bad) > 5 else "")
        + " — read and use the exact canonical source path; never invent or forward-reference evidence."
    )


def _identity_contradiction_flags(p: Path, fm: dict) -> list[str]:
    """Flag a filename designation that contradicts the page's own name/aliases.

    Domain-neutral shape only (``prefix-number``). A disagreement is review evidence, not a hard
    reject: aliases can legitimately document historical designations, but a polished profile must
    be quarantined until a human resolves the identity.
    """
    if _namespace(p) != "entities" or not isinstance(fm, dict):
        return []
    slug_ids = {(prefix.casefold(), number) for prefix, number in _SLUG_DESIGNATION.findall(p.stem)}
    if not slug_ids:
        return []
    values = [fm.get("name"), fm.get("title")]
    aliases = fm.get("aliases") or []
    values.extend(aliases if isinstance(aliases, list) else [aliases])
    declared = {
        (prefix.casefold(), number)
        for value in values
        if value is not None
        for prefix, number in _VALUE_DESIGNATION.findall(str(value))
    }
    conflicts = sorted(
        {
            (prefix, slug_num, declared_num)
            for prefix, slug_num in slug_ids
            for dprefix, declared_num in declared
            if prefix == dprefix and slug_num != declared_num
        }
    )
    if not conflicts:
        return []
    detail = ", ".join(
        f"{prefix.upper()}-{slug_num} vs {prefix.upper()}-{declared_num}"
        for prefix, slug_num, declared_num in conflicts[:4]
    )
    return [f"entity identity contradiction between path and declared name/aliases: {detail}"]


def _degeneration_flags(body: Optional[str]) -> list:
    """SOFT review flag (never a reject) for a DEGENERATE generation — a repetition-loop word-salad
    (comma/wikilink-aware). See the block comment above."""
    if not body:
        return []
    prose = _DEGEN_WIKILINK.sub(
        " ", _DEGEN_FENCE.sub("\n", body)
    )  # code + wikilink-lists are not prose
    worst = max((len(seg.split()) for seg in _DEGEN_STOP.split(prose)), default=0)
    if worst > _DEGEN_MAX_RUN:
        return [f"degenerate: {worst}-word unpunctuated run (repetition loop)"]
    return []


def _briefing_link_reject(p: Path, body: Optional[str]) -> Optional[str]:
    if _namespace(p) not in _STRICT_LINK_NS or not body:
        return None
    targets = []
    for m in _WIKILINK.finditer(body):
        t = m.group(1).strip().strip("/")
        if t.endswith(".md"):
            t = t[:-3]
        if t:
            targets.append(t)
    if not targets:
        return None
    # one walk builds both the exact rel-path set and the basename->rel-path map
    rels: set[str] = set()
    by_base: dict[str, str] = {}
    for f in WIKI.rglob("*.md"):
        rel = f.relative_to(WIKI).as_posix()[:-3]
        rels.add(rel)
        by_base.setdefault(f.stem, rel)
    broken = []
    n_source = n_knowledge = 0  # classify resolved links for the cite check
    for t in dict.fromkeys(targets):  # de-dup, keep order
        target_rel = t if t in rels else (by_base.get(t) if "/" not in t else None)
        if target_rel:
            ns = target_rel.split("/")[0]
            if ns == "sources":
                n_source += 1
            elif ns not in (
                "dashboards",
                "operational",
            ):  # a substantive claim, not a nav/meta link
                n_knowledge += 1
            continue
        base = t.split("/")[-1]
        if base in by_base:  # right page, wrong dir/shard
            broken.append(f"[[{t}]] — did you mean [[{by_base[base]}]]?")
            continue
        near = difflib.get_close_matches(base, list(by_base), n=2, cutoff=0.6)
        hint = (
            " — did you mean " + " or ".join(f"[[{by_base[n]}]]" for n in near) + "?"
            if near
            else ""
        )
        broken.append(f"[[{t}]] (no such page){hint}")
    if broken:
        return (
            "briefing links must resolve to existing pages (cite what you actually read; "
            "do not guess slugs): " + "; ".join(broken)
        )
    # A briefing that makes ENTITY claims must be verifiable: at least one resolvable
    # [[sources/...]] link. Footnotes and code-span paths are not links an analyst can click,
    # and the brief lanes keep omitting real citations despite the prompt (unenforced half).
    # A pure "nothing happened this week" briefing (no knowledge links) is exempt.
    if n_knowledge and not n_source:
        return (
            f"briefing cites {n_knowledge} entit{'y' if n_knowledge == 1 else 'ies'} but no source — "
            "every development must end with a resolvable [[sources/<path>]] link (a footnote or "
            "a code-span path is not a citation an analyst can verify)"
        )
    return None
