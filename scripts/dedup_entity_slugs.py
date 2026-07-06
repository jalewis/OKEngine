#!/usr/bin/env python3
"""dedup_entity_slugs.py — resolve duplicate-slug collisions before an entity
partition migration (okengine#165).

A vault carrying both a legacy layout (entities/{type}/[{L}/]{slug}) and the
canonical by-letter layout accrues SLUG COLLISIONS: the same slug at two paths.
Some are TRUE DUPLICATES (one real-world thing, re-created at the canonical seat
by a lane while the imported copy still sits in a type-dir); some are
COINCIDENTAL (two different things sharing a slug, e.g. an attack-pattern and a
CVE both named `log4shell`) — merging those would destroy a page.

Phase 1 (default): classify. For each collision pair from okf_migrate.build_map,
ask the model (llm_lib.classify — local, thinking off) whether the two pages
describe the same real-world thing. Decisions checkpoint per pair to a resumable
JSON file; `uncertain` pairs are left for the operator. Re-runs skip decided pairs.

Phase 2 (--apply): execute the decisions —
  - same-thing  -> MERGE into the canonical seat: the longer body wins, frontmatter
    is the canonical page's with sources/tags unioned + `merged_from:` provenance;
    the legacy file is removed (this is a snapshot-backed operator migration, not
    an agent delete) and full-path refs to it are rewritten to the canonical key.
  - different-things -> DISAMBIGUATE the legacy page: its slug becomes
    `<slug>-<type>` (still in its current dir — the main migration re-nests it),
    and full-path refs to the old key are rewritten. Bare-slug refs resolve to the
    canonical page afterwards, which is the correct default.

Usage:
  dedup_entity_slugs.py [--root /opt/vault] [--namespace entities]
                        [--decisions <path>] [--apply] [--budget 600]
Env: OKENGINE_LLM_BASE_URL / OKENGINE_LLM_MODEL (llm_lib) · DEDUP_CALL_TIMEOUT (90)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cron"))
import llm_lib       # noqa: E402
import okf_migrate   # noqa: E402

_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
CALL_TIMEOUT = int(os.environ.get("DEDUP_CALL_TIMEOUT", "90"))


def _page(root: Path, key: str):
    import yaml
    p = root / "wiki" / (key + ".md")
    t = p.read_text(encoding="utf-8", errors="replace")
    m = _FM.match(t)
    fm = {}
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            fm = {}
    body = t[m.end():] if m else t
    return p, fm, body


def _digest(key: str, fm: dict, body: str) -> str:
    return (f"path: {key}\ntype: {fm.get('type')}\ntitle: {fm.get('title') or fm.get('name')}\n"
            f"tags: {fm.get('tags')}\n---\n{body.strip()[:800]}")


def collision_jobs(root: Path, collisions):
    """Collision pairs -> classify jobs (a, b, dest). Canonical seat occupied: each
    legacy classifies against the canonical page (b == dest). Canonical seat EMPTY
    (two legacy paths racing for one destination): the two legacies classify against
    each other — reading the nonexistent destination livelocked here (okengine#165)."""
    groups: dict[str, list[str]] = {}
    for cur, new in collisions:
        groups.setdefault(new, []).append(cur)
    jobs = []
    for dest, curs in groups.items():
        if (root / "wiki" / (dest + ".md")).is_file():
            jobs += [(c, dest, dest) for c in curs]
        elif len(curs) >= 2:
            jobs += [(curs[i], curs[0], dest) for i in range(1, len(curs))]
        # single legacy -> empty dest is not a collision at all (main() re-derives live)
    return jobs


def classify_pairs(root: Path, collisions, decisions: dict, budget: int,
                   checkpoint=None) -> dict:
    deadline = time.monotonic() + budget
    done = uncertain = 0
    for cur, other, dest in collision_jobs(root, collisions):
        pair_id = f"{cur}::{other}"
        legacy_id = f"{cur}::{dest}"          # pre-group runs keyed legacy::canonical
        if pair_id in decisions or legacy_id in decisions:
            continue
        if time.monotonic() > deadline:
            print(f"dedup: time budget reached — {done} classified this run (resumable)")
            break
        try:
            _, fm_a, body_a = _page(root, cur)
            _, fm_b, body_b = _page(root, other)
        except FileNotFoundError:
            # live-vault race: a lane moved one side since the collision scan.
            # Skip — the pair re-evaluates against live state on the next pass.
            print(f"  vanished (live-vault race), skipping: {cur}")
            continue
        prompt = ("Two wiki pages share the slug "
                  f"'{cur.rsplit('/', 1)[-1]}'. Do they describe the SAME real-world thing "
                  "(same company/malware/vulnerability/campaign — one page duplicated), or "
                  "DIFFERENT things that coincidentally share a name?\n\n"
                  f"## Page A\n{_digest(cur, fm_a, body_a)}\n\n"
                  f"## Page B\n{_digest(other, fm_b, body_b)}")
        try:
            verdict = llm_lib.classify(prompt, ["same-thing", "different-things"],
                                       timeout=CALL_TIMEOUT, retries=0)
        except llm_lib.LLMError as e:
            print(f"dedup: model endpoint failed on {pair_id} — stopping (resumable): {e}")
            break
        decisions[pair_id] = {"legacy": cur, "other": other, "canonical": dest,
                              "verdict": verdict,
                              "decided": datetime.now(timezone.utc).isoformat()[:19] + "Z"}
        done += 1
        if verdict == "uncertain":
            uncertain += 1
        if checkpoint:
            checkpoint(decisions)                     # flush PER PAIR — a kill loses nothing
        print(f"  {verdict:<17} {cur}", flush=True)
    print(f"dedup classify: {done} decided this run ({uncertain} uncertain), "
          f"{len(decisions)} total decided")
    return decisions


def _rewrite_refs(root: Path, old_key: str, new_key: str) -> int:
    pat = re.compile(r"\[\[" + re.escape(old_key) + r"([\]#|])")
    n_files = 0
    for p in (root / "wiki").rglob("*.md"):
        try:
            c = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"[[{old_key}" not in c:
            continue
        p.write_text(pat.sub(lambda m: f"[[{new_key}{m.group(1)}", c), encoding="utf-8")
        n_files += 1
    return n_files


def apply_decisions(root: Path, decisions: dict) -> None:
    import yaml
    merged = renamed = skipped = 0
    log_lines = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    def _merge(a_key, b_key, dest_key):
        """Fold a+b into the canonical seat: longer body wins; frontmatter from the page
        already at dest (else the longer one); sources/tags unioned; merged_from provenance."""
        _, fm_a, body_a = _page(root, a_key)
        _, fm_b, body_b = _page(root, b_key)
        a_longer = len(body_a.strip()) > len(body_b.strip())
        keep_body = body_a if a_longer else body_b
        fm = dict(fm_b if b_key == dest_key or not a_longer else fm_a)
        for k in ("sources", "tags"):
            u = list(dict.fromkeys((fm_b.get(k) or []) + (fm_a.get(k) or [])))
            if u:
                fm[k] = u
        fm["merged_from"] = list(dict.fromkeys((fm.get("merged_from") or [])
                                               + [k for k in (a_key, b_key) if k != dest_key]))
        fm["updated"] = today
        (root / "wiki" / (dest_key + ".md")).parent.mkdir(parents=True, exist_ok=True)
        (root / "wiki" / (dest_key + ".md")).write_text(
            "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
            + "\n---" + keep_body, encoding="utf-8")
        n = 0
        for k in (a_key, b_key):
            if k != dest_key:
                (root / "wiki" / (k + ".md")).unlink()
                n += _rewrite_refs(root, k, dest_key)
        return n

    def _disambiguate(key):
        _, fm_x, _ = _page(root, key)
        typ = str(fm_x.get("type") or "entity").strip() or "entity"
        slug = key.rsplit("/", 1)[-1]
        new_key = key.rsplit("/", 1)[0] + f"/{slug}-{typ}"
        if (root / "wiki" / (new_key + ".md")).exists():
            return None
        os.rename(root / "wiki" / (key + ".md"), root / "wiki" / (new_key + ".md"))
        _rewrite_refs(root, key, new_key)
        return new_key

    for d in decisions.values():
        if d.get("applied"):
            continue
        cur, verdict = d["legacy"], d["verdict"]
        other = d.get("other") or d["canonical"]     # pre-group decisions: other == canonical
        dest = d["canonical"]
        pa = root / "wiki" / (cur + ".md")
        pb = root / "wiki" / (other + ".md")
        if not pa.is_file() or not pb.is_file():
            skipped += 1
            continue
        if verdict == "same-thing":
            n = _merge(cur, other, dest)
            log_lines.append(f"- {today} dedup-165 merge {cur} + {other} -> {dest} (refs in {n} files)")
            merged += 1
        elif verdict == "different-things":
            if other == dest:
                nk = _disambiguate(cur)              # canonical page keeps the slug
                if nk is None:
                    skipped += 1
                    continue
                log_lines.append(f"- {today} dedup-165 disambiguate {cur} -> {nk}")
                renamed += 1
            else:
                # two legacies, no canonical: same type = true ambiguity -> operator
                _, fm_a, body_a = _page(root, cur)
                _, fm_b, body_b = _page(root, other)
                if str(fm_a.get("type")) == str(fm_b.get("type")):
                    skipped += 1
                    print(f"  operator: same-type different-things {cur} vs {other}")
                    continue
                loser = cur if len(body_a.strip()) <= len(body_b.strip()) else other
                nk = _disambiguate(loser)            # longer body keeps the plain slug
                if nk is None:
                    skipped += 1
                    continue
                log_lines.append(f"- {today} dedup-165 disambiguate {loser} -> {nk}")
                renamed += 1
        else:
            skipped += 1                                       # uncertain: operator's
            continue
        d["applied"] = today
    if log_lines:
        with open(root / "wiki" / "log.md", "a", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n")
    print(f"dedup apply: {merged} merged, {renamed} disambiguated, {skipped} skipped/uncertain")


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    ap.add_argument("--namespace", default="entities")
    ap.add_argument("--decisions", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--budget", type=int, default=600)
    args = ap.parse_args(argv)
    root = Path(args.root)
    dpath = Path(args.decisions) if args.decisions else root / ".okengine" / "entity-dedup-decisions.json"
    dpath.parent.mkdir(parents=True, exist_ok=True)
    decisions = json.loads(dpath.read_text()) if dpath.is_file() else {}

    _, collisions = okf_migrate.build_map(root, args.namespace)
    print(f"dedup: {len(collisions)} live collision(s), {len(decisions)} prior decision(s)")

    def _flush(d):
        dpath.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")

    if args.apply:
        apply_decisions(root, decisions)
    else:
        decisions = classify_pairs(root, collisions, decisions, args.budget, checkpoint=_flush)
    dpath.write_text(json.dumps(decisions, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"decisions -> {dpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
