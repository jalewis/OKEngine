#!/usr/bin/env python3
"""Exact-path reference audit and conservative relocation repair.

Unlike the grounding percentage monitor, this inventory treats the complete canonical path as
identity.  It scans frontmatter ``sources`` relationships and body wikilinks across the entire
wiki, classifies a missing target by unique basename relocation, ambiguity, or absence, and writes
the complete inventory to ``dashboards/reference-integrity.json``.  ``--repair`` changes only
unique-basename relocations; fuzzy matches are evidence for review, never permission to rewrite.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
OUT_JSON = WIKI / "dashboards" / "reference-integrity.json"
OUT_MD = WIKI / "dashboards" / "reference-integrity.md"
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
_LINK = re.compile(r"\[\[([^\]|#\n]+)")


def _canonical(value: str) -> str:
    return value.strip().strip("[]").strip("/").removeprefix("wiki/").removesuffix(".md")


def _inventory() -> tuple[list[dict], dict[Path, list[tuple[str, str, str]]]]:
    pages = [p for p in WIKI.rglob("*.md") if "dashboards/reference-integrity" not in p.as_posix()]
    rels = {p.relative_to(WIKI).as_posix()[:-3]: p for p in pages}
    by_base: dict[str, list[str]] = defaultdict(list)
    for rel in rels:
        by_base[Path(rel).name.casefold()].append(rel)
    findings: dict[str, dict] = {}
    rewrites: dict[Path, list[tuple[str, str, str]]] = defaultdict(list)

    def record(target: str, origin: str, location: str, raw: str) -> None:
        if not target or target in rels:
            return
        candidates = sorted(by_base.get(Path(target).name.casefold(), []))
        classification = (
            "unique-relocation" if len(candidates) == 1
            else "ambiguous-relocation" if candidates else "no-candidate"
        )
        item = findings.setdefault(target, {
            "target": target, "classification": classification,
            "candidates": candidates, "inbound": [],
        })
        item["inbound"].append({"page": origin, "location": location})
        if classification == "unique-relocation":
            rewrites[rels[origin]].append((location, raw, candidates[0]))

    for rel, path in rels.items():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _FM.match(text)
        if match:
            try:
                fm = yaml.safe_load(match.group(1)) or {}
            except yaml.YAMLError:
                fm = {}
            values = fm.get("sources") if isinstance(fm, dict) else None
            values = values if isinstance(values, list) else ([values] if values else [])
            for value in values:
                if not isinstance(value, str):
                    continue
                target = _canonical(value)
                if target.startswith(("sources/", "source/")):
                    record(target, rel, "frontmatter:sources", value)
        body = text[match.end():] if match else text
        for link in _LINK.findall(body):
            target = _canonical(link)
            # Bare/entity/concept logical links deliberately resolve through the partition index;
            # this incident is source-path integrity. Do not mistake valid logical entity links for
            # physical relocation candidates.
            if target.startswith(("sources/", "source/")):
                record(target, rel, "body:wikilink", link)
    return sorted(findings.values(), key=lambda x: (-len(x["inbound"]), x["target"])), rewrites


def _repair(rewrites: dict[Path, list[tuple[str, str, str]]]) -> tuple[int, int]:
    pages = changes = 0
    for path, actions in rewrites.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        original = text
        match = _FM.match(text)
        head, body = (text[:match.end()], text[match.end():]) if match else ("", text)
        for location, raw, target in actions:
            before = head if location == "frontmatter:sources" else body
            if location == "frontmatter:sources" and match:
                head = head.replace(raw, target)
            elif location == "body:wikilink":
                body = body.replace(f"[[{raw}", f"[[{target}")
            after = head if location == "frontmatter:sources" else body
            if after != before:
                changes += 1
        text = head + body
        if text != original:
            path.write_text(text, encoding="utf-8")
            pages += 1
    return pages, changes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args(argv)
    if not WIKI.is_dir():
        print(f"reference-integrity: wiki missing at {WIKI}", file=sys.stderr)
        return 1
    findings, rewrites = _inventory()
    repaired_pages = repaired_refs = 0
    if args.repair:
        repaired_pages, repaired_refs = _repair(rewrites)
        findings, _ = _inventory()
    counts = {
        key: sum(1 for item in findings if item["classification"] == key)
        for key in ("unique-relocation", "ambiguous-relocation", "no-candidate")
    }
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "updated": now, "missing_targets": len(findings),
        "inbound_references": sum(len(item["inbound"]) for item in findings),
        "affected_pages": len({row["page"] for item in findings for row in item["inbound"]}),
        "counts": counts, "repaired_pages": repaired_pages, "repaired_references": repaired_refs,
        "findings": findings,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    lines = [
        "---", "type: dashboard", 'title: "Reference integrity"', f"updated: {now}", "---", "",
        f"# Reference integrity — {now}", "",
        f"- missing targets: **{payload['missing_targets']}**",
        f"- inbound references: **{payload['inbound_references']}**",
        f"- affected pages: **{payload['affected_pages']}**",
        f"- unique relocations: **{counts['unique-relocation']}**",
        f"- ambiguous relocations: **{counts['ambiguous-relocation']}**",
        f"- no candidate: **{counts['no-candidate']}**", "",
        "Complete inventory: `dashboards/reference-integrity.json`.", "",
        "| Target | Class | Inbound | Candidate |", "|---|---|---:|---|",
    ]
    lines.extend(
        f"| `{item['target']}` | {item['classification']} | {len(item['inbound'])} | "
        f"{', '.join(f'`{c}`' for c in item['candidates'])} |"
        for item in findings
    )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"reference-integrity: {payload['missing_targets']} missing targets, "
        f"{payload['inbound_references']} inbound, repaired {repaired_refs} refs/{repaired_pages} pages"
    )
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
