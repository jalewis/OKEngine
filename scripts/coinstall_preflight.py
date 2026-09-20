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
from datetime import datetime, timezone
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


def _namespace_owned_by_incoming_pack(host: Path, pack: Path, namespace: str) -> bool:
    meta = _yaml(pack / "pack.yaml")
    name = str(meta.get("name") or pack.name)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-") or "pack"
    manifest = host / ".okengine" / "installed-domains" / f"{safe}.json"
    try:
        state = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if namespace in (state.get("owned_namespaces") or {}):
        return True
    # Conservative migration for manifests written before namespace ownership was recorded: the
    # install-domain merge leaves an exact pack-name marker adjacent to its schema entries.
    try:
        return f"# co-installed ({name}, framework install-domain)" in (host / "schema.yaml").read_text()
    except OSError:
        return False


_GENERATED_TYPES = {"dashboard"}


def _page_types(directory: Path) -> tuple[set[str], int]:
    """Frontmatter `type:` of every non-generated page under `directory`, and how many were read.

    Cheap by construction: only the head of each file is read, because `type:` is frontmatter and
    frontmatter is first. Generated per-directory artifacts (INDEX.md, dashboards) are skipped --
    they are written by the engine into whatever namespace they describe, so they say nothing about
    who owns it, and counting them as foreign content would veto every adoption.
    """
    types: set[str] = set()
    read = 0
    for page in sorted(directory.rglob("*.md")):  # rglob-ok: bounded by one namespace, run by hand
        if page.name == "INDEX.md":
            continue
        try:
            with page.open(encoding="utf-8", errors="replace") as fh:
                head = fh.read(512)
        except OSError:
            continue
        match = re.search(r"^type:\s*(\S+)", head, re.M)
        value = match.group(1).strip().strip("\"'") if match else ""
        if value in _GENERATED_TYPES:
            continue
        read += 1
        types.add(value or "<untyped>")
    return types, read


def _owned_types(pack: Path) -> set[str]:
    owns = (_yaml(pack / "pack.yaml").get("owns") or {}).get("types") or []
    return {str(t).strip() for t in owns if str(t).strip()} if isinstance(owns, list) else set()


def _namespace_content_is_incoming_packs(host: Path, pack: Path, namespace: str) -> tuple[bool, str]:
    """Is `wiki/<namespace>/` this pack's own content, on the evidence of what is in it?

    A namespace the host schema does not declare, holding only pages whose types the incoming pack
    owns EXCLUSIVELY, is this pack's content composed in ahead of its declaration -- an adoption,
    not a collision (okengine#812). The exclusivity requirement is the whole safety argument: if
    the host's pack.yaml claims the type too, ownership is genuinely ambiguous and no directory
    scan can resolve it, so the FAIL stands and a human reconciles `owns.types` first.
    """
    incoming = _owned_types(pack)
    if not incoming:
        return False, "the pack declares no owned types to attribute the content to"
    contested = incoming & _owned_types(host)
    types, read = _page_types(host / "wiki" / namespace)
    if not read:
        return True, "empty of pages"
    foreign = types - incoming
    if foreign:
        return False, f"holds {read} page(s) with type(s) the pack does not own: " \
                      f"{', '.join(sorted(foreign)[:4])}"
    ambiguous = types & contested
    if ambiguous:
        return False, (f"holds {read} page(s) whose type(s) BOTH packs claim in owns.types "
                       f"({', '.join(sorted(ambiguous)[:4])}) -- reconcile ownership first")
    return True, f"holds {read} page(s), all of types this pack owns exclusively"


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
        if (ns in host_part and host_part.get(ns) == pack_part.get(ns)
                and _namespace_owned_by_incoming_pack(host, pack, ns)):
            add("INFO", "namespaces", f"'wiki/{ns}/' already installed (host partitioning "
                                      "matches the pack's) — no action")
            continue
        # The host schema does not declare this namespace, yet its directory exists and holds
        # nothing but this pack's own types: the pack was composed in ahead of its declaration
        # (okengine#811 found 524 such pages on a live vault, in a namespace no schema declared and
        # therefore outside every partition guard). Refusing here is what pushed that install off
        # the supported path in the first place; adopting it is what lets the declaration finally
        # land. WARN, never silence -- the merge that follows will reshelve the content under the
        # declared strategy, which is a real change to a live vault.
        if ns not in host_part:
            adoptable, why = _namespace_content_is_incoming_packs(host, pack, ns)
            if adoptable:
                add("WARN", "namespaces",
                    f"'wiki/{ns}/' exists but the host schema does not declare it; {why} — "
                    f"ADOPTING into this pack's declared namespace. An INITIAL install merges the "
                    f"declaration, after which the reshelve drain files that content under the "
                    f"pack's declared strategy; --refresh is runtime-only and merges no schema.")
                continue
            add("FAIL", "namespaces", f"pack namespace 'wiki/{ns}/' already exists in host and "
                                      f"{why} — reconcile ownership or pick a subtree name")
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


