#!/usr/bin/env python3
"""authority_enrich — stamp canonical identity-authority IDs onto vault pages (okengine#314).

The connector runtime (#273) acquires enrichment records but nothing APPLIES them: this lane is
the deterministic apply layer. Given an ``mode: enrichment`` manifest carrying an ``enrich:``
block, it selects eligible pages, runs the connector once per page (the page's ``match.page_field``
value becomes the manifest's ``match.query_input``), and stamps ``authority_ids.<authority>`` when
exactly one record matches. Everything is arithmetic over exact normalized string equality —
no LLM judgment participates anywhere (the okengine#313 pattern), so results are auditable and
an agent cannot launder an identity claim into a stamped authority ID.

Safety rails (the #314 acceptance contract):
  - ADDITIVE ONLY: the lane adds ``authority_ids.<a>`` + an attribution observation; it never
    modifies any other frontmatter field or the page body.
  - NEVER OVERWRITE: a page already carrying a DIFFERENT ``authority_ids.<a>`` value gets a
    ``conflicts`` entry + ``needs_review`` — the existing value is left untouched.
  - AMBIGUITY -> REVIEW, never merged: two or more matching records (or the same authority ID
    already stamped on another page — the convergence check) flag ``needs_review`` with a
    ``conflicts`` entry instead of guessing.
  - Attribution: every stamp appends an ``authority_observations`` entry
    ``{authority, id, source, observed_at, basis}`` so the assertion stays attributable.
  - Observability: a coverage artifact (eligible/stamped/unmatched/ambiguous/conflicts/duplicates)
    is written under ``.okengine/connectors/authority/<authority>.json`` and the run summary goes
    to stdout; the connector's own attempts land in the collection ledger as usual.

Matching: a record matches a page iff the normalized (casefold/strip) page field value equals any
value resolved from the record payload's ``match.candidate_paths`` (dotted paths; a list segment
maps over items; a trailing dict resolves its ``value`` key — the ROR ``names[].value`` shape).

Env: WIKI_PATH (vault root). Pure script (no_agent): always emits {"wakeAgent": false}.
Usage:
    authority_enrich.py --manifest <pack>/connectors/ror-organizations.yaml [--limit 25]
                        [--dry-run] [--fixture f.json] [--state-root DIR] [--ledger-root DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?", re.S)
_CONNECTOR = Path(__file__).resolve().parent / "source_connector.py"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split(text: str):
    m = _FM_RE.match(text)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, text
    return (fm if isinstance(fm, dict) else None), text[m.end():]


def _resolve(payload, path: str):
    """All leaf string values at a dotted path; list segments map over items; dict leaves
    resolve their 'value' key (ROR names[].value)."""
    nodes = [payload]
    for seg in path.split("."):
        nxt = []
        for node in nodes:
            if isinstance(node, list):
                node_items = node
            else:
                node_items = [node]
            for item in node_items:
                if isinstance(item, dict) and seg in item:
                    nxt.append(item[seg])
        nodes = nxt
    out = []
    stack = list(nodes)
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(n)
        elif isinstance(n, dict):
            if "value" in n:
                stack.append(n["value"])
        elif n is not None:
            out.append(str(n))
    return out


def _norm(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def _run_connector(manifest_path: Path, query_input: str, value: str, args) -> dict | None:
    # NOTE: never --summary-only here — the lane needs the items[] payload on stdout.
    cmd = [sys.executable, str(_CONNECTOR), "--manifest", str(manifest_path),
           "--param", f"{query_input}={value}", "--observed-at", _now()]
    if args.fixture:
        cmd += ["--fixture", str(args.fixture)]
    if args.state_root:
        cmd += ["--state-root", str(args.state_root)]
    if args.ledger_root:
        cmd += ["--collection-ledger", str(args.ledger_root)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  ERROR connector run failed for {value!r}: {exc}", file=sys.stderr)
        return None
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    print(f"  ERROR no JSON result for {value!r} (rc={proc.returncode}): "
          f"{proc.stderr.strip()[:200]}", file=sys.stderr)
    return None


def _flag(fm: dict, authority: str, detail: str) -> None:
    conflicts = fm.get("conflicts") if isinstance(fm.get("conflicts"), list) else []
    entry = {"field": f"authority_ids.{authority}", "detail": detail}
    if entry not in conflicts:
        conflicts.append(entry)
    fm["conflicts"] = conflicts
    fm["needs_review"] = True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=25, help="max connector lookups per run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--state-root", type=Path)
    ap.add_argument("--ledger-root", type=Path)
    args = ap.parse_args(argv)

    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8")) or {}
    enrich = manifest.get("enrich") or {}
    if manifest.get("mode") != "enrichment" or not enrich:
        print("ERROR: manifest is not an enrichment connector with an enrich: block", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1
    authority = enrich["authority"]
    id_path = enrich["id_path"]
    match = enrich["match"]
    targets = enrich["targets"]
    types = {str(t) for t in targets.get("types") or []}
    namespaces = [str(n).strip("/") for n in targets.get("namespaces") or []]
    page_field, query_input = match["page_field"], match["query_input"]
    candidate_paths = [str(c) for c in match["candidate_paths"]]

    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    # scan: eligible pages + the existing authority-id map (for the convergence/duplicate check)
    eligible: list[tuple[Path, dict, str]] = []
    id_owners: dict[str, list[Path]] = {}
    for p in sorted(WIKI.rglob("*.md")):
        rel = p.relative_to(WIKI).as_posix()
        if p.name.startswith(("_", ".")) or p.name.upper().startswith("INDEX") or ".bak" in p.name:
            continue
        if namespaces and not any(rel.startswith(ns + "/") for ns in namespaces):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:          # page vanished mid-scan (reshelve race) — skip
            continue
        fm, _body = _split(text)
        if not fm or str(fm.get("type") or "") not in types:
            continue
        if str(fm.get("status") or "").lower() == "tombstoned":
            continue
        ids = fm.get("authority_ids") if isinstance(fm.get("authority_ids"), dict) else {}
        if ids.get(authority):
            id_owners.setdefault(str(ids[authority]), []).append(p)
            continue
        value = str(fm.get(page_field) or "").strip()
        if value:
            eligible.append((p, fm, value))

    stamped = unmatched = ambiguous = conflicts_n = duplicates = 0
    now = _now()
    for p, _fm, value in eligible[: max(0, args.limit)]:
        result = _run_connector(args.manifest, query_input, value, args)
        if not result or not result.get("ok"):
            continue
        hits = []
        for item in result.get("items") or []:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            cands = {_norm(v) for cp in candidate_paths for v in _resolve(payload, cp)}
            if _norm(value) in cands:
                aid = (_resolve(payload, id_path) or [None])[0]
                if aid:
                    hits.append(str(aid))
        hits = sorted(set(hits))

        # re-read at write time (the connector call took real time)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, body = _split(text)
        if not fm:
            continue
        ids = fm.get("authority_ids") if isinstance(fm.get("authority_ids"), dict) else {}

        if len(hits) == 0:
            unmatched += 1
            print(f"  unmatched {p.relative_to(WIKI)}: no exact {authority} match for {value!r}")
            continue
        if len(hits) > 1:
            ambiguous += 1
            _flag(fm, authority, f"ambiguous: {len(hits)} {authority} records exactly match "
                                 f"{value!r} ({', '.join(hits[:3])}); never auto-merged")
            print(f"  ambiguous {p.relative_to(WIKI)}: {len(hits)} candidates -> review")
        elif ids.get(authority) and str(ids[authority]) != hits[0]:
            conflicts_n += 1
            _flag(fm, authority, f"existing {authority} id {ids[authority]!r} disagrees with "
                                 f"connector match {hits[0]!r}; existing value kept")
            print(f"  conflict {p.relative_to(WIKI)}: kept existing {ids[authority]!r}")
        else:
            aid = hits[0]
            owners = id_owners.get(aid, [])
            if owners and p not in owners:
                duplicates += 1
                _flag(fm, authority, f"{authority} id {aid!r} is already stamped on "
                                     f"{owners[0].relative_to(WIKI).as_posix()}; duplicate identity "
                                     f"needs human convergence")
                print(f"  duplicate {p.relative_to(WIKI)}: {aid} also on {owners[0].name} -> review")
            else:
                ids = dict(ids)
                ids[authority] = aid
                fm["authority_ids"] = ids
                obs = fm.get("authority_observations") \
                    if isinstance(fm.get("authority_observations"), list) else []
                obs.append({"authority": authority, "id": aid,
                            "source": str(manifest.get("id") or ""), "observed_at": now,
                            "basis": f"exact {page_field} match via {query_input}"})
                fm["authority_observations"] = obs
                id_owners.setdefault(aid, []).append(p)
                stamped += 1
                print(f"  stamp {p.relative_to(WIKI)}: {authority}={aid}")
        if not args.dry_run:
            p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
                         + "---\n" + body, encoding="utf-8")

    # convergence sweep over PRE-EXISTING stamps: the same authority id on >1 page needs review
    for aid, owners in id_owners.items():
        if len(owners) < 2:
            continue
        for p in owners:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm, body = _split(text)
            if not fm:
                continue
            detail = (f"{authority} id {aid!r} appears on {len(owners)} pages "
                      f"({', '.join(o.relative_to(WIKI).as_posix() for o in owners[:3])}); "
                      f"duplicate identity needs human convergence")
            existing = fm.get("conflicts") if isinstance(fm.get("conflicts"), list) else []
            if any(c.get("detail") == detail for c in existing if isinstance(c, dict)):
                continue
            _flag(fm, authority, detail)
            duplicates += 1
            if not args.dry_run:
                p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
                             + "---\n" + body, encoding="utf-8")

    coverage = {"authority": authority, "manifest": str(manifest.get("id") or ""), "at": now,
                "eligible": len(eligible), "looked_up": min(len(eligible), max(0, args.limit)),
                "stamped": stamped, "unmatched": unmatched, "ambiguous": ambiguous,
                "conflicts": conflicts_n, "duplicates": duplicates,
                "already_stamped": sum(len(v) for v in id_owners.values()) - stamped,
                "dry_run": bool(args.dry_run)}
    if not args.dry_run:
        cov_dir = VAULT / ".okengine" / "connectors" / "authority"
        cov_dir.mkdir(parents=True, exist_ok=True)
        (cov_dir / f"{authority}.json").write_text(json.dumps(coverage, indent=1) + "\n",
                                                   encoding="utf-8")
    print(f"authority-enrich[{authority}]: {stamped} stamped, {unmatched} unmatched, "
          f"{ambiguous} ambiguous, {conflicts_n} conflicts, {duplicates} duplicates "
          f"({len(eligible)} eligible{' [dry-run]' if args.dry_run else ''})")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
