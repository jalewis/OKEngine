#!/usr/bin/env python3
"""coinstall_preflight.py — collision checker for multipack (walk-up) installs.

Before installing a pack alongside another (docs/authoring-a-pack.md §8), report every
surface where the two would collide. FAIL = do not install without resolving; WARN =
resolve-by-rule exists (host wins / dedupe / id-merge) — apply it deliberately.

Checks:
  1. types            pack types/additions vs the HOST ROOT schema (host wins on name
                      collision — flagged with a required-field diff); same-name types in
                      OTHER walk-up domains are INFO (walk-up separates contracts).
  2. namespaces       pack wiki namespaces vs host top-level dirs.
  3. crons            job name/id collisions; engine-template PROMPT-KEY collisions
                      (two packs supplying prompts for the same engine job — host wins).
  4. configs          same-named files under config/ (id-merge needed); duplicate rule
                      ids inside completeness-rules merges.
  5. feeds            xmlUrl overlap (dedupe on merge).
  6. raw streams      raw/<stream> paths referenced by pack scripts vs host's.
  7. dashboards       dashboard paths written by pack scripts that already exist in host.

Usage: coinstall_preflight.py <host-deployment-dir> <pack-dir> [--additions <yaml>]
Exit: 0 = PASS/WARN only · 1 = FAIL findings · 2 = usage/parse error
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

FINDINGS: list[tuple[str, str, str]] = []   # (level, area, message)


def add(level: str, area: str, msg: str) -> None:
    FINDINGS.append((level, area, msg))


def _yaml(p: Path) -> dict:
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
        return d if isinstance(d, dict) else {}
    except Exception as e:
        add("FAIL", "parse", f"unparseable yaml: {p} ({e})")
        return {}


def check_types(host: Path, pack: Path, additions: Path | None,
                subtree: bool = False) -> None:
    hroot = _yaml(host / "schema.yaml")
    htypes = hroot.get("types") or {}
    src = additions if additions else pack / "schema.yaml"
    ptypes = (_yaml(src).get("types") or {})
    if not ptypes:
        add("WARN", "types", f"no types found in {src.name} — nothing to merge?")
    for t, tdef in ptypes.items():
        if t in htypes:
            hreq = set((htypes[t] or {}).get("required") or [])
            preq = set((tdef or {}).get("required") or [])
            if hreq == preq:
                add("WARN", "types", f"type '{t}' already in host root schema (identical "
                                     "required fields) — HOST WINS, do not add")
            elif subtree:
                # subtree shape: the type never lands in the host root — walk-up keeps
                # the contracts separate (nearest schema governs). Awareness, not a block.
                add("WARN", "types", f"type '{t}' also in host root schema with DIFFERENT "
                                     f"required fields (host {sorted(hreq)} vs subtree "
                                     f"{sorted(preq)}) — walk-up separates the contracts; "
                                     "make sure pages land on the intended side")
            else:
                add("FAIL", "types", f"type '{t}' collides with host root schema with "
                                     f"DIFFERENT required fields (host {sorted(hreq)} vs "
                                     f"pack {sorted(preq)}) — reconcile before install")
    # a pack type must not be shadowed by a host type_alias (found live: sec's
    # `software` type vs the host's `software: product` alias — the alias wins in
    # normalization drains and silently retypes the pack's pages)
    haliases = hroot.get("type_aliases") or {}
    for t2 in ptypes:
        if t2 in haliases:
            add("FAIL", "types", f"host type_alias '{t2}: {haliases[t2]}' shadows pack type "
                                 f"'{t2}' — retire the alias or rename the type")
    # okengine#181: the guest may ALSO bring type_aliases (host-schema-additions.yaml), merged
    # into the host by merge_type_aliases. An incoming alias whose KEY equals a HOST-OWNED type
    # would retype the host's pages on the normalization drain — the merge SKIPS it (host wins),
    # but surface it so a real conflict is deliberate rather than silent.
    for a, c in (_yaml(src).get("type_aliases") or {}).items():
        if a in htypes:
            add("WARN", "types", f"incoming type_alias '{a}: {c}' shadows host-owned type "
                                 f"'{a}' — SKIPPED on merge (host wins); reconcile if intended")
    # other walk-up domains: same-name types are separated by walk-up, but say so
    for sub in (host / "wiki").rglob("schema.yaml"):
        stypes = _yaml(sub).get("types") or {}
        both = sorted(set(ptypes) & set(stypes))
        if both:
            add("INFO", "types", f"domain {sub.parent.relative_to(host)} also declares "
                                 f"{both} — walk-up keeps contracts separate; no action")


def _domain_slug(pack: Path) -> str:
    d = (_yaml(pack / "pack.yaml").get("domain"))
    if d:
        return str(d).strip().strip("/")
    name = pack.resolve().name
    return name[len("okpack-"):] if name.startswith("okpack-") else name


def check_namespaces(host: Path, pack: Path, subtree: bool = False) -> None:
    sub_schema = pack / "subdomain" / "schema.yaml"
    if subtree:
        # Subtree shape: the pack's STANDALONE namespaces never land at the host
        # root — they nest under wiki/<slug>/. The only root-level surface to check
        # is the subtree dir itself (first real subtree run false-positived on the
        # standalone `incidents` namespace colliding with an unrelated host dir).
        slug = _domain_slug(pack)
        d = host / "wiki" / slug
        if not d.is_dir():
            return
        if ((d / "schema.yaml").is_file() and sub_schema.is_file()
                and (d / "schema.yaml").read_bytes() == sub_schema.read_bytes()):
            add("INFO", "namespaces", f"'wiki/{slug}/' is this pack's already-installed "
                                      "sub-domain (schema identical) — no action")
        else:
            add("FAIL", "namespaces", f"subtree 'wiki/{slug}/' already exists in host and "
                                      "is not this pack's install — pick --under or "
                                      "reconcile ownership")
        return
    pns = set((( _yaml(pack / "schema.yaml").get("partitioning") or {}).get("namespaces") or {}).keys())
    core = {"entities", "sources", "concepts", "predictions", "findings", "briefings", "trends"}
    pack_part = (_yaml(pack / "schema.yaml").get("partitioning") or {}).get("namespaces") or {}
    host_part = (_yaml(host / "schema.yaml").get("partitioning") or {}).get("namespaces") or {}
    for ns in sorted(pns - core):
        d = host / "wiki" / ns
        if not d.is_dir():
            continue
        # idempotency (subtree shape): the dir carrying THIS pack's subdomain schema
        # is its own already-installed walk-up sub-domain, not a conflict.
        if (sub_schema.is_file() and (d / "schema.yaml").is_file()
                and (d / "schema.yaml").read_bytes() == sub_schema.read_bytes()):
            add("INFO", "namespaces", f"'wiki/{ns}/' is this pack's already-installed "
                                      "sub-domain (schema identical) — no action")
            continue
        # idempotency (taxonomy shape): the host schema already declares this
        # namespace with the pack's exact partitioning def — a previous
        # install-domain merge, not a conflict.
        if ns in host_part and host_part.get(ns) == pack_part.get(ns):
            add("INFO", "namespaces", f"'wiki/{ns}/' already installed (host partitioning "
                                      "matches the pack's) — no action")
            continue
        add("FAIL", "namespaces", f"pack namespace 'wiki/{ns}/' already exists in host — "
                                  "pick a subtree name or reconcile ownership")


def check_crons(host: Path, pack: Path) -> None:
    def jobs(d: Path):
        f = d / "crons" / "domain-crons.json"
        if not f.is_file():
            return []
        try:
            return json.loads(f.read_text())
        except Exception as e:
            add("FAIL", "crons", f"unparseable {f} ({e})")
            return []
    hj, pj = jobs(host), jobs(pack)
    hnames = {j.get("name") for j in hj}
    hids = {j.get("id") for j in hj}
    # (id, name) pairs already in the host — the pack's OWN job from a previous
    # install (install-domain re-run): already installed, not a conflict.
    hpairs = {(j.get("id"), j.get("name")) for j in hj}
    for j in pj:
        if (j.get("id"), j.get("name")) in hpairs:
            add("INFO", "crons", f"job '{j.get('name')}' already installed (same id) — no action")
            continue
        if j.get("name") in hnames:
            add("FAIL", "crons", f"job name collision: {j.get('name')}")
        if j.get("id") in hids:
            add("FAIL", "crons", f"job ID collision: {j.get('id')} ({j.get('name')}) — remint")
    def prompts(d: Path):
        f = d / "crons" / "engine-template-prompts.json"
        try:
            return json.loads(f.read_text()) if f.is_file() else {}
        except Exception:
            return {}
    both = sorted(set(prompts(host)) & set(prompts(pack)))
    for k in both:
        add("WARN", "crons", f"engine-template prompt collision on job '{k}' — the shared "
                             "engine lane can carry ONE prompt: HOST WINS, pack prompt skipped")


def check_configs(host: Path, pack: Path) -> None:
    hc, pc = host / "config", pack / "config"
    if pc.is_dir():
        for f in pc.iterdir():
            if f.is_file() and (hc / f.name).is_file():
                add("WARN", "configs", f"config file '{f.name}' exists in both — id-keyed "
                                       "merge required (never overwrite)")
                if "rules" in f.name:
                    def ids(p):
                        try:
                            return {r.get("id") for r in (yaml.safe_load(p.read_text()) or {}).get("rules", [])
                                    if isinstance(r, dict)}
                        except Exception:
                            return set()
                    def rules_by_id(p):
                        try:
                            return {r.get("id"): r for r in
                                    (yaml.safe_load(p.read_text()) or {}).get("rules", [])
                                    if isinstance(r, dict)}
                        except Exception:
                            return {}
                    hr, pr = rules_by_id(hc / f.name), rules_by_id(f)
                    dup = sorted((ids(hc / f.name) & ids(f)) - {None})
                    # idempotency: an id whose rule body is IDENTICAL both sides is a
                    # previous merge of this same pack, not a conflict.
                    same = [d for d in dup if hr.get(d) == pr.get(d)]
                    differ = [d for d in dup if hr.get(d) != pr.get(d)]
                    if same:
                        add("INFO", "configs", f"rule id(s) already merged (identical) in "
                                               f"{f.name}: {same} — no action")
                    if differ:
                        add("FAIL", "configs", f"rule id collision in {f.name}: {differ}")


def check_feeds(host: Path, pack: Path) -> None:
    def urls(d: Path):
        out = set()
        for f in (d / "feeds").glob("*.opml") if (d / "feeds").is_dir() else []:  # glob-ok: pack feeds/ is a flat dir, not a sharded content namespace
            out |= set(re.findall(r'xmlUrl="([^"]+)"', f.read_text(encoding="utf-8", errors="replace")))
        return out
    both = urls(host) & urls(pack)
    if both:
        add("WARN", "feeds", f"{len(both)} feed URL(s) present in both — dedupe on merge "
                             "(double-fetch = double raws)")


def check_streams_dashboards(host: Path, pack: Path) -> None:
    scripts = list((pack / "crons" / "scripts").glob("*.py")) if (pack / "crons" / "scripts").is_dir() else []  # glob-ok: pack crons/scripts/ is a flat dir, not a sharded content namespace
    # idempotency: a pack script whose byte-identical copy is already staged in the
    # host is a previous install of THIS pack — the streams/dashboards it references
    # are its own, not a foreign collision.
    def _installed(s: Path) -> bool:
        h = host / "crons" / "scripts" / s.name
        return h.is_file() and h.read_bytes() == s.read_bytes()
    streams: dict[str, bool] = {}
    dashes: dict[str, bool] = {}
    for s in scripts:
        t = s.read_text(encoding="utf-8", errors="replace")
        own = _installed(s)
        for st in re.findall(r'raw/([a-z0-9-]+)', t):
            streams[st] = streams.get(st, True) and own
        for dn in re.findall(r'dashboards/([a-z0-9-]+)\.md', t):
            dashes[dn] = dashes.get(dn, True) and own
    for st in sorted(streams):
        if (host / "raw" / st).is_dir():
            if streams[st]:
                add("INFO", "raw-streams", f"raw/{st}/ is this pack's own already-installed "
                                           "stream (scripts staged identical) — no action")
            else:
                add("FAIL", "raw-streams", f"raw stream 'raw/{st}/' already used by host — "
                                           "two packs interleaving one stream corrupts ingest provenance")
    for dname in sorted(dashes):
        if (host / "wiki" / "dashboards" / f"{dname}.md").is_file():
            if dashes[dname]:
                add("INFO", "dashboards", f"dashboards/{dname}.md is this pack's own "
                                          "already-installed dashboard — no action")
            else:
                add("FAIL", "dashboards", f"pack writes dashboards/{dname}.md which host already "
                                          "maintains — prefix the pack's dashboard")


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("pack")
    ap.add_argument("--additions", default="", help="host-schema-additions.yaml (taxonomy-augmenting shape)")
    ap.add_argument("--subtree", action="store_true",
                    help="walk-up subtree shape: types don't land in the host root, so a "
                         "root-contract difference is awareness (WARN), not a block")
    a = ap.parse_args(argv)
    host, pack = Path(a.host), Path(a.pack)
    if not (host / "schema.yaml").is_file() or not (pack / "schema.yaml").is_file():
        print("usage error: host and pack must both carry schema.yaml", file=sys.stderr)
        return 2
    additions = Path(a.additions) if a.additions else None

    check_types(host, pack, additions, subtree=a.subtree)
    check_namespaces(host, pack, subtree=a.subtree)
    check_crons(host, pack)
    check_configs(host, pack)
    check_feeds(host, pack)
    check_streams_dashboards(host, pack)

    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    fails = 0
    print(f"coinstall preflight: {pack.name} -> {host.name}")
    for level, area, msg in sorted(FINDINGS, key=lambda f: (order[f[0]], f[1])):
        print(f"  {level:<4} [{area}] {msg}")
        fails += level == "FAIL"
    if not FINDINGS:
        print("  clean — no collisions found")
    print(f"verdict: {'FAIL — resolve before installing' if fails else 'OK to install (apply WARN rules deliberately)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