def _host_lane_scripts(host: Path, pack: Path) -> set[str]:
    """Script basenames the HOST's own cron jobs attribute to this pack, by job-name prefix.

    `<pack>-<lane>` is the required naming for a co-installed job (check_crons enforces the prefix),
    so it is the host's own record of who owns a lane script -- independent of the script's current
    contents, and available even when no ownership manifest was ever written.
    """
    name = str(_yaml(pack / "pack.yaml").get("name") or pack.name)
    path = host / "crons" / "domain-crons.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    rows = raw.get("jobs", []) if isinstance(raw, dict) else raw
    return {Path(str(row.get("script"))).name for row in rows
            if isinstance(row, dict) and str(row.get("name") or "").startswith(f"{name}-")
            and row.get("script")}


def check_streams_dashboards(host: Path, pack: Path) -> None:
    scripts = list((pack / "crons" / "scripts").glob("*.py")) if (pack / "crons" / "scripts").is_dir() else []  # glob-ok: pack crons/scripts/ is a flat dir, not a sharded content namespace
    # idempotency: a pack script whose byte-identical copy is already staged in the
    # host is a previous install of THIS pack — the streams/dashboards it references
    # are its own, not a foreign collision.
    lanes = _host_lane_scripts(host, pack)

    def _installed(s: Path) -> bool:
        h = host / "crons" / "scripts" / s.name
        if h.is_file() and h.read_bytes() == s.read_bytes():
            return True
        # Byte-identity cannot be the only proof of ownership on a REFRESH, whose whole purpose is
        # that the pack's script has changed and should replace the host's. Keying ownership to
        # bytes means a pack can never refresh a script that writes a dashboard: the moment it
        # moves ahead of the deployed copy, its own dashboard reads as a foreign collision. The
        # host's cron jobs name their owner, so a script driven by a job named `<pack>-...` is this
        # pack's however far its contents have drifted (okengine#812).
        return s.name in lanes
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


_TRUST_RANK = {"public": 0, "private": 1}   # higher = more restrictive


OVERRIDES_REL = Path(".okengine") / "coinstall-overrides.yaml"


def load_overrides(host: Path) -> dict:
    """Operator decisions recorded for this deployment. Absent file = no overrides, never an error."""
    path = host / OVERRIDES_REL
    return _yaml(path) if path.is_file() else {}


def trust_override(host: Path, pack_name: str, guest_trust: str, host_trust: str) -> dict | None:
    """The recorded acceptance for THIS exposure, or None.

    Consent is to one specific (guest, host) trust pair, not to a pack. If either side's declared
    trust later changes, the operator accepted a different exposure than the one now proposed and
    the override no longer applies -- re-consent is required. That is the whole difference between
    this and editing `trust:` in a pack file, where the decision vanishes into a one-word diff.
    """
    entry = ((load_overrides(host).get("trust_exposure") or {}) or {}).get(pack_name)
    if not isinstance(entry, dict):
        return None
    if str(entry.get("guest_trust") or "").strip().lower() != guest_trust:
        return None
    if str(entry.get("host_trust") or "").strip().lower() != host_trust:
        return None
    return entry if str(entry.get("reason") or "").strip() else None


def record_trust_override(host: Path, pack_name: str, guest_trust: str, host_trust: str,
                          reason: str) -> Path:
    """Write the operator's acceptance. A reason is mandatory: an exposure accepted for no stated
    reason cannot be reviewed later, which is the only thing that makes this better than a bypass."""
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("a recorded trust-exposure override requires a reason")
    path = host / OVERRIDES_REL
    data = load_overrides(host)
    data.setdefault("trust_exposure", {})[pack_name] = {
        "guest_trust": guest_trust,
        "host_trust": host_trust,
        "accepted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Recorded by `framework install-domain --accept-trust-exposure`. Each entry is an\n"
        "# operator's explicit acceptance of ONE exposure: serving a more-private guest at this\n"
        "# host's trust. It is keyed to both trust values, so changing either one revokes it.\n"
        "# Every standing entry is reported by `framework validate` — accepted is not invisible.\n"
        + yaml.safe_dump(data, sort_keys=True, default_flow_style=False),
        encoding="utf-8")
    return path


