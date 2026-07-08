#!/usr/bin/env python3
"""Deterministic canonical assembler — fuse per-source observations into the golden record.

Multi-source entity resolution (okengine#38). For each canonical entity, reads its
per-source observation pages (observations/<source>/<slug>.md — one importer per source,
never merged) and fuses them into the canonical entities/<slug>.md by `merge_policy`:

  - union     — combine all values, deduped, order-stable (additive sets/relationships)
  - consensus — headline value = highest source reliability (Admiralty A-F; recency
                tiebreak); ALL distinct values preserved with attribution; a genuine
                disagreement flags the page for review (G3, never silently dropped)
  - latest    — most-recently-observed value wins (evolving status)

Token-free / no LLM. Generic over any OKF vault: reads `merge_policy` + `source_registry`
from the pack's schema.yaml; hardcodes no domain fields. The canonical body (agent
synthesis) + non-owned frontmatter are preserved. Inert until observations/ exists.

Usage: canonical_assemble.py [--vault DIR] [--dry-run]
Env: WIKI_PATH (default /opt/vault).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okf_migrate  # noqa: E402  — shared shard-aware page locator (find_page / canonical_key)

_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?(.*)\Z", re.S)
# A>...>F, higher rank = more reliable; unknown reliability sorts below F.
_REL_RANK = {c: i for i, c in enumerate("FEDCBA")}
# Label fields use consensus-pick but NEVER flag a conflict — sources legitimately format
# the same entity's name/title differently; that's not a disagreement to review.
_NO_FLAG = {"name", "title"}
# Per-source provenance / Admiralty weighting metadata. Lives on OBSERVATIONS only — never a
# claim about the entity. Stripped before fusion (else it false-conflicts: kev cred 1 vs nvd 2)
# AND dropped from the canonical on write (so a leaked value self-heals on re-assembly). #40.
_OBS_ENVELOPE = {"source", "canonical", "reliability", "credibility"}


def _rank(rel) -> int:
    return _REL_RANK.get(str(rel or "").strip().upper(), -1)


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def _key(v) -> str:
    """Stable dedup/identity key for a value (scalars and dict items alike)."""
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    return str(v).strip().lower()


# ── the pure fusion core ─────────────────────────────────────────────────────
def fuse(observations: list[dict], policy: dict) -> dict:
    """Fuse source observations into a canonical field set.

    observations: [{source, reliability, observed: 'YYYY-MM-DD', fields: {f: value}}].
    policy: {'union': set, 'consensus': set, 'latest': set}; unlisted fields infer by
            shape (list -> union, scalar -> consensus).
    Returns {'fields': {f: fused_value}, 'conflicts': [{field, headline, values:[...]}]}.
    """
    union_f = {str(x) for x in policy.get("union", [])}
    consensus_f = {str(x) for x in policy.get("consensus", [])}
    latest_f = {str(x) for x in policy.get("latest", [])}

    names: list[str] = []
    for o in observations:
        for f in (o.get("fields") or {}):
            if f not in names:
                names.append(f)

    fields: dict = {}
    conflicts: list[dict] = []

    for f in names:
        present = [(o, (o.get("fields") or {}).get(f)) for o in observations
                   if not _empty((o.get("fields") or {}).get(f))]
        if not present:
            continue
        sample = present[0][1]
        mode = ("union" if f in union_f else "consensus" if f in consensus_f
                else "latest" if f in latest_f
                else ("union" if isinstance(sample, list) else "consensus"))

        if mode == "union":
            seen, out = set(), []
            for _o, val in present:
                for item in _as_list(val):
                    k = _key(item)
                    if k and k not in seen:
                        seen.add(k)
                        out.append(item)
            fields[f] = out

        elif mode == "latest":
            best = max(present, key=lambda ov: (str(ov[0].get("observed") or ""),
                                                _rank(ov[0].get("reliability"))))
            fields[f] = best[1]

        else:  # consensus
            distinct: dict = {}
            for o, val in present:
                k = _key(val)
                d = distinct.setdefault(k, {"value": val, "sources": [], "rank": -1,
                                            "observed": ""})
                src = str(o.get("source") or "")
                if src and src not in d["sources"]:
                    d["sources"].append(src)
                d["rank"] = max(d["rank"], _rank(o.get("reliability")))
                d["observed"] = max(d["observed"], str(o.get("observed") or ""))
            ordered = sorted(distinct.values(), key=lambda d: (d["rank"], d["observed"]),
                             reverse=True)
            fields[f] = ordered[0]["value"]
            if len(ordered) > 1 and f not in _NO_FLAG:
                conflicts.append({
                    "field": f,
                    "headline": ordered[0]["value"],
                    "values": [{"value": d["value"], "sources": d["sources"]}
                               for d in ordered],
                })

    return {"fields": fields, "conflicts": conflicts}


# ── schema config ────────────────────────────────────────────────────────────
def load_schema(vault: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load((vault / "schema.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def merge_policy(schema: dict) -> dict:
    mp = schema.get("merge_policy") or {}
    return {k: {str(x) for x in (mp.get(k) or [])} for k in ("union", "consensus", "latest")}


def source_reliability(schema: dict) -> dict:
    reg = schema.get("source_registry") or {}
    return {k: str((v or {}).get("reliability") or "") for k, v in reg.items()}


# ── I/O: scan observations, group by entity, write canonical ─────────────────
def read_fm(path: Path) -> tuple[dict, str]:
    try:
        m = _FM.match(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}, ""
    if not m:
        return {}, ""
    try:
        import yaml
        fm = yaml.safe_load(m.group(1))
        return (fm if isinstance(fm, dict) else {}), m.group(2)
    except Exception:
        return {}, m.group(2)


def collect_observations(vault: Path, reliability: dict) -> dict:
    """{canonical_slug: [observation dict]} from observations/<source>/.../<slug>.md.
    The canonical slug is the page stem (or its `canonical:` field); source is the first
    path segment under observations/ (or the page's `source:` field)."""
    root = vault / "wiki" / "observations"
    out: dict = {}
    if not root.is_dir():
        return out
    for p in root.rglob("*.md"):
        if p.name.startswith(("_", ".")):
            continue
        fm, _ = read_fm(p)
        rel = p.relative_to(root)
        source = str(fm.get("source") or (rel.parts[0] if rel.parts else ""))
        slug = str(fm.get("canonical") or p.stem).strip().lower()
        if not slug or not source:
            continue
        out.setdefault(slug, []).append({
            "source": source,
            "type": str(fm.get("type") or ""),
            "reliability": fm.get("reliability") or reliability.get(source, ""),
            "observed": str(fm.get("last_seen") or fm.get("updated")
                            or fm.get("last_updated") or ""),
            # Strip the observation ENVELOPE (per-source provenance/Admiralty metadata + OKF
            # bookkeeping) so only real content is fused — see _OBS_ENVELOPE.
            "fields": {k: v for k, v in fm.items()
                       if k not in _OBS_ENVELOPE
                       and k not in ("type", "last_updated", "updated", "version")},
        })
    return out


def _canonical_type(obs: list[dict]) -> str:
    """The entity type for the canonical — the type the most reliable observation asserts."""
    best = max((o for o in obs if o.get("type")),
               key=lambda o: _rank(o.get("reliability")), default=None)
    return best["type"] if best else "entity"


_PRED_LABEL = {"uses-technique": "Techniques", "uses-malware": "Malware",
               "uses-tool": "Tools", "mitigated-by": "Mitigations"}
_ASSOC_HEAD = "## Associated (MITRE ATT&CK)"


def _assoc_section(rels: list) -> str:
    """A maintained body section listing a group's MITRE relationships as internal
    [[entities/<slug>|name]] wikilinks, grouped by predicate. Sourced — not fabricated."""
    by: dict = {}
    for r in rels or []:
        if isinstance(r, dict) and r.get("t"):
            by.setdefault(_PRED_LABEL.get(r.get("p"), "Related"), set()).add((r["t"], r.get("n") or r["t"]))
    if not by:
        return ""
    out = [_ASSOC_HEAD, ""]
    for label in sorted(by):
        items = sorted(by[label])
        out.append(f"**{label}** ({len(items)}): "
                   + ", ".join(f"[[entities/{t}|{n}]]" for t, n in items))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _set_managed_section(body: str, head: str, section: str) -> str:
    """Replace (or append) the auto-maintained section, preserving the agent's prose above."""
    body = re.sub(r"\n*" + re.escape(head) + r".*?(?=\n## |\Z)", "", body, flags=re.S).rstrip()
    return (body + "\n\n" + section.rstrip() + "\n") if section.strip() else (body.rstrip() + "\n")


def _dump_fm(d: dict) -> str:
    import yaml
    # default_flow_style=None: scalar-only lists render inline ([a, b]) to match the existing
    # page convention (and minimize diff churn); nested/dict lists fall back to block.
    return yaml.safe_dump(d, sort_keys=False, allow_unicode=True,
                          default_flow_style=None).rstrip("\n")


def write_canonical(vault: Path, slug: str, type_: str, fused: dict, conflicts: list,
                    sources: list, policy: dict, today: str, dry_run: bool = False) -> str:
    """Write the canonical entities/<slug>.md from the fused fields, PRESERVING the agent
    body + any curated/non-owned frontmatter, UNIONing additive (union-policy) fields with
    the existing page (so agent/feed additions are never dropped), and flagging conflicts
    for G3 review. Returns the new page text."""
    # The canonical seat, shard-correct: find the page wherever it currently sits (find_page), else
    # the schema's canonical key for a new one. The old `entities/slug[0]/slug.md` used the RAW first
    # char — wrong for an uppercase, digit, or symbol slug (canonical shards lowercase, map digits to
    # `0-9` and symbols to `_`), so the assembler read/wrote a different shard than reshelve files to.
    existing = okf_migrate.find_page(vault, "entities", slug)
    path = existing if existing else vault / "wiki" / f"{okf_migrate.canonical_key(vault, 'entities', slug, fused)}.md"
    existing_fm, body = (read_fm(path) if path.exists() else ({}, ""))
    union_f = policy.get("union", set())

    fields = dict(fused)
    for k in list(fields):                       # preserve agent-added LIST items, but don't
        if k in union_f:                         # merge a legacy comma-string into a list field
            existing_items = existing_fm.get(k) if isinstance(existing_fm.get(k), list) else []
            seen, merged = set(), []
            for item in existing_items + _as_list(fields[k]):
                kk = _key(item)
                if kk and kk not in seen:
                    seen.add(kk)
                    merged.append(item)
            fields[k] = merged

    # A consensus CONFLICT must not regress a value the canonical already holds: keep the
    # existing value and let review (needs_review) arbitrate, rather than overwrite with a
    # tied/possibly-wrong consensus pick (e.g. an entity-resolution over-merge).
    for c in conflicts:
        f = c.get("field")
        if f and f in fields and not _empty(existing_fm.get(f)):
            fields[f] = existing_fm[f]

    # mitre_rels is rendered as a body section (internal wikilinks), not a frontmatter field.
    assoc = fields.pop("mitre_rels", None)

    owned = set(fields) | {"needs_review", "conflicts", "assembled_from", "last_updated",
                           "version", "type", "name", "mitre_rels"}
    out: dict = {"type": existing_fm.get("type") or type_ or "entity"}
    name = existing_fm.get("name") or fields.get("name") or slug
    out["name"] = name
    for k, v in existing_fm.items():             # preserve curated / agent / non-owned fm,
        if k not in owned and k not in _OBS_ENVELOPE and not _empty(v):  # but never carry the
            out[k] = v                           # per-source envelope onto the canonical
    for k, v in fields.items():                  # assembler-owned fused fields
        if k != "name" and not _empty(v):
            out[k] = v
    out["assembled_from"] = sorted(sources)
    if conflicts:
        out["conflicts"] = conflicts             # Phase E reader renders these; flags review now
        out["needs_review"] = True
    note = (f"\n\n> Canonical record assembled from {', '.join(sorted(sources))}. "
            "Per-source detail under observations/; synthesis below is the ingest agent's.")
    page_body = body if body.strip() else f"{name}." + note + "\n"
    if assoc:
        page_body = _set_managed_section(page_body, _ASSOC_HEAD, _assoc_section(assoc))

    # Idempotency (okengine#43): if the owned frontmatter (excluding version/last_updated) and
    # the body are unchanged, skip the write + version bump — so the assembler can run often
    # without churning every canonical (re-tiering, reader cache, git noise). The body carries
    # the agent's prose, so an unchanged agent section doesn't force a spurious rewrite.
    if path.exists():
        prior = {k: v for k, v in existing_fm.items() if k not in ("version", "last_updated")}
        if out == prior and page_body == body:
            return path.read_text(encoding="utf-8", errors="replace"), False

    out["last_updated"] = today
    out["version"] = int(existing_fm.get("version") or 0) + 1
    new_text = "---\n" + _dump_fm(out) + "\n---\n" + page_body
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    return new_text, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble canonical entities from observations (no_agent).")
    ap.add_argument("--vault", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="assemble only this canonical slug (targeted run)")
    args = ap.parse_args(argv)
    vault = Path(args.vault)
    schema = load_schema(vault)
    policy, reliability = merge_policy(schema), source_reliability(schema)

    today = date.today().isoformat()
    groups = collect_observations(vault, reliability)
    if args.only:
        groups = {k: v for k, v in groups.items() if k == args.only.strip().lower()}
    counts = {"entities": 0, "written": 0, "skipped": 0, "conflicts": 0, "observations": 0, "multi": 0}
    for slug, obs in sorted(groups.items()):
        counts["entities"] += 1
        counts["observations"] += len(obs)
        srcs = sorted({o["source"] for o in obs})
        if len(srcs) > 1:
            counts["multi"] += 1
        result = fuse(obs, policy)
        if result["conflicts"]:
            counts["conflicts"] += 1
        try:
            _, wrote = write_canonical(vault, slug, _canonical_type(obs), result["fields"],
                                       result["conflicts"], srcs, policy, today, dry_run=args.dry_run)
        except OSError:
            continue
        counts["written" if wrote else "skipped"] += 1
        if args.dry_run and len(srcs) > 1:       # show the multi-source wins in dry-run
            al = result["fields"].get("aliases", [])
            print(f"  · {slug}: {srcs} -> aliases {len(al)}"
                  f"{', CONFLICTS ' + str([c['field'] for c in result['conflicts']]) if result['conflicts'] else ''}")
    verb = "would assemble" if args.dry_run else "assembled"
    print(f"canonical-assemble: {verb} {counts['written']} canonical(s) "
          f"({counts['skipped']} unchanged, skipped) from "
          f"{counts['observations']} observation(s) ({counts['multi']} multi-source, "
          f"{counts['conflicts']} with conflicts){' [dry-run]' if args.dry_run else ''}")
    if not groups:
        print("  (no observations/ yet — inert until importers write per-source records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
