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
re-run applies nothing. `--refresh` replaces changed runtime artifacts only when the pack's
ownership manifest proves they belong to that co-installed domain. Runs `coinstall_preflight`
first and refuses on FAIL.

The write-path probes (invalid page under the subtree must REJECT; host pages untouched)
need a LIVE gateway and are printed as the post-install checklist, not run here — along
with the deploy commands that make the cron/feed merges take effect.

Usage:
  framework install-domain <deployment-dir> <pack-dir>
      [--under wiki/<slug>] [--shape subtree|taxonomy] [--refresh] [--apply]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent

PERSONA_MARKER = "## Installed domain:"
MAX_OPML_BYTES = 10 * 1024 * 1024


def _safe_xml(data: bytes | str) -> ET.Element:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_OPML_BYTES:
        raise ET.ParseError("OPML exceeds 10 MiB safety limit")
    upper = raw[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ET.ParseError("DTD/entity declarations are not permitted")
    return ET.fromstring(raw)  # nosec B314


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
    if flow.endswith("\n..."):
        flow = flow[:-4]
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


def merge_coverage_fields(host: Path, pack: Path, plan: Plan) -> None:
    """Fold a guest's `coverage_fields` (the field-population ratios corpus_audit tracks, okengine
    #264) into the host schema so the type OWNER's coverage declarations TRAVEL into a composed /
    bundle vault. Without this a member (okpack-vuln: cve.cvss_base) declares coverage the composed
    schema silently drops — the metric only ever appears in the member's STANDALONE deploy, and a
    bundle like okpack-cti shows UNDETECTABLE despite owning the enriched type. Additive + deduped
    by (type, field); a host entry for the same pair wins."""
    additions = _yaml(pack / "subdomain" / "host-schema-additions.yaml").get("coverage_fields") or []
    additions = [a for a in additions if isinstance(a, dict) and a.get("type") and a.get("field")]
    if not additions:
        return
    hschema = host / "schema.yaml"
    have = {(str(e.get("type")), str(e.get("field")))
            for e in (_yaml(hschema).get("coverage_fields") or []) if isinstance(e, dict)}
    add = [a for a in additions if (str(a["type"]), str(a["field"])) not in have]
    if not add:
        return
    text = hschema.read_text(encoding="utf-8")
    # host declares coverage_fields INLINE (`coverage_fields: [...]`) — a list can't be block-inserted
    # into; leave it for a human rather than append a second key YAML would silently de-dup. (Packs
    # write block style, so this is belt-and-braces.)
    if re.search(r"^coverage_fields:[ \t]*\S", text, re.M) and \
            not re.search(r"^coverage_fields:[ \t]*(#.*)?$", text, re.M):
        plan.warn("host declares coverage_fields inline — skipping guest merge (add by hand)")
        return

    def _do(add=add):
        t = hschema.read_text(encoding="utf-8")

        def _entry(a):
            body = f"type: {a['type']}, field: {a['field']}"
            if a.get("min") is not None:
                body += f", min: {a['min']}"
            return f"  - {{{body}}}\n"
        block = ("  # --- co-installed coverage_fields (okpack: "
                 f"{pack_name(pack)}, framework install-domain) ---\n"
                 + "".join(_entry(a) for a in add))
        new_t = _insert_under(t, r"^coverage_fields:[ \t]*(#.*)?$", block)
        if new_t is None:                       # no coverage_fields yet — add a fresh top-level block
            new_t = t.rstrip("\n") + "\ncoverage_fields:\n" + block
        got = {(str(e.get("type")), str(e.get("field")))
               for e in (yaml.safe_load(new_t).get("coverage_fields") or []) if isinstance(e, dict)}
        assert {(str(a["type"]), str(a["field"])) for a in add} <= got, \
            "coverage_fields merge failed to parse back"
        hschema.write_text(new_t, encoding="utf-8")
    plan.step(f"merge {len(add)} coverage_field(s) into schema.yaml: "
              f"{[(a['type'], a['field']) for a in add]}", _do)


def merge_enums(host: Path, pack: Path, plan: Plan) -> None:
    """Fold a guest's `enums` (vocabularies) + `field_enums` (field->rule) from host-schema-additions
    into the host schema, so a type OWNER's vocabulary TRAVELS into a composed/bundle vault and
    corpus_audit's enum-drift metric can actually SEE the guest's fields. Without this a member
    (okpack-vuln: cve.severity, okpack-incidents: incident.incident_type, okpack-indicators:
    indicator.indicator_type) declares an enum the composed schema silently drops — the A-class vocab
    drift on those fields reads UNDETECTABLE in the bundle deploy though the drift is real
    (okengine#259 rec 12). Additive + deduped by key; a host entry for the same key WINS (never
    overwritten — the host's contract is authoritative). Mirrors merge_coverage_fields."""
    add = _yaml(pack / "subdomain" / "host-schema-additions.yaml")
    g_enums = add.get("enums") if isinstance(add.get("enums"), dict) else {}
    g_fe = add.get("field_enums") if isinstance(add.get("field_enums"), dict) else {}
    if not g_enums and not g_fe:
        return
    hschema = host / "schema.yaml"
    hy = _yaml(hschema)
    h_enums = hy.get("enums") if isinstance(hy.get("enums"), dict) else {}
    h_fe = hy.get("field_enums") if isinstance(hy.get("field_enums"), dict) else {}
    new_enums = {k: v for k, v in g_enums.items() if k not in h_enums}
    new_fe = {k: v for k, v in g_fe.items() if k not in h_fe}
    merged_fe = {}
    for key, guest in g_fe.items():
        current = h_fe.get(key)
        if key not in h_fe or not isinstance(current, dict) or not isinstance(guest, dict):
            continue
        current_by_type = current.get("by_type")
        guest_by_type = guest.get("by_type")
        if not isinstance(current_by_type, dict) or not isinstance(guest_by_type, dict):
            continue
        missing = {name: value for name, value in guest_by_type.items()
                   if name not in current_by_type}
        if missing:
            value = dict(current)
            value["by_type"] = {**current_by_type, **missing}
            merged_fe[key] = value
    if not new_enums and not new_fe and not merged_fe:
        return

    text = hschema.read_text(encoding="utf-8")
    # a host that declares either block INLINE (`enums: {...}`) can't be block-inserted into without
    # YAML silently de-duping a second same-named key — leave it for a human (mirror coverage_fields).
    for key in ("enums", "field_enums"):
        if re.search(rf"^{key}:[ \t]*\S", text, re.M) and \
                not re.search(rf"^{key}:[ \t]*(#.*)?$", text, re.M):
            plan.warn(f"host declares {key} inline — skipping guest merge (add by hand)")
            return

    def _render(d: dict) -> str:
        return "".join(
            f"  {k}: {yaml.safe_dump(v, default_flow_style=True, allow_unicode=True).strip()}\n"
            for k, v in d.items())

    def _do():
        t = hschema.read_text(encoding="utf-8")
        pn = pack_name(pack)
        for key, block_d in (("enums", new_enums), ("field_enums", new_fe)):
            if not block_d:
                continue
            block = (f"  # --- co-installed {key} (okpack: {pn}, framework install-domain) ---\n"
                     + _render(block_d))
            nt = _insert_under(t, rf"^{key}:[ \t]*(#.*)?$", block)
            if nt is None:                       # no such block yet — add a fresh top-level one
                nt = t.rstrip("\n") + f"\n{key}:\n" + block
            t = nt
        # Existing field contracts frequently use an inline `status: {by_type: ...}`. Replace
        # exactly that entry with a merged value so guest-owned type cases travel without dumping
        # or reformatting the operator's full schema. Existing host type cases always win.
        for field, value in merged_fe.items():
            section = re.search(r"^field_enums:[ \t]*(?:#.*)?$", t, re.M)
            assert section is not None, "field_enums section vanished during merge"
            start = re.search(rf"^  {re.escape(str(field))}:.*$", t[section.end():], re.M)
            assert start is not None, f"field_enums.{field} entry vanished during merge"
            absolute_start = section.end() + start.start()
            line_end = t.find("\n", absolute_start)
            line_end = len(t) if line_end < 0 else line_end + 1
            first_line = t[absolute_start:line_end]
            if first_line.rstrip().endswith(":"):
                rest = t[line_end:]
                next_entry = re.search(r"^(?:  \S|\S)", rest, re.M)
                absolute_end = line_end + (next_entry.start() if next_entry else len(rest))
            else:
                absolute_end = line_end
            t = t[:absolute_start] + _dump_entry(str(field), value, 2) + t[absolute_end:]
        parsed = yaml.safe_load(t)
        got_e = parsed.get("enums") or {}
        got_fe = parsed.get("field_enums") or {}
        assert set(new_enums) <= set(got_e) and set(new_fe) <= set(got_fe), \
            "enums merge failed to parse back"
        for field, value in merged_fe.items():
            assert got_fe.get(field) == value, f"field_enums.{field} nested merge failed"
        hschema.write_text(t, encoding="utf-8")
    plan.step(f"merge {len(new_enums)} enum(s) + {len(new_fe)} field_enum(s) + "
              f"{len(merged_fe)} by-type extension(s) into schema.yaml: "
              f"{sorted(list(new_enums) + list(new_fe) + list(merged_fe))}", _do)


def merge_field_contracts(host: Path, pack: Path, plan: Plan) -> None:
    """Carry additive field-shape and list-item contracts into a taxonomy host.

    Type required fields alone are not sufficient: dropping `facets: list` or its item contract
    silently weakens the same pack when co-installed. Host values win on an existing key; new keys
    are inserted surgically under the existing top-level mapping.
    """
    additions = _yaml(pack / "subdomain" / "host-schema-additions.yaml")
    hschema = host / "schema.yaml"
    # Compare against the effective schema when extensions are enabled. A common contract may be
    # extension-owned even though it is absent from schema.yaml; inserting it into the root would
    # make the next extension composition fail as a duplicate declaration.
    composed = host / ".okengine" / "composed-schema.yaml"
    host_schema = _yaml(composed) if composed.is_file() else _yaml(hschema)
    pending = {}
    for section in ("field_shapes", "field_items"):
        guest = additions.get(section) if isinstance(additions.get(section), dict) else {}
        current = host_schema.get(section) if isinstance(host_schema.get(section), dict) else {}
        pending[section] = {key: value for key, value in guest.items() if key not in current}
        for key in set(guest) & set(current):
            if guest[key] != current[key]:
                plan.warn(f"{section}.{key} already differs in host — host wins; verify compatibility")
    if not any(pending.values()):
        return

    def _do():
        text = hschema.read_text(encoding="utf-8")
        for section, values in pending.items():
            if not values:
                continue
            block = (f"  # --- co-installed {section} (okpack: {pack_name(pack)}, "
                     "framework install-domain) ---\n" +
                     "".join(_dump_entry(str(key), value, 2) for key, value in sorted(values.items())))
            updated = _insert_under(text, rf"^{section}:[ \t]*(#.*)?$", block)
            if updated is None:
                updated = text.rstrip("\n") + f"\n{section}:\n" + block
            text = updated
        parsed = yaml.safe_load(text)
        for section, values in pending.items():
            assert set(values) <= set(parsed.get(section) or {}), f"{section} merge failed"
        hschema.write_text(text, encoding="utf-8")

    count = sum(len(values) for values in pending.values())
    plan.step(f"merge {count} field shape/item contract(s)", _do)


def merge_list_contracts(host: Path, pack: Path, plan: Plan) -> None:
    """Union additive top-level enforcement lists without reformatting the host schema."""
    additions = _yaml(pack / "subdomain" / "host-schema-additions.yaml")
    hschema = host / "schema.yaml"
    current = _yaml(hschema)
    pending: dict[str, list] = {}
    for key in ("protected_fields", "depth_critical_types"):
        guest = additions.get(key) if isinstance(additions.get(key), list) else []
        host_values = current.get(key) if isinstance(current.get(key), list) else []
        merged = [*host_values, *(value for value in guest if value not in host_values)]
        if merged != host_values:
            pending[key] = merged
    if not pending:
        return

    def _do():
        text = hschema.read_text(encoding="utf-8")
        for key, values in pending.items():
            document = yaml.compose(text)
            value_node = None
            if isinstance(document, yaml.MappingNode):
                for key_node, candidate in document.value:
                    if key_node.value == key:
                        value_node = candidate
                        break
            rendered = yaml.safe_dump(values, default_flow_style=True, sort_keys=False,
                                      width=10**6).strip()
            if rendered.endswith("\n..."):
                rendered = rendered[:-4]
            if value_node is None:
                text = text.rstrip("\n") + f"\n{key}: {rendered}\n"
            else:
                text = text[:value_node.start_mark.index] + rendered + text[value_node.end_mark.index:]
        parsed = yaml.safe_load(text)
        for key, values in pending.items():
            assert parsed.get(key) == values, f"{key} merge failed"
        hschema.write_text(text, encoding="utf-8")

    plan.step(f"merge {len(pending)} additive enforcement list contract(s): {sorted(pending)}", _do)


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
        sroot, droot = _safe_xml(src.read_bytes()), _safe_xml(dst.read_bytes())
    except (OSError, ET.ParseError) as e:
        plan.fail(f"feeds OPML unparseable: {e}")
        return
    have = {o.get("xmlUrl") for o in droot.iter("outline") if o.get("xmlUrl")}
    new = [o for o in sroot.iter("outline")
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
        got = {o.get("xmlUrl") for o in _safe_xml(new_t).iter("outline")}
        assert {o.get("xmlUrl") for o in new} <= got, "feed merge failed to parse back"
        dst.write_text(new_t, encoding="utf-8")
    plan.step(f"merge {len(new)} feed(s) into feeds/feeds.opml (deduped by xmlUrl)", _do)


def merge_crons(host: Path, pack: Path, plan: Plan, *, refresh: bool = False,
                owned: set[str] | None = None) -> None:
    pname = pack_name(pack)
    src = pack / "crons" / "domain-crons.json"
    if src.is_file():
        dst = host / "crons" / "domain-crons.json"
        cur = json.loads(dst.read_text(encoding="utf-8")) if dst.is_file() else []
        cur_jobs = cur["jobs"] if isinstance(cur, dict) else cur
        by_name = {j.get("name"): (i, j) for i, j in enumerate(cur_jobs)}
        owned = owned or set()
        add, update, stale, unprefixed = [], [], [], []
        for j in json.loads(src.read_text(encoding="utf-8")):
            if not str(j.get("name", "")).startswith(f"{pname}-"):
                unprefixed.append(j.get("name"))
                continue
            name = j.get("name")
            if name not in by_name:
                add.append(j)
            elif ({k: v for k, v in by_name[name][1].items() if k != "enabled"} !=
                  {k: v for k, v in j.items() if k != "enabled"}):
                if refresh and name in owned:
                    update.append((by_name[name][0], j))
                else:
                    stale.append(name)
        if unprefixed:
            plan.warn(f"cron job(s) skipped — co-installed domain jobs must be "
                      f"'{pname}-' prefixed: {unprefixed}")
        if add:
            def _do(add=add, cur=cur, cur_jobs=cur_jobs, dst=dst):
                cur_jobs.extend(add)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
            plan.step(f"append {len(add)} domain cron job(s): {[j['name'] for j in add]}", _do)
        if stale:
            plan.fail(f"installed cron job(s) differ from {pname}: {stale} — "
                      "run install-domain --refresh --apply to accept the pack update")
        if update:
            def _update(update=update, cur=cur, cur_jobs=cur_jobs, dst=dst):
                for index, job in update:
                    merged = dict(job)
                    # Preserve the deployment operator's explicit on/off decision. Pack updates
                    # own the job contract, but never get to silently activate a disabled lane.
                    if "enabled" in cur_jobs[index]:
                        merged["enabled"] = cur_jobs[index]["enabled"]
                    cur_jobs[index] = merged
                dst.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
            plan.step(f"refresh {len(update)} owned domain cron job(s): "
                      f"{[j['name'] for _, j in update]}", _update)
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


def merge_lane_scripts(host: Path, pack: Path, plan: Plan, *, refresh: bool = False,
                       owned: set[str] | None = None, shared: set[str] | None = None,
                       scope: set[str] | None = None) -> None:
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
    owned = owned or set()
    shared = shared or set()
    copy, replace, preserve, clash = [], [], [], []
    for f in sorted(src.glob("*.py")):  # glob-ok: pack crons/scripts/ is a flat dir, not a sharded content namespace
        if scope is not None and f.name not in scope:
            continue
        d = dst / f.name
        if not d.is_file():
            copy.append(f.name)
        elif d.read_bytes() != f.read_bytes():
            if refresh and f.name in owned:
                replace.append(f.name)
            elif refresh and f.name in shared:
                preserve.append(f.name)
            else:
                clash.append(f.name)
    if preserve:
        plan.warn(f"shared support script(s) differ and remain host-controlled: {preserve}")
    for n in clash:
        plan.fail(f"lane script crons/scripts/{n} differs from the installed pack; "
                  "use --refresh only for a pack that owns this artifact, or rename a true collision")
    if copy or replace:
        def _do(copy=copy, replace=replace):
            dst.mkdir(parents=True, exist_ok=True)
            for n in copy + replace:
                (dst / n).write_bytes((src / n).read_bytes())
        desc = []
        if copy:
            desc.append(f"copy {len(copy)} new: {copy}")
        if replace:
            desc.append(f"refresh {len(replace)} owned: {replace}")
        plan.step("; ".join(desc) + " in crons/scripts/", _do)


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


def _mapping_value(node, key: str):
    """Return a composed-YAML mapping value without round-tripping the document."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, value in node.value:
        if k.value == key:
            return value
    return None


def _replace_cockpit_boxes(text: str, tab: str, boxes: list[dict]) -> str:
    """Replace one tab's boxes node while leaving the rest of the commented schema intact."""
    root = yaml.compose(text)
    cockpit = _mapping_value(root, "cockpit")
    tab_defs = _mapping_value(cockpit, "tab_defs")
    tab_node = _mapping_value(tab_defs, tab)
    boxes_node = _mapping_value(tab_node, "boxes")
    assert boxes_node is not None, f"cockpit tab {tab!r} has no boxes list"
    replacement = yaml.safe_dump(boxes, default_flow_style=True, sort_keys=False,
                                 width=10**6).strip()
    if replacement.endswith("\n..."):
        replacement = replacement[:-4]
    # A block sequence's end mark is the first character of the following mapping key. Replacing
    # it with a one-line flow sequence therefore has to restore the line boundary. A flow sequence
    # embedded in an inline mapping must not gain one (that would split `{..., boxes: []}`).
    if not boxes_node.flow_style:
        replacement += "\n"
    return text[:boxes_node.start_mark.index] + replacement + text[boxes_node.end_mark.index:]


def merge_cockpit(host: Path, pack: Path, plan: Plan, *, refresh: bool = False) -> None:
    """Taxonomy shape: fold a guest's cockpit `tab_defs` + `tabs` (from host-schema-additions.yaml)
    into the host's `cockpit:` block, so a composed vault surfaces the guest domain's tab (a
    Vulnerabilities pack contributes its Vulnerabilities tab). Host wins on a tab it already declares.
    Presentation config, not the enforced write schema — but without this merge a guest tab had NO
    canonical home: it was only ever hand-added to the composed vault, so a recompose dropped it."""
    add_ck = _yaml(pack / "subdomain" / "host-schema-additions.yaml").get("cockpit") or {}
    add_defs = add_ck.get("tab_defs") if isinstance(add_ck.get("tab_defs"), dict) else {}
    add_names = [str(t).strip() for t in (add_ck.get("tabs") or []) if str(t).strip()]
    contributions = (add_ck.get("tab_contributions")
                     if isinstance(add_ck.get("tab_contributions"), dict) else {})
    aliases = add_ck.get("tab_aliases") if isinstance(add_ck.get("tab_aliases"), dict) else {}
    if not add_defs and not add_names and not contributions and not aliases:
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
            if nt is None:
                # A bare/minimal host is legitimate (a fresh `framework init` scaffold ships no
                # cockpit config at all) — SEED the block instead of failing the whole install.
                # This crashed every coinstall of a cockpit-bearing guest (vuln/indicators/
                # detections/incidents) onto a scaffold host in the deploy-matrix gate.
                if re.search(r"^cockpit:\s*(#.*)?$", t, re.M):
                    # cockpit block exists but has no tab_defs: — open one at the top of the block
                    nt = _insert_under(t, r"^cockpit:\s*(#.*)?$", "  tab_defs:\n" + block)
                else:
                    # no cockpit at all — append a minimal block; tabs seeds [browse] so the
                    # _do_tabs step below can slot the guest tabs before it.
                    seed = (f"# --- co-installed cockpit (okpack: {pack_name(pack)}, install-domain) ---\n"
                            "cockpit:\n  tabs: [browse]\n  tab_defs:\n" + block)
                    nt = t + ("" if t.endswith("\n") else "\n") + seed
            assert nt is not None, "host cockpit block is malformed — merge the guest tab_defs by hand"
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

    # A guest may extend a host-owned workspace without creating another top-level tab. Stable,
    # pack-prefixed contribution IDs and box IDs make ownership explicit and refresh deterministic.
    pname = pack_name(pack)
    prospective_defs = set(htabdefs) | set(add_defs)
    desired_by_target: dict[str, list[dict]] = {}
    contribution_ids = set()
    for cid, declaration in contributions.items():
        cid = str(cid).strip()
        if not cid.startswith(pname + "."):
            plan.fail(f"cockpit contribution {cid!r} must be prefixed with {pname!r}")
            continue
        if not isinstance(declaration, dict):
            plan.fail(f"cockpit contribution {cid!r} must be a mapping")
            continue
        target = str(declaration.get("target") or "").strip()
        boxes = declaration.get("boxes")
        if target not in prospective_defs:
            plan.fail(f"cockpit contribution {cid!r} targets missing tab {target!r}")
            continue
        if not isinstance(boxes, list) or not boxes:
            plan.fail(f"cockpit contribution {cid!r} must declare at least one box")
            continue
        normalized = []
        for box in boxes:
            bid = str(box.get("id") or "").strip() if isinstance(box, dict) else ""
            if not bid or not bid.startswith(pname + "."):
                plan.fail(f"every box in cockpit contribution {cid!r} needs a stable, "
                          f"{pname!r}-prefixed id")
                continue
            normalized.append({**box, "id": bid, "contribution": cid})
        if len(normalized) == len(boxes):
            desired_by_target.setdefault(target, []).extend(normalized)
            contribution_ids.add(cid)

    if desired_by_target:
        def _do_contributions():
            path = host / "schema.yaml"
            text = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text) or {}
            defs = ((parsed.get("cockpit") or {}).get("tab_defs") or {})
            for target, desired in desired_by_target.items():
                current = list((defs.get(target) or {}).get("boxes") or [])
                owned = [b for b in current if b.get("contribution") in contribution_ids]
                foreign = [b for b in current if b.get("contribution") not in contribution_ids]
                foreign_ids = {str(b.get("id")) for b in foreign if b.get("id")}
                collisions = sorted(foreign_ids & {str(b["id"]) for b in desired})
                assert not collisions, f"cockpit box id collision on {target}: {collisions}"
                if owned == desired:
                    continue
                assert not owned or refresh, (f"cockpit contribution on {target!r} changed; "
                                              "rerun install-domain with --refresh")
                merged = foreign + desired
                text = _replace_cockpit_boxes(text, target, merged)
                reparsed = yaml.safe_load(text) or {}
                actual = ((((reparsed.get("cockpit") or {}).get("tab_defs") or {})
                           .get(target) or {}).get("boxes") or [])
                assert actual == merged, (f"cockpit contribution merge for {target!r} did not "
                                          "parse back exactly; schema was not written")
                defs[target]["boxes"] = merged
            path.write_text(text, encoding="utf-8")
        plan.step(f"merge cockpit contribution(s) into {sorted(desired_by_target)}",
                  _do_contributions)

    if aliases:
        invalid = {str(k): str(v) for k, v in aliases.items()
                   if str(v) not in prospective_defs}
        for alias, target in invalid.items():
            plan.fail(f"cockpit tab alias {alias!r} targets missing tab {target!r}")
        valid = {str(k).strip(): str(v).strip() for k, v in aliases.items()
                 if str(v) in prospective_defs}
        existing_aliases = hcock.get("tab_aliases") or {}
        conflicts = {k: (existing_aliases[k], v) for k, v in valid.items()
                     if k in existing_aliases and existing_aliases[k] != v}
        for alias, values in conflicts.items():
            plan.fail(f"cockpit tab alias {alias!r} conflicts: {values[0]!r} vs {values[1]!r}")
        new_aliases = {k: v for k, v in valid.items() if k not in existing_aliases}
        if new_aliases:
            def _do_aliases():
                text = hschema.read_text(encoding="utf-8")
                block = "".join(_dump_entry(k, v, 4) for k, v in new_aliases.items())
                if re.search(r"^  tab_aliases:\s*$", text, re.M):
                    updated = _insert_under(text, r"^  tab_aliases:\s*$", block)
                else:
                    updated = _insert_under(text, r"^cockpit:\s*(#.*)?$", "  tab_aliases:\n" + block)
                assert updated is not None
                hschema.write_text(updated, encoding="utf-8")
            plan.step(f"merge cockpit tab alias(es): {sorted(new_aliases)}", _do_aliases)


def install_taxonomy(host: Path, pack: Path, slug: str, plan: Plan, *, refresh: bool = False,
                     manifest: dict | None = None, source_manifest: dict | None = None) -> None:
    manifest = manifest or {}
    source_manifest = source_manifest or {}
    if refresh:
        # Refresh is deliberately a runtime-only corridor. Schema, cockpit, feeds, rules, and
        # persona are additive composition contracts and need a fresh bundle composition—not a
        # cron update that might partially rewrite the write boundary.
        merge_crons(host, pack, plan, refresh=True,
                    owned=set((manifest.get("cron_jobs") or {}).keys()))
        merge_lane_scripts(host, pack, plan, refresh=True,
                           owned=set((manifest.get("lane_scripts") or {}).keys()),
                           shared=set((source_manifest.get("shared_support_scripts") or {}).keys()),
                           scope=(set((source_manifest.get("lane_scripts") or {}).keys()) |
                                  set((source_manifest.get("shared_support_scripts") or {}).keys())))
        merge_cockpit(host, pack, plan, refresh=True)
        return
    added = merge_types(host, pack, plan)
    merge_type_aliases(host, pack, plan)
    merge_coverage_fields(host, pack, plan)
    merge_enums(host, pack, plan)
    merge_field_contracts(host, pack, plan)
    merge_list_contracts(host, pack, plan)
    merge_namespaces(host, pack, plan)
    merge_rules(host, pack, added, plan)
    merge_feeds(host, pack, plan)
    merge_crons(host, pack, plan, refresh=refresh,
                owned=set((manifest.get("cron_jobs") or {}).keys()))
    merge_lane_scripts(host, pack, plan, refresh=refresh,
                       owned=set((manifest.get("lane_scripts") or {}).keys()),
                       shared=set((source_manifest.get("shared_support_scripts") or {}).keys()),
                       scope=(set((source_manifest.get("lane_scripts") or {}).keys()) |
                              set((source_manifest.get("shared_support_scripts") or {}).keys())))
    merge_cockpit(host, pack, plan, refresh=refresh)
    if not refresh:
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


def _legacy_runtime_ownership(host: Path, pack: Path) -> dict:
    """Conservatively adopt a pre-manifest install for an explicit first --refresh.

    A matching pack-prefixed job name proves the domain was previously installed. Only scripts
    directly referenced by those jobs are adopted; an unrelated same-named helper remains a hard
    collision instead of being overwritten merely because --refresh was supplied.
    """
    pname = pack_name(pack)
    try:
        incoming_raw = json.loads((pack / "crons" / "domain-crons.json").read_text())
        incoming = incoming_raw.get("jobs", []) if isinstance(incoming_raw, dict) else incoming_raw
    except (OSError, json.JSONDecodeError):
        incoming = []
    try:
        host_raw = json.loads((host / "crons" / "domain-crons.json").read_text())
        host_jobs = host_raw.get("jobs", []) if isinstance(host_raw, dict) else host_raw
    except (OSError, json.JSONDecodeError):
        host_jobs = []
    present = {str(row.get("name")) for row in host_jobs if isinstance(row, dict)}
    owned_jobs, owned_scripts = {}, {}
    for row in incoming:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if name in present and name.startswith(f"{pname}-"):
            owned_jobs[name] = "legacy-adopted"
            if row.get("script"):
                owned_scripts[Path(str(row["script"])).name] = "legacy-adopted"
    source = _load_mod("composed_pack_state.py").source_manifest(pack, detect_shape(pack, None) or "taxonomy")
    return {"cron_jobs": owned_jobs, "lane_scripts": owned_scripts,
            "shared_support_scripts": source.get("shared_support_scripts") or {}}


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
    ap.add_argument("--refresh", action="store_true",
                    help="replace changed cron jobs/scripts owned by this installed pack")
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

    state = _load_mod("composed_pack_state.py")
    pname = pack_name(pack)
    prior_manifest = state.load(host, pname)
    incoming_manifest = state.source_manifest(pack, shape)
    ownership = prior_manifest or (_legacy_runtime_ownership(host, pack) if ns.refresh else {})
    plan = Plan(ns.apply)
    if shape == "subtree":
        install_subtree(host, pack, slug, plan)
    else:
        install_taxonomy(host, pack, slug, plan, refresh=ns.refresh, manifest=ownership,
                         source_manifest=incoming_manifest)
    # A pack-version migration is one logical update with the install-domain edits planned above.
    # Snapshot BEFORE those edits, otherwise the migration runner's own pre-migration snapshot can
    # only roll back to an already-partially-updated deployment.
    fu = _load_mod("framework_upgrade.py")
    incoming_ver = str(incoming_manifest.get("pack_version") or "") or None
    installed_ver = (fu.installed_pack_version(
        host, pname, fallback=str(prior_manifest.get("pack_version") or "") or None)
        if prior_manifest else None)
    update_snapshot = None
    if ns.apply and prior_manifest and incoming_ver and installed_ver != incoming_ver:
        snap_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-install-domain"
        update_snapshot = fu.snapshot(
            host, snap_id, {"operation": "install-domain", "pack": pname,
                            "from": installed_ver, "to": incoming_ver})
        print(f"  transaction snapshot: .okengine/snapshots/{snap_id} (before domain update)")
    rc = plan.run()
    if rc == 0 and ns.apply:
        manifest = incoming_manifest
        # Never bless a new source snapshot unless its changed owned runtime artifacts were
        # actually refreshed. A normal re-run may diagnose staleness but must preserve the last
        # known-good hashes so validation continues to detect it.
        runtime_changed = prior_manifest and any(
            manifest.get(key) != prior_manifest.get(key) for key in ("lane_scripts", "cron_jobs"))
        if not runtime_changed or ns.refresh or not prior_manifest:
            if state.write(host, manifest):
                print(f"  recorded ownership manifest: {state.manifest_path(host, pname)}")
    if rc == 0 and plan.steps and ns.apply and shape == "taxonomy":
        # The taxonomy merge edited the host root schema.yaml — but the RUNTIME write path PREFERS
        # <host>/.okengine/composed-schema.yaml (base ⊕ pack ⊕ extensions) whenever an enabled
        # extension made that artifact exist, and it still predates the merge. Left stale, every
        # okengine-write into the newly co-installed namespace/type is silently rejected ("namespace
        # not declared", okengine#115) until the next full deploy. Regenerate it now so the write path
        # sees the merge immediately. This also applies to --refresh: Cockpit reads the effective
        # artifact, so leaving it stale would hide refreshed tab contributions/aliases even though
        # root schema.yaml is correct. write_composed_schema self-heals the no-extension case too
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
    # okengine#312: re-installing an EXISTING member is a pack update — run the guest's
    # pack-version migrations (pack/migrations/m_*.py, span (installed, incoming]) against the
    # composed host vault through the engine's upgrade runner: dry-run preview in plan mode,
    # snapshot + roll-forward gate + auto-rollback under --apply. A NEW member just gets its
    # version baselined so a future refresh can compute the span.
    if rc == 0:
        if prior_manifest:
            try:
                changelog = (pack / "CHANGELOG.md").read_text(encoding="utf-8")
            except OSError:
                changelog = None
            if fu.run_pack_migrations(host, pname, installed_ver, incoming_ver,
                                      apply=ns.apply, migrations_dir=pack / "migrations",
                                      changelog_text=changelog, record=ns.apply,
                                      apply_hint="--apply") != 0:
                if update_snapshot is not None:
                    added = fu.added_since_snapshot(host, update_snapshot)
                    modified = fu.changed_since_snapshot(host, update_snapshot)
                    n = fu.restore(host, update_snapshot, added=added, modified=modified)
                    shutil.rmtree(update_snapshot, ignore_errors=True)
                    print(f"  ↩ domain update transaction ROLLED BACK ({n} files restored)")
                return 1
        elif ns.apply and incoming_ver and incoming_ver != "0.0.0":
            fu.record_pack_version(host, pname, incoming_ver)
    if update_snapshot is not None:
        shutil.rmtree(update_snapshot, ignore_errors=True)
    if rc == 0 and plan.steps:
        print("\nnext steps:\n" + _checklist(shape, slug))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