def check_trust(host: Path, pack: Path, pending_acceptance: str = "") -> None:
    """The reader/cockpit serve ONE global trust — the HOST's, frozen at first deploy by
    ensure-runtime. Co-installing a guest serves its content at the HOST's trust, so a guest that is
    MORE RESTRICTIVE than the host (a `private` guest on a `public` host) is exposed on the
    unauthenticated reader — the leak. The reverse (a public guest on a private host) is merely
    over-protected and safe. FAIL only the exposing direction (invariant-audit HIGH #2). An unknown
    trust token is treated as most-restrictive (fail-safe)."""
    ht = str((_yaml(host / "pack.yaml") or {}).get("trust") or "private").strip().lower()
    gt = str((_yaml(pack / "pack.yaml") or {}).get("trust") or "private").strip().lower()
    if _TRUST_RANK.get(ht, 1) < _TRUST_RANK.get(gt, 1):
        name = str((_yaml(pack / "pack.yaml") or {}).get("name") or pack.name)
        # A dry run must not write to the host, so an acceptance being made in THIS invocation is
        # passed in rather than recorded first. Previewing the plan and consenting are different
        # acts, and the default mode of this command is a preview.
        accepted = ({"accepted_at": "on --apply", "reason": pending_acceptance}
                    if pending_acceptance else trust_override(host, name, gt, ht))
        if accepted:
            # The exposure is real whether or not it was accepted; what the override changes is
            # whether it BLOCKS. Keeping it on the report is what stops an accepted exposure from
            # becoming an invisible one (okengine#813).
            add("WARN", "trust",
                f"host trust '{ht}' is MORE PUBLIC than guest trust '{gt}' — ACCEPTED "
                f"{accepted.get('accepted_at')}: {accepted.get('reason')}")
            return
        add("FAIL", "trust",
            f"host trust '{ht}' is MORE PUBLIC than guest trust '{gt}' — co-install would serve the "
            f"guest's content at the host's '{ht}' trust, exposing a '{gt}' pack on the host's "
            f"reader. Raise the guest's declared exposure, don't co-install it here, or accept the "
            f"exposure on the record with --accept-trust-exposure --reason '<why>'.")


def check_extension_collisions(host: Path, pack: Path, additions: Path | None = None,
                               subtree: bool = False) -> None:
    """okengine#326 [10]: a pack type/namespace that collides with an ENABLED EXTENSION's owned id
    was INVISIBLE — every other check compares against the host ROOT schema.yaml, never the enabled
    extensions that also contribute ids. The composed artifact records an `owners` map tagging each
    id `engine` / `pack` / `ext:<id>`; read it and flag a pack id colliding with an `ext:*`-owned one
    (the same collision the compose gate enforces, surfaced pre-install). Absent composed artifact or
    no enabled extensions -> nothing to check."""
    composed_path = host / ".okengine" / "composed-schema.yaml"
    if not composed_path.is_file():
        return   # no composed artifact (no enabled extensions, or not yet composed) — nothing to check
    composed = _yaml(composed_path)
    owners = composed.get("owners") or {}
    ext_types = {t: o for t, o in (owners.get("types") or {}).items()
                 if isinstance(o, str) and o.startswith("ext:")}
    ext_ns = {ns: o for ns, o in (owners.get("namespaces") or {}).items()
              if isinstance(o, str) and o.startswith("ext:")}
    if not ext_types and not ext_ns:
        return
    psch = _yaml(additions) if additions and additions.is_file() else _yaml(pack / "schema.yaml")
    ptypes = set(psch.get("types") or {})
    pack_meta = _yaml(pack / "pack.yaml")
    owned_namespaces = (pack_meta.get("owns") or {}).get("namespaces") or []
    # Compare the exact namespace set the selected installer will land. Taxonomy installs merge
    # only pack-owned namespaces (list or mapping keys); an empty owns block lands none. Subtree
    # installs copy the supplied subdomain schema, so its partitioning declaration is authoritative.
    namespace_source = ((psch.get("partitioning") or {}).get("namespaces") or {}) \
        if subtree else owned_namespaces
    if isinstance(namespace_source, dict):
        namespace_source = namespace_source.keys()
    elif not isinstance(namespace_source, list):
        namespace_source = []
    pns = {str(namespace).strip() for namespace in namespace_source if str(namespace).strip()}
    lvl = "WARN" if subtree else "FAIL"      # subtree keeps contracts separate (walk-up) -> awareness
    for t in sorted(ptypes & set(ext_types)):
        add(lvl, "types", f"type '{t}' collides with an EXTENSION-owned type ({ext_types[t]}) enabled "
            f"on the host — invisible to a root-schema-only check. "
            + ("walk-up separates the contracts; ensure pages land on the intended side"
               if subtree else "reconcile before install (rename the pack type or disable the extension)"))
    for ns in sorted(pns & set(ext_ns)):
        add(lvl, "namespaces", f"namespace 'wiki/{ns}/' collides with an EXTENSION-owned namespace "
            f"({ext_ns[ns]}) enabled on the host — reconcile before install")


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("pack")
    ap.add_argument("--additions", default="", help="host-schema-additions.yaml (taxonomy-augmenting shape)")
    ap.add_argument("--subtree", action="store_true",
                    help="walk-up subtree shape: types don't land in the host root, so a "
                         "root-contract difference is awareness (WARN), not a block")
    ap.add_argument("--assume-trust-accepted", default="",
                    help="reason for an acceptance being made in this run but not yet recorded "
                         "(install-domain passes it on a dry run, which must not write)")
    a = ap.parse_args(argv)
    host, pack = Path(a.host), Path(a.pack)
    if not (host / "schema.yaml").is_file() or not (pack / "schema.yaml").is_file():
        print("usage error: host and pack must both carry schema.yaml", file=sys.stderr)
        return 2
    additions = Path(a.additions) if a.additions else None

    check_trust(host, pack, a.assume_trust_accepted)
    check_types(host, pack, additions, subtree=a.subtree)
    check_namespaces(host, pack, subtree=a.subtree)
    check_extension_collisions(host, pack, additions, subtree=a.subtree)
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
