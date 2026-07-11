#!/usr/bin/env python3
"""framework install-domain — install a pack ALONGSIDE a host in a live deployment (okengine#173).

Automates the two co-install shapes from docs/authoring-a-pack.md §8 (first done by hand
into a live multipack deployment; the INSTALL-* docs in the two reference packs are the
spec this encodes):

  subtree   (pack ships subdomain/schema.yaml, e.g. a knowledge-state pack)
            wiki/<slug>/ walk-up sub-domain: copy the subdomain schema, create its
            namespace dirs, merge the pack's completeness rules SCOPED to the subtree's
            own types (a standalone pack's entity-type rules would flood a mature host),
            append the pack persona under a provenance marker.

  taxonomy  (pack ships subdomain/host-schema-additions.yaml, e.g. a security-taxonomy pack)
            augment the HOST's schema: merge the addition types (HOST WINS on name
            collision — reported, never overwritten), merge feeds by xmlUrl, append the
            pack's prefixed domain cron jobs + engine-template prompts (host wins on
            prompt keys), append the pack persona under the same marker.

Naming standard: the subtree/marker slug is the pack's DOMAIN slug (pack.yaml `domain:`,
else the pack dir name minus its `okpack-` prefix) — never an ad-hoc name.

Dry-run by default (prints the exact plan); `--apply` writes. Idempotent: every merge is
key-based (type name / rule id / job name / prompt key / xmlUrl / persona marker), so a
re-run applies nothing. Runs `coinstall_preflight` first and refuses on FAIL.

The write-path probes (invalid page under the subtree must REJECT; host pages untouched)
need a LIVE gateway and are printed as the post-install checklist, not run here — along
with the deploy commands that make the cron/feed merges take effect.

Usage:
  framework install-domain <deployment-dir> <pack-dir>
      [--under wiki/<slug>] [--shape subtree|taxonomy] [--apply]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent

PERSONA_MARKER = "## Installed domain:"


def _load_mod(filename: str):
    spec = importlib.util.spec_from_file_location(filename[:-3], _HERE / filename)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _yaml(p: Path) -> dict:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _insert_under(text: str, anchor_re: str, block: str) -> str | None:
    """Surgical YAML edit: insert `block` immediately after the line matching
    `anchor_re`. Live deployment schemas are heavily commented — a parse+safe_dump
    round-trip would destroy every comment (caught heading into the first real
    install), so the installer only ever ADDS lines to the existing text. Returns
    None when the anchor is missing (caller degrades to a loud manual step)."""
    m = re.search(anchor_re, text, re.M)
    if not m:
        return None
    at = text.index("\n", m.end()) + 1
    return text[:at] + block + text[at:]


def _dump_entry(key: str, val, indent: int) -> str:
    pad = " " * indent
    flow = yaml.safe_dump(val, default_flow_style=True, sort_keys=False,
                          width=10**6).strip()
    return f"{pad}{key}: {flow}\n"


class Plan:
    """Collected steps; dry-run prints them, --apply executes them in order."""

    def __init__(self, apply: bool):
        self.apply = apply
        self.steps: list[tuple[str, object]] = []   # (description, thunk|None)
        self.infos: list[str] = []
        self.warns: list[str] = []
        self.fails: list[str] = []

    def step(self, desc: str, thunk=None):
        self.steps.append((desc, thunk))

    def info(self, msg: str):
        self.infos.append(msg)

    def warn(self, msg: str):
        self.warns.append(msg)

    def fail(self, msg: str):
        self.fails.append(msg)

    def run(self) -> int:
        for i in self.infos:
            print(f"  INFO  {i}")
        for w in self.warns:
            print(f"  WARN  {w}")
        for f in self.fails:
            print(f"  FAIL  {f}")
        if self.fails:
            print("install-domain: blocked by FAIL finding(s) above.")
            return 1
        mode = "APPLY" if self.apply else "PLAN (dry-run — use --apply to write)"
        print(f"install-domain: {mode}")
        for desc, thunk in self.steps:
            print(f"  - {desc}")
            if self.apply and thunk is not None:
                thunk()
        if not self.steps:
            print("  (nothing to do — already installed)")
        return 0


def detect_shape(pack: Path, flag: str | None) -> str | None:
    sub = pack / "subdomain"
    has_subtree = (sub / "schema.yaml").is_file()
    has_taxonomy = (sub / "host-schema-additions.yaml").is_file()
    if flag:
        want = {"subtree": has_subtree, "taxonomy": has_taxonomy}[flag]
        return flag if want else None
    if has_subtree and has_taxonomy:
        return "both"
    if has_subtree:
        return "subtree"
    if has_taxonomy:
        return "taxonomy"
    return None


def domain_slug(pack: Path, under: str | None) -> str:
    if under:
        slug = under.strip("/")
        if slug.startswith("wiki/"):
            slug = slug[len("wiki/"):]
        return slug.strip("/")
    d = _yaml(pack / "pack.yaml").get("domain")
    if d:
        return str(d).strip().strip("/")
    name = pack.resolve().name
    return name[len("okpack-"):] if name.startswith("okpack-") else name


def pack_name(pack: Path) -> str:
    return str(_yaml(pack / "pack.yaml").get("name") or pack.resolve().name)


# ── merges (each returns plan steps; all key-based → idempotent) ─────────────
def merge_rules(host: Path, pack: Path, scope_types: set[str], plan: Plan) -> None:
    """Merge the pack's completeness rules whose when.type is one of the pack's
    OWN co-installed types. Everything else is skipped loudly."""
    src = pack / "config" / "completeness-rules.yaml"
    if not src.is_file():
        return
    dst = host / "config" / "completeness-rules.yaml"
    have = {r.get("id") for r in (_yaml(dst).get("rules") or [])}
    add, skipped = [], []
    for r in (_yaml(src).get("rules") or []):
        rid, rtype = r.get("id"), ((r.get("when") or {}).get("type"))
        if rid in have:
            continue
        (add if rtype in scope_types else skipped).append(r)
    if skipped:
        plan.warn(f"completeness rules NOT merged (host-owned/entity-world types, "
                  f"see INSTALL doc): {[r.get('id') for r in skipped]}")
    if not add:
        return

    def _do(add=add):
        # text-append, never parse+rewrite (a safe_dump round-trip strips the file's
        # comments). Valid because `rules:` is the file's trailing top-level list;
        # the parse-back assert catches any file where that stops being true.
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.is_file():
            dst.write_text("rules:\n", encoding="utf-8")
        t = dst.read_text(encoding="utf-8")
        assert re.search(r"^rules:\s*(#.*)?$", t, re.M), \
            "host completeness-rules.yaml has no top-level rules: block"
        # match the file's EXISTING list-item indent (a 0-indent list with 2-indent
        # appendees is a YAML parse error); default 2 when the list is empty
        mi = re.search(r"^rules:\s*(?:#.*)?\n((?:\s*#.*\n)*)(\s*)- ", t, re.M)
        pad = mi.group(2) if mi else "  "
        block = (pad + "# --- co-installed rules (" + pack_name(pack)
                 + ", framework install-domain) ---\n")
        dumped = yaml.safe_dump(add, sort_keys=False, allow_unicode=True,
                                default_flow_style=False)
        block += "".join(pad + ln + "\n" if ln.strip() else "\n"
                         for ln in dumped.splitlines())
        new_t = t.rstrip("\n") + "\n" + block
        got = {r.get("id") for r in (yaml.safe_load(new_t).get("rules") or [])}
        assert {r.get("id") for r in add} <= got, "rule merge failed to parse back"
        dst.write_text(new_t, encoding="utf-8")
    plan.step(f"merge {len(add)} completeness rule(s) into config/completeness-rules.yaml: "
              f"{[r.get('id') for r in add]}", _do)


def merge_types(host: Path, pack: Path, plan: Plan) -> set[str]:
    """Taxonomy shape: fold addition types into the host schema. Host wins."""
    additions = _yaml(pack / "subdomain" / "host-schema-additions.yaml").get("types") or {}
    hschema = host / "schema.yaml"
    htypes = _yaml(hschema).get("types") or {}
    add, kept = {}, []
    for tname, tdef in additions.items():
        if tname in htypes:
            hreq = set((htypes[tname] or {}).get("required") or [])
            preq = set((tdef or {}).get("required") or [])
            if hreq != preq:
                plan.warn(f"type '{tname}': host already declares it with different "
                          f"required fields (host {sorted(hreq)} vs pack {sorted(preq)}) "
                          f"— host wins, check compatibility")
            kept.append(tname)
        else:
            add[tname] = tdef
    if kept:
        plan.info(f"skip {len(kept)} type(s) the host already owns (host wins): {kept}")
    if add:
        def _do(add=add):
            t = hschema.read_text(encoding="utf-8")
            block = "".join(_dump_entry(k, v or {}, 2) for k, v in sorted(add.items()))
            block = ("  # --- co-installed types (okpack: "
                     f"{pack_name(pack)}, framework install-domain) ---\n" + block)
            new_t = _insert_under(t, r"^types:\s*(#.*)?$", block)
            assert new_t is not None, "host schema has no top-level `types:` block"
            got = set((yaml.safe_load(new_t).get("types") or {}))
            assert set(add) <= got, "type merge failed to parse back"
            hschema.write_text(new_t, encoding="utf-8")
        plan.step(f"merge {len(add)} type(s) into schema.yaml: {sorted(add)}", _do)
    return set(additions)


def merge_type_aliases(host: Path, pack: Path, plan: Plan) -> None:
    """Taxonomy shape: fold a guest's `type_aliases` (alias -> canonical) into the host schema
    so old/variant type names resolve to the composed vault's canonical types — the STIX-name
    reconciliation path (okengine#181; the runtime backfill is scripts/cron/schema_type_drain.py,
    which reads the host's merged type_aliases). Host wins on a key it already maps; an alias
    whose KEY equals a host-OWNED type is SKIPPED — it would shadow a real type (coinstall
    preflight FAILs that case up front, so this is belt-and-braces)."""
    additions = _yaml(pack / "subdomain" / "host-schema-additions.yaml").get("type_aliases") or {}
    if not isinstance(additions, dict) or not additions:
        return
    hschema = host / "schema.yaml"
    hs = _yaml(hschema)
    hmap = hs.get("type_aliases") if isinstance(hs.get("type_aliases"), dict) else {}
    htypes = set(hs.get("types") or {})
    add, kept, shadowed = {}, [], []
    for alias, canon in additions.items():
        if alias in htypes:
            shadowed.append(alias)                 # would shadow a real host type — skip
        elif alias in hmap:
            kept.append(alias)                     # host already maps it — host wins
        else:
            add[str(alias)] = str(canon)
    if shadowed:
        plan.warn(f"skip {len(shadowed)} alias(es) that shadow a host-owned type: {shadowed}")
    if kept:
        plan.info(f"skip {len(kept)} alias(es) the host already declares (host wins): {kept}")
    if not add:
        return

    def _do(add=add):
        t = hschema.read_text(encoding="utf-8")
        # alias values are plain type-name scalars — format them directly. (_dump_entry is for
        # DICT values; safe_dump of a bare scalar emits a `...` doc-end marker that corrupts the
        # append.) safe_dump each key/value so an odd name is still quoted correctly.
        def _scalar(x):
            return yaml.safe_dump(str(x), default_flow_style=True, width=10**6).split("\n")[0].strip()
        # 1. block-style `type_aliases:` (own line) — insert the new pairs under it.
        block = ("  # --- co-installed type_aliases (okpack: "
                 f"{pack_name(pack)}, framework install-domain) ---\n"
                 + "".join(f"  {_scalar(k)}: {_scalar(v)}\n" for k, v in sorted(add.items())))
        new_t = _insert_under(t, r"^type_aliases:\s*(#.*)?$", block)
        if new_t is None:
            # 2. INLINE flow map `type_aliases: {a: b, ...}` (possibly multi-line) — inject the new
            # pairs before the closing `}` rather than appending a SECOND `type_aliases:` key, which
            # YAML duplicate-key resolution would let clobber the host's own aliases (okengine#181).
            m = re.search(r"^(type_aliases:\s*\{)(.*?)(\})", t, re.M | re.S)
            if m:
                inner = m.group(2).strip().rstrip(",")
                pairs = ", ".join(f"{_scalar(k)}: {_scalar(v)}" for k, v in sorted(add.items()))
                merged = f"{m.group(1)}{inner + ', ' if inner else ''}{pairs}{m.group(3)}"
                new_t = t[:m.start()] + merged + t[m.end():]
            else:
                # 3. no type_aliases anywhere — add a fresh block.
                new_t = t.rstrip("\n") + "\ntype_aliases:\n" + block
        got = yaml.safe_load(new_t).get("type_aliases") or {}
        assert set(add) <= set(got), "type_alias merge failed to parse back"
        hschema.write_text(new_t, encoding="utf-8")
    plan.step(f"merge {len(add)} type_alias(es) into schema.yaml: {sorted(add)}", _do)


def merge_namespaces(host: Path, pack: Path, plan: Plan) -> None:
    """Taxonomy shape: the pack's OWNED namespaces (pack.yaml owns.namespaces) must
    land in the host schema, or every page written into them is refused by the
    undeclared-namespace guard (okengine#115) — and their permission entries (e.g. a
    human-authored contracts/ register) silently would not exist. Carries the pack's
    partitioning def + permissions + tier entry; host wins if the namespace exists."""
    owned = [str(n) for n in ((_yaml(pack / "pack.yaml").get("owns") or {}).get("namespaces") or [])]
    if not owned:
        return
    ps = _yaml(pack / "schema.yaml")
    hschema = host / "schema.yaml"
    hs = _yaml(hschema)
    hns = ((hs.get("partitioning") or {}).get("namespaces") or {})
    add_part, add_perm, add_tier = {}, {}, {}
    for ns in owned:
        if ns in hns:
            plan.warn(f"namespace '{ns}' already declared by the host — host wins; "
                      "verify the partitioning/permission contract matches the pack's")
            continue
        add_part[ns] = ((ps.get("partitioning") or {}).get("namespaces") or {}).get(ns) or {"strategy": "flat"}
        pperm = (((ps.get("permissions") or {}).get("namespaces") or {}).get(ns))
        if pperm:
            add_perm[ns] = pperm
        ptier = (((ps.get("tier") or {}).get("namespaces") or {}).get(ns))
        if ptier:
            add_tier[ns] = ptier
    if not add_part:
        return

    def _do(add_part=add_part, add_perm=add_perm, add_tier=add_tier):
        t = hschema.read_text(encoding="utf-8")
        mark = f"    # co-installed ({pack_name(pack)}, framework install-domain)\n"
        blk = mark + "".join(_dump_entry(k, v, 4) for k, v in sorted(add_part.items()))
        if re.search(r"^partitioning:\s*(#.*)?$", t, re.M):
            m = re.search(r"^partitioning:\s*(#.*)?$", t, re.M)
            seg = t[m.end():]
            m2 = re.search(r"^  namespaces:\s*(#.*)?$", seg, re.M)
            if m2:
                at = m.end() + seg.index("\n", m2.end()) + 1
                t = t[:at] + blk + t[at:]
            else:
                at = t.index("\n", m.end()) + 1
                t = t[:at] + "  namespaces:\n" + blk + t[at:]
        else:
            # host schema has no partitioning block at all — append one (top-level
            # append is always valid YAML)
            t = t.rstrip("\n") + "\npartitioning:\n  namespaces:\n" + blk
        if add_perm:
            blk = mark + "".join(_dump_entry(k, v, 4) for k, v in sorted(add_perm.items()))
            m = re.search(r"^permissions:\s*(#.*)?$", t, re.M)
            if m:
                seg = t[m.end():]
                m2 = re.search(r"^  namespaces:\s*(#.*)?$", seg, re.M)
                if m2:
                    at = m.end() + seg.index("\n", m2.end()) + 1
                    t = t[:at] + blk + t[at:]
                else:
                    at = t.index("\n", m.end()) + 1
                    t = t[:at] + "  namespaces:\n" + blk + t[at:]
            else:
                t = t.rstrip("\n") + "\npermissions:\n  namespaces:\n" + blk
        if add_tier:
            m = re.search(r"^tier:\s*(#.*)?$", t, re.M)
            if m:
                seg = t[m.end():]
                m2 = re.search(r"^  namespaces:\s*(#.*)?$", seg, re.M)
                if m2:
                    at = m.end() + seg.index("\n", m2.end()) + 1
                    t = t[:at] + mark + "".join(_dump_entry(k, v, 4)
                                                for k, v in sorted(add_tier.items())) + t[at:]
        got = yaml.safe_load(t)
        assert set(add_part) <= set((got.get("partitioning") or {}).get("namespaces") or {}), \
            "namespace merge failed to parse back"
        if add_perm:
            assert set(add_perm) <= set((got.get("permissions") or {}).get("namespaces") or {}), \
                "permission merge failed to parse back"
        hschema.write_text(t, encoding="utf-8")
        wiki = host / "wiki"
        for ns in add_part:
            (wiki / ns).mkdir(parents=True, exist_ok=True)
    perm_note = f" (+ permissions for {sorted(add_perm)})" if add_perm else ""
    plan.step(f"merge {len(add_part)} owned namespace(s) into schema partitioning"
              f"{perm_note} + create wiki dirs: {sorted(add_part)}", _do)


def merge_feeds(host: Path, pack: Path, plan: Plan) -> None:
    src = pack / "feeds" / "feeds.opml"
    if not src.is_file():
        return
    dst = host / "feeds" / "feeds.opml"
    if not dst.is_file():
        plan.step(f"copy {src} -> feeds/feeds.opml",
                  lambda: (dst.parent.mkdir(parents=True, exist_ok=True),
                           dst.write_bytes(src.read_bytes())))
        return
    try:
        stree, dtree = ET.parse(src), ET.parse(dst)
    except ET.ParseError as e:
        plan.fail(f"feeds OPML unparseable: {e}")
        return
    have = {o.get("xmlUrl") for o in dtree.getroot().iter("outline") if o.get("xmlUrl")}
    new = [o for o in stree.getroot().iter("outline")
           if o.get("xmlUrl") and o.get("xmlUrl") not in have]
    if not new:
        return

    def _do(new=new):
        # text-insert before </body>, never ET.write (which drops the file's XML
        # comments — deployment OPMLs carry provenance headers worth keeping)
        t = dst.read_text(encoding="utf-8")
        assert "</body>" in t, "host feeds.opml has no </body>"
        def esc(v):
            return (v or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        lines = "".join(
            f'    <outline text="{esc(o.get("text") or o.get("title") or "")}" '
            f'type="rss" xmlUrl="{esc(o.get("xmlUrl"))}"/>\n' for o in new)
        lines = f"    <!-- co-installed feeds ({pack_name(pack)}, framework install-domain) -->\n" + lines
        new_t = t.replace("</body>", lines + "  </body>", 1)
        got = {o.get("xmlUrl") for o in ET.fromstring(new_t).iter("outline")}
        assert {o.get("xmlUrl") for o in new} <= got, "feed merge failed to parse back"
        dst.write_text(new_t, encoding="utf-8")
    plan.step(f"merge {len(new)} feed(s) into feeds/feeds.opml (deduped by xmlUrl)", _do)


def merge_crons(host: Path, pack: Path, plan: Plan) -> None:
    pname = pack_name(pack)
    src = pack / "crons" / "domain-crons.json"
    if src.is_file():
        dst = host / "crons" / "domain-crons.json"
        cur = json.loads(dst.read_text(encoding="utf-8")) if dst.is_file() else []
        cur_jobs = cur["jobs"] if isinstance(cur, dict) else cur
        have = {j.get("name") for j in cur_jobs}
        add, unprefixed = [], []
        for j in json.loads(src.read_text(encoding="utf-8")):
            if j.get("name") in have:
                continue
            if not str(j.get("name", "")).startswith(f"{pname}-"):
                unprefixed.append(j.get("name"))
                continue
            add.append(j)
        if unprefixed:
            plan.warn(f"cron job(s) skipped — co-installed domain jobs must be "
                      f"'{pname}-' prefixed: {unprefixed}")
        if add:
            def _do(add=add, cur=cur, cur_jobs=cur_jobs, dst=dst):
                cur_jobs.extend(add)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
            plan.step(f"append {len(add)} domain cron job(s): {[j['name'] for j in add]}", _do)
    src = pack / "crons" / "engine-template-prompts.json"
    if src.is_file():
        # Engine-template prompts NEVER auto-merge in a co-install. Shared engine
        # lanes (daily-brief, trends-refresh, prediction-*) are per-VAULT decisions
        # the HOST already made — including the decision to leave a stub promptless
        # (a promptless stub is SKIPPED; merging a prompt would silently ACTIVATE
        # the lane). First real install caught this: the incoming pack's generic
        # scaffold prompts would have switched on four lanes the host runs without.
        # A pack lane worth keeping distinct belongs as a PREFIXED DOMAIN job (the
        # sec threat-brief rule).
        new = json.loads(src.read_text(encoding="utf-8"))
        dst = host / "crons" / "engine-template-prompts.json"
        cur = json.loads(dst.read_text(encoding="utf-8")) if dst.is_file() else {}
        extra = sorted(set(new) - set(cur))
        if extra:
            plan.info(f"pack ships engine-template prompt(s) the host does not use: "
                      f"{extra} — NOT merged (shared lanes are the host's decision; "
                      "adopt deliberately, or re-add a distinct lane as a prefixed "
                      "domain job)")


def merge_lane_scripts(host: Path, pack: Path, plan: Plan) -> None:
    """Copy the pack's lane scripts (crons/scripts/*.py) into the host's staging dir —
    a merged domain job references its script by name, and deploy-cron-scripts stages
    from the HOST deployment only (first real install shipped a job whose script was
    never staged; the deploy verifier caught it, this closes it at install time).
    Byte-identical existing copies are the idempotent no-op; differing content is a
    name collision and FAILs."""
    src = pack / "crons" / "scripts"
    if not src.is_dir():
        return
    dst = host / "crons" / "scripts"
    copy, clash = [], []
    for f in sorted(src.glob("*.py")):  # glob-ok: pack crons/scripts/ is a flat dir, not a sharded content namespace
        d = dst / f.name
        if not d.is_file():
            copy.append(f.name)
        elif d.read_bytes() != f.read_bytes():
            clash.append(f.name)
    for n in clash:
        plan.fail(f"lane script name collision: crons/scripts/{n} exists in the host "
                  "with DIFFERENT content — rename the pack's script (prefix convention)")
    if copy:
        def _do(copy=copy):
            dst.mkdir(parents=True, exist_ok=True)
            for n in copy:
                (dst / n).write_bytes((src / n).read_bytes())
        plan.step(f"copy {len(copy)} lane script(s) into crons/scripts/: {copy}", _do)


def _persona_provenance(pack: Path) -> str:
    """A wording-INDEPENDENT identity marker for a pack's persona block.

    The idempotency check must not depend on the pack's PERSONA.md first line embedding the
    slug / pack-name: a pack shipping a friendly '## Installed domain: <title>' heading (no
    slug) slipped past the marker heuristic below, so re-apply double-appended the section.
    This provenance comment always carries the pack identity and is emitted on every append."""
    return f"<!-- okengine:installed-domain {pack_name(pack)} -->"


def append_persona(host: Path, pack: Path, slug: str, plan: Plan) -> None:
    src = pack / "subdomain" / "PERSONA.md"
    dst = host / "CLAUDE.md"
    prov = _persona_provenance(pack)
    marker = f"{PERSONA_MARKER} "
    if dst.is_file():
        existing = dst.read_text(encoding="utf-8")
        if prov in existing:
            return          # already installed by this engine (identity provenance marker present)
        for line in existing.splitlines():   # legacy heuristic — hosts installed before the provenance marker
            if line.startswith(marker) and (f"okpack-{slug}" in line or pack_name(pack) in line
                                            or f"`wiki/{slug}/`" in line):
                return
    if not src.is_file():
        plan.warn(f"pack ships no subdomain/PERSONA.md — append the persona section "
                  f"to CLAUDE.md manually (marker: '{PERSONA_MARKER} … ({pack_name(pack)})')")
        return
    body = src.read_text(encoding="utf-8").strip()
    if not body.startswith(PERSONA_MARKER):
        body = f"{PERSONA_MARKER} {slug} ({pack_name(pack)})\n\n{body}"

    def _do(body=body, prov=prov):
        with dst.open("a", encoding="utf-8") as f:
            f.write("\n\n" + prov + "\n" + body + "\n")
    plan.step(f"append persona section to CLAUDE.md ('{body.splitlines()[0]}')", _do)


def install_subtree(host: Path, pack: Path, slug: str, plan: Plan) -> None:
    sschema = pack / "subdomain" / "schema.yaml"
    sub_types = set(_yaml(sschema).get("types") or {})
    # completeness rules are INSTANCE-GLOBAL (when.type matches vault-wide): a rule
    # on a type the host root ALSO declares would fire on the host's pages too —
    # that merge is an operator decision, not an automatic one.
    host_types = set(_yaml(host / "schema.yaml").get("types") or {})
    shared = sorted(sub_types & host_types)
    if shared:
        plan.info(f"rule scope excludes type(s) shared with the host root (rules are "
                  f"instance-global — merge those deliberately if wanted): {shared}")
    sub_types -= host_types
    tgt = host / "wiki" / slug
    if not (tgt / "schema.yaml").is_file():
        def _do():
            tgt.mkdir(parents=True, exist_ok=True)
            (tgt / "schema.yaml").write_bytes(sschema.read_bytes())
        plan.step(f"create wiki/{slug}/ + copy subdomain/schema.yaml (walk-up domain)", _do)
    nss = [str(n).strip("/") for n in
           ((_yaml(sschema).get("partitioning") or {}).get("namespaces") or [])]
    missing = [n for n in nss if not (tgt / n).is_dir()]
    if missing:
        plan.step(f"create namespace dir(s) under wiki/{slug}/: {missing}",
                  lambda m=missing: [(tgt / n).mkdir(parents=True, exist_ok=True) for n in m])
    merge_rules(host, pack, sub_types, plan)
    append_persona(host, pack, slug, plan)


def merge_cockpit(host: Path, pack: Path, plan: Plan) -> None:
    """Taxonomy shape: fold a guest's cockpit `tab_defs` + `tabs` (from host-schema-additions.yaml)
    into the host's `cockpit:` block, so a composed vault surfaces the guest domain's tab (a
    Vulnerabilities pack contributes its Vulnerabilities tab). Host wins on a tab it already declares.
    Presentation config, not the enforced write schema — but without this merge a guest tab had NO
    canonical home: it was only ever hand-added to the composed vault, so a recompose dropped it."""
    add_ck = _yaml(pack / "subdomain" / "host-schema-additions.yaml").get("cockpit") or {}
    add_defs = add_ck.get("tab_defs") if isinstance(add_ck.get("tab_defs"), dict) else {}
    add_names = [str(t).strip() for t in (add_ck.get("tabs") or []) if str(t).strip()]
    if not add_defs and not add_names:
        return
    hschema = host / "schema.yaml"
    hcock = _yaml(hschema).get("cockpit") or {}
    htabdefs, htabs = hcock.get("tab_defs") or {}, hcock.get("tabs") or []

    new_defs = {k: v for k, v in add_defs.items() if k not in htabdefs}
    if [k for k in add_defs if k in htabdefs]:
        plan.info(f"skip cockpit tab_def(s) the host already declares (host wins): "
                  f"{[k for k in add_defs if k in htabdefs]}")
    if new_defs:
        def _do_defs(add=new_defs):
            t = hschema.read_text(encoding="utf-8")
            block = f"    # --- co-installed tabs (okpack: {pack_name(pack)}, install-domain) ---\n"
            block += "".join(_dump_entry(k, v, 4) for k, v in add.items())
            nt = _insert_under(t, r"^  tab_defs:\s*$", block)
            assert nt is not None, "host cockpit has no `tab_defs:` block (add a cockpit: block to the host pack)"
            assert set(add) <= set(((yaml.safe_load(nt).get("cockpit") or {}).get("tab_defs") or {})), \
                "cockpit tab_def merge failed to parse back"
            hschema.write_text(nt, encoding="utf-8")
        plan.step(f"merge {len(new_defs)} cockpit tab_def(s): {sorted(new_defs)}", _do_defs)

    missing = [n for n in add_names if n not in htabs]
    if missing:
        def _do_tabs(missing=missing):
            t = hschema.read_text(encoding="utf-8")
            m = re.search(r"^(  tabs:\s*)\[([^\]\n]*)\]\s*$", t, re.M)
            assert m is not None, "host cockpit `tabs:` is not a single-line flow list — merge by hand"
            cur = [x.strip() for x in m.group(2).split(",") if x.strip()]
            for n in missing:                              # add before `browse` so browse stays last
                if n not in cur:
                    cur.insert(cur.index("browse") if "browse" in cur else len(cur), n)
            hschema.write_text(t[:m.start()] + m.group(1) + "[" + ", ".join(cur) + "]" + t[m.end():],
                               encoding="utf-8")
        plan.step(f"add {len(missing)} tab(s) to the cockpit nav: {missing}", _do_tabs)


def install_taxonomy(host: Path, pack: Path, slug: str, plan: Plan) -> None:
    added = merge_types(host, pack, plan)
    merge_type_aliases(host, pack, plan)
    merge_namespaces(host, pack, plan)
    merge_rules(host, pack, added, plan)
    merge_feeds(host, pack, plan)
    merge_crons(host, pack, plan)
    merge_lane_scripts(host, pack, plan)
    merge_cockpit(host, pack, plan)
    append_persona(host, pack, slug, plan)


def _checklist(shape: str, slug: str) -> str:
    common = (
        "  # deploy the merged config (feeds/crons only take effect after):\n"
        "  HERMES_UID=<uid> CRON_PACK_DIR=<deployment> bash scripts/deploy-cron-scripts.sh\n"
        "  HERMES_UID=<uid> CRON_PACK_DIR=<deployment> bash scripts/deploy-cron-plus-jobs.sh\n"
        "  # then verify (the probes ARE the contract):\n")
    if shape == "subtree":
        return common + (
            f"  - an INVALID page under wiki/{slug}/ (missing a required field) must "
            "REJECT at the write path; a root-domain page is untouched\n"
            "  - the completeness audit evaluates the merged rules\n"
            f"  - wiki/INDEX.md lists {slug}/ after the next index rebuild")
    return common + (
        "  - a page of an ADDED type validates in the host's entities/\n"
        "  - a page of a REUSED type still validates against the HOST contract\n"
        "  - pack lanes appear pack-prefixed in cron-plus.sh list; the pack's raw/<stream> "
        "fills independently")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="framework install-domain",
        description="Install a pack alongside the host in a live deployment (dry-run default).")
    ap.add_argument("deployment", help="host deployment dir (the live pack instance)")
    ap.add_argument("pack", help="incoming pack dir")
    ap.add_argument("--under", help="subtree override, e.g. wiki/<slug>/ "
                                    "(default: the pack's domain slug)")
    ap.add_argument("--shape", choices=["subtree", "taxonomy"],
                    help="required only when the pack ships both co-install forms")
    ap.add_argument("--apply", action="store_true", help="write (default: print the plan)")
    ns = ap.parse_args(argv)

    host, pack = Path(ns.deployment).resolve(), Path(ns.pack).resolve()
    for p, what in ((host, "deployment"), (pack, "pack")):
        if not (p / "schema.yaml").is_file():
            print(f"ERROR: {what} {p} has no schema.yaml", file=sys.stderr)
            return 2
    shape = detect_shape(pack, ns.shape)
    if shape is None:
        print("ERROR: pack ships no matching co-install form (subdomain/schema.yaml or "
              "subdomain/host-schema-additions.yaml) — see docs/authoring-a-pack.md §8",
              file=sys.stderr)
        return 2
    if shape == "both":
        print("ERROR: pack ships BOTH co-install forms — pick one with --shape",
              file=sys.stderr)
        return 2
    slug = domain_slug(pack, ns.under)

    # collision gate first — a FAIL here is a real conflict, never force past it.
    # Gate on what actually LANDS: the additions file (taxonomy) or the subdomain
    # schema (subtree) — the pack's standalone schema stays in the pack repo.
    preflight = _load_mod("coinstall_preflight.py")
    landing = ("host-schema-additions.yaml" if shape == "taxonomy" else "schema.yaml")
    pf_args = [str(host), str(pack), "--additions", str(pack / "subdomain" / landing)]
    if shape == "subtree":
        pf_args.append("--subtree")
    print(f"— coinstall preflight ({shape} shape) —")
    if preflight.main(pf_args) != 0:
        print("install-domain: preflight FAIL — resolve the collisions first.",
              file=sys.stderr)
        return 1

    plan = Plan(ns.apply)
    if shape == "subtree":
        install_subtree(host, pack, slug, plan)
    else:
        install_taxonomy(host, pack, slug, plan)
    rc = plan.run()
    if rc == 0 and plan.steps and ns.apply and shape == "taxonomy":
        # The taxonomy merge edited the host root schema.yaml — but the RUNTIME write path PREFERS
        # <host>/.okengine/composed-schema.yaml (base ⊕ pack ⊕ extensions) whenever an enabled
        # extension made that artifact exist, and it still predates the merge. Left stale, every
        # okengine-write into the newly co-installed namespace/type is silently rejected ("namespace
        # not declared", okengine#115) until the next full deploy. Regenerate it now so the write path
        # sees the merge immediately; write_composed_schema self-heals the no-extension case too
        # (no fragments → it removes any stale artifact so schema.yaml governs).
        compose = _load_mod("extension_compose.py")
        errs = compose.write_composed_schema(host)
        if errs:
            print("ERROR: schema.yaml merged, but composed-schema.yaml regeneration FAILED — the "
                  "runtime write path will reject writes to the new namespace until the next deploy:",
                  file=sys.stderr)
            for e in errs:
                print(f"  - {e}", file=sys.stderr)
            return 1
        print("  regenerated .okengine/composed-schema.yaml — runtime write path now sees the merge")
    if rc == 0 and plan.steps:
        print("\nnext steps:\n" + _checklist(shape, slug))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
