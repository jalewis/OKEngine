#!/usr/bin/env python3
"""framework validate — pre-deploy sanity check for an OKF domain pack.

Catches the deploy-breaking mistakes before they hit production: a schema.yaml
that won't parse, a cron JSON with a bad shape, a cron script with a syntax
error, an unfilled persona, feeds that don't resolve, or a committed `.env`.
Domain-agnostic — validates the pack *spec* (docs/deploy-a-new-domain.md §1),
not any domain's content.

Usage:
  scripts/framework_validate.py <pack-dir> [--probe-feeds] [--quiet]

Exit: 0 = no FAILs (WARNs allowed) · 1 = at least one FAIL · 2 = bad invocation.

Severity is strict about real requirements:
  FAIL = a required file/config/variable is missing or wrong, so the deploy will
         error, a cron/lane won't run, or the pack ships incomplete (unrendered
         {{tokens}}, a missing/unpinned engine.version, a README that is missing,
         a stub, or has no Deploy section, a missing LICENSE, a cron with no
         usable schedule or no action, an empty engine-template prompt, an invalid
         pack.yaml enum, a gateway compose that never passes .env to the runtime,
         malformed schema/JSON, a committed .env, …).
  WARN = valid and deployable but worth fixing — inert-scaffold defaults (empty
         example feeds, unfilled persona placeholders), optional/engine-supplied
         fields, or a cross-pack type reference single-pack validate can't resolve.
  OK/INFO = fine.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

_VER_RE = re.compile(r"\bv\d+\.\d+\.\d+\b")
# Unrendered scaffold placeholder, e.g. {{PACK}} / {{DOMAIN}}. Matches only
# {{UPPER_SNAKE}} (mirrors framework_init) so a Python f-string's {{...}} or a
# lowercase brace pair never trips it.
_TOKEN_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
# Declarative pack files where a surviving token = a broken deploy. (Cron *.py
# scripts are excluded — they're compile-checked, and f-strings use {{ }}.)
_TOKEN_SCAN = ("schema.yaml", "CLAUDE.md", "pack.yaml", "engine.version",
               "README.md", ".env.example", "docker-compose.yml",
               ".okengine/application.yaml",
               "crons/domain-crons.json", "crons/engine-template-prompts.json",
               ".hermes-data/config.yaml")
# scaffold placeholders that mean a field is still unfilled
_PLACEHOLDERS = ("<One line:", "<who reads", "<Steps the ingest", "<Schema +",
                 "<Entity types", "Replace the placeholders")


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []   # (severity, check, detail)

    def add(self, sev: str, check: str, detail: str = ""):
        self.rows.append((sev, check, detail))

    def ok(self, c, d=""): self.add("OK", c, d)
    def info(self, c, d=""): self.add("INFO", c, d)
    def warn(self, c, d=""): self.add("WARN", c, d)
    def fail(self, c, d=""): self.add("FAIL", c, d)

    @property
    def n_fail(self): return sum(1 for s, _, _ in self.rows if s == "FAIL")
    @property
    def n_warn(self): return sum(1 for s, _, _ in self.rows if s == "WARN")


def _load_yaml(p: Path):
    """Parse a YAML file; return None on a parse error so a broken pack file fails/skips gracefully
    instead of crashing the whole validator (callers report the FAIL or `or {}` past it)."""
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def check_schema(pack: Path, r: Report) -> None:
    sp = pack / "schema.yaml"
    if not sp.is_file():
        r.fail("schema.yaml", "missing — the pack's contract is required")
        return
    if yaml is None:
        r.warn("schema.yaml", "PyYAML unavailable; skipped parse")
        return
    sch = _load_yaml(sp)
    if sch is None:
        r.fail("schema.yaml parses", "empty or unparseable YAML")
        return
    if not isinstance(sch, dict):
        r.fail("schema.yaml shape", "top level is not a mapping")
        return
    r.ok("schema.yaml parses")
    okf = sch.get("okf") or {}
    req = okf.get("required") if isinstance(okf, dict) else None
    if not (isinstance(req, list) and "type" in req):
        r.warn("schema.okf.required", "should be a list containing `type` (OKF v0.1 base)")
    else:
        r.ok("schema.okf.required", f"{req}")
    types = sch.get("types")
    if types in (None, {}):
        # A pack may declare ZERO types and inherit the whole engine-owned core (okengine#90):
        # the merged schema still carries source/concept/prediction/… — so this is valid, just a
        # minimal pack (the scaffold's starting state). Domain types get added on top.
        r.ok("schema.types", "none declared — inherits the engine core (okengine#90)")
    elif not isinstance(types, dict):
        r.fail("schema.types", "must be a mapping of page types")
    else:
        bad = [t for t, d in types.items()
               if not isinstance(d, dict) or "type" not in (d.get("required") or ["type"])]
        if bad:
            r.warn("schema.types[].required", f"types whose required list omits `type`: {bad}")
        else:
            r.ok("schema.types", f"{len(types)} types")
    for block in ("partitioning", "hot_set"):
        (r.ok if isinstance(sch.get(block), dict) else r.warn)(
            f"schema.{block}", "" if isinstance(sch.get(block), dict) else "absent (engine defaults apply)")
    # Each partitioned namespace's `strategy` must be one okf_migrate KNOWS — a typo (`by_date`) or
    # invented value silently degraded to flat in _new_key while every 'is this partitioned?' matcher
    # tests `strategy != "flat"` and treated it as partitioned, forking canonicals (invariant-audit
    # #25). Gate it at validate — the earliest gate — so it never reaches the drain.
    _VALID_STRATEGIES = {"flat", "by-letter", "by-date", "by-type"}
    part_ns = ((sch.get("partitioning") or {}).get("namespaces") or {}) if isinstance(sch, dict) else {}
    if isinstance(part_ns, dict):
        for ns, cfg in part_ns.items():
            strat = (cfg or {}).get("strategy", "flat") if isinstance(cfg, dict) else "flat"
            if strat not in _VALID_STRATEGIES:
                r.fail(f"partitioning.{ns}.strategy",
                       f"unknown strategy {strat!r} — valid: {sorted(_VALID_STRATEGIES)} "
                       f"(an unknown value silently degrades to flat while drains treat it as partitioned)")
    for block in ("permissions", "review", "tier"):
        if isinstance(sch.get(block), dict):
            r.info(f"schema.{block}", "declared (G2/G3/G4 policy)")
    if "strict_types" in sch and not isinstance(sch.get("strict_types"), bool):
        r.fail("schema.strict_types", "must be a boolean (true closes the composed type taxonomy)")
    local_type_names = set(types) if isinstance(types, dict) else set()
    # Pack schemas are additive fragments over the engine-owned core. Taxonomy
    # inputs may target core types without illegally redeclaring those types.
    base = _load_yaml(Path(__file__).resolve().parents[1] / "config" / "base-schema.yaml") or {}
    base_types = base.get("types") if isinstance(base, dict) else {}
    effective_type_names = local_type_names | (set(base_types) if isinstance(base_types, dict) else set())
    _check_engine_inputs(sch, effective_type_names, r)


def _check_engine_inputs(sch: dict, type_names: set, r: Report) -> None:
    """The OPTIONAL engine cron inputs (type_aliases, classify_hints,
    operational_types, classify_catchall, depth_critical_types, protected_fields).
    Absent ⇒ generic engine behaviour. When present, shape-check them and warn on
    references to undeclared types."""
    # dict-shaped: {alias|type: ...}
    aliases = sch.get("type_aliases")
    if aliases is not None:
        if not isinstance(aliases, dict):
            r.fail("schema.type_aliases", "must be a mapping {alias: canonical-type}")
        else:
            unknown = sorted({str(v) for v in aliases.values()} - type_names) if type_names else []
            shadow = sorted({str(k) for k in aliases} & type_names)
            if unknown:
                r.warn("schema.type_aliases", f"alias target(s) not in `types:` {unknown}")
            if shadow:
                # FAIL, not WARN — same severity as coinstall_preflight and the
                # deployment-validate lane. An alias key that IS a declared type makes
                # the normalization drains silently retype canonical pages; a pack-side
                # WARN here let exactly that reach a live deployment (the digest-alias
                # class caught on the v0.9.0 readiness sweep).
                r.fail("schema.type_aliases", f"alias key(s) SHADOW declared types "
                                              f"(drains would silently retype pages — retire the "
                                              f"alias or the type): {shadow}")
            if not unknown and not shadow:
                r.info("schema.type_aliases", f"{len(aliases)} alias(es)")
    hints = sch.get("classify_hints")
    if hints is not None:
        if not isinstance(hints, dict):
            r.fail("schema.classify_hints", "must be a mapping {canonical-type: [tags]}")
        else:
            unknown = sorted({str(k) for k in hints} - type_names) if type_names else []
            (r.warn if unknown else r.info)(
                "schema.classify_hints",
                f"key(s) not in `types:` {unknown}" if unknown else f"{len(hints)} hinted type(s)")
    # list-shaped; the type-referencing ones warn on undeclared entries
    for key, refs_types in (("operational_types", True), ("classify_catchall", True),
                            ("depth_critical_types", True), ("protected_fields", False)):
        val = sch.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            r.fail(f"schema.{key}", "must be a list")
            continue
        if refs_types and type_names:
            unknown = sorted({str(x) for x in val} - type_names)
            (r.warn if unknown else r.info)(
                f"schema.{key}", f"entr(ies) not in `types:` {unknown}" if unknown else f"{len(val)} entr(ies)")
        else:
            r.info(f"schema.{key}", f"{len(val)} entr(ies)")


def check_compose_drift(pack: Path, r: Report) -> None:
    """#169 class 2: pack composes are hand-copied skeleton snapshots and drift (one live
    pack shipped its reader with ZERO auth env). Checks fail-safe env plumbing and that
    pack.yaml's port_offset agrees with the bound ports."""
    cf = pack / "docker-compose.yml"
    if not cf.is_file():
        return
    try:
        comp = yaml.safe_load(cf.read_text()) or {}
    except Exception as e:
        r.fail("compose drift", f"docker-compose.yml unparseable ({e})")
        return
    services = comp.get("services") or {}
    for svc in ("okengine-reader", "okengine-cockpit"):
        s = services.get(svc)
        if not s:
            continue
        env = " ".join(map(str, s.get("environment") or []))
        for var in ("OKENGINE_TRUST", "OKENGINE_BIND", "OKENGINE_READER_PASSWORD"):
            if var not in env:
                r.fail("compose drift", f"{svc} missing {var} plumbing — the auth/trust "
                                        "fail-safe is skeleton-standard (okengine#90 P4a)")
    meta = _load_yaml(pack / "pack.yaml") or {}
    if meta.get("port_offset"):
        import re as _re
        # Scan only ACTUAL bindings: drop full-line comments first, so a commented-out example
        # (e.g. a doc line showing the un-offset mcp port `# ports: [...:8730:8730]`) is not
        # mistaken for a live binding and does not trip the drift check.
        raw = "\n".join(ln for ln in cf.read_text().splitlines() if not ln.lstrip().startswith("#"))
        base_hits = sorted({p for p in _re.findall(r":(\d{4,5}):\d+", raw)
                            if p in ("9200", "9201", "8730")})
        if base_hits:
            r.fail("compose drift", f"pack.yaml declares port_offset {meta['port_offset']} but "
                                    f"compose binds un-offset base port(s) {base_hits}")


def check_prompt_residue(pack: Path, r: Report) -> None:
    """#169 class 4 (structural form): prompts referencing `type:` tokens or [[ns/ link
    prefixes the pack's schema doesn't declare — sibling-domain residue from cloned cron
    trees. Prose-noun residue still needs a human read."""
    import re as _re
    sch = _load_yaml(pack / "schema.yaml") or {}
    types = set(sch.get("types") or {}) | {"source", "concept", "prediction", "finding",
        "dashboard", "briefing", "trend", "entity", "gap", "term", "lacuna", "battle-card",
        "daily-brief", "weekly-review", "marketing-pulse",
        # first-party extension-owned types (fragments compose them in when enabled)
        "messaging-brief", "value-prop-snapshot", "forecast-review", "portfolio-watch"}
    nss = set((sch.get("partitioning") or {}).get("namespaces") or {}) | {"entities",
        "sources", "concepts", "predictions", "findings", "briefings", "trends", "dashboards",
        "operational", "raw", "gaps", "glossary", "lacuna", "marketing", "reports", "dailies",
        "doctrine", "config"}
    blob = ""
    for f in ("crons/domain-crons.json", "crons/engine-template-prompts.json"):
        p = pack / f
        if p.is_file():
            blob += p.read_text(encoding="utf-8", errors="replace")
    for s in sorted({m for m in _re.findall(r"type:\s*([a-z][a-z0-9-]{2,})", blob) if m not in types})[:6]:
        r.warn("prompt residue", f"prompts reference `type: {s}` — not in this pack's schema (sibling residue?)")
    for s in sorted({m for m in _re.findall(r"\[\[([a-z][a-z0-9-]{2,})/", blob) if m not in nss})[:6]:
        r.warn("prompt residue", f"prompts link [[{s}/ — namespace not declared here")


def check_validator_vintage(pack: Path, r: Report) -> None:
    """#169 class 3: three vintages of vendored validate.py gave three verdicts on one
    contract. The skeleton's copy carries VALIDATE_VERSION; missing/older stamp = refresh."""
    vp = pack / "validate.py"
    if not vp.is_file():
        return
    import re as _re
    m = _re.search(r'VALIDATE_VERSION\s*=\s*"([^"]+)"', vp.read_text(encoding="utf-8", errors="replace"))
    skel = Path(__file__).resolve().parent.parent / "templates/pack/skeleton/validate.py"
    sm = _re.search(r'VALIDATE_VERSION\s*=\s*"([^"]+)"', skel.read_text()) if skel.is_file() else None
    if not m:
        r.warn("validator vintage", "vendored validate.py has no VALIDATE_VERSION stamp — "
                                    "pre-consolidation vintage; refresh from the skeleton")
    elif sm and m.group(1) != sm.group(1):
        r.warn("validator vintage", f"validate.py {m.group(1)} vs skeleton {sm.group(1)} — refresh")


def check_subdomain_form(pack: Path, r: Report) -> None:
    """Single-source rule for the co-install form (authoring-a-pack §8): every type a
    subdomain/ schema or host-schema-additions file declares must exist in the pack's
    MAIN schema with the same required fields — the co-install form is DERIVED from the
    standalone schema, and drift between the two forms is a shipped bug."""
    sub = pack / "subdomain"
    if not sub.is_dir():
        r.info("subdomain form", "none shipped (standalone-only pack; see authoring-a-pack §8)")
        return
    main = _load_yaml(pack / "schema.yaml") or {}
    mtypes = main.get("types") or {}
    for f in sorted(sub.glob("*.yaml")):  # glob-ok: pack subdomain/ dir is flat, not a sharded vault namespace
        d = _load_yaml(f)
        if d is None:
            r.fail("subdomain form", f"{f.name}: unparseable")
            continue
        stypes = d.get("types") or {}
        for tname, tdef in stypes.items():
            if tname not in mtypes:
                r.fail("subdomain form", f"{f.name}: type '{tname}' not in the main schema — "
                                          "the co-install form must be derived, not divergent")
            else:
                mreq = set((mtypes[tname] or {}).get("required") or [])
                sreq = set((tdef or {}).get("required") or [])
                if mreq != sreq:
                    r.fail("subdomain form", f"{f.name}: type '{tname}' required-fields drift "
                                              f"(main {sorted(mreq)} vs form {sorted(sreq)})")
        if stypes:
            r.ok("subdomain form", f"{f.name}: {len(stypes)} type(s), all ⊆ main schema")
        # a subdomain schema (the walk-up contract that LANDS) that declares types but
        # no partitioning leaves the installer with no dirs to create and the subtree's
        # namespace guard a NO-OP — found live on the first automated subtree install
        if (f.name == "schema.yaml" and stypes
                and not ((d.get("partitioning") or {}).get("namespaces"))):
            r.warn("subdomain form", f"{f.name}: declares types but no "
                                     "partitioning.namespaces — the installer creates no "
                                     "dirs and the subtree namespace guard won't enforce")
    if (not (sub / "INSTALL-ALONGSIDE.md").is_file() and not list(sub.glob("INSTALL*.md"))  # glob-ok: flat pack dir
            and not (sub / "README.md").is_file()):
        r.warn("subdomain form", "subdomain/ ships no INSTALL doc — the probes ARE the contract")


def check_persona(pack: Path, r: Report) -> None:
    cp = pack / "CLAUDE.md"
    if not cp.is_file():
        r.fail("CLAUDE.md (persona)", "missing — cron agents read this at $WIKI_PATH/CLAUDE.md")
        return
    txt = cp.read_text(encoding="utf-8", errors="replace")
    if len(txt.strip()) < 80:
        r.fail("CLAUDE.md (persona)", "effectively empty")
        return
    unfilled = [ph for ph in _PLACEHOLDERS if ph in txt]
    if unfilled:
        r.warn("CLAUDE.md filled in", f"{len(unfilled)} scaffold placeholder(s) remain — fill before deploy")
    else:
        r.ok("CLAUDE.md (persona)")


def _engine_meta_mod():
    """Load the sibling engine_meta module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "engine_meta.py"
    spec = importlib.util.spec_from_file_location("engine_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_engine_version(pack: Path, r: Report) -> None:
    # Required: the pack must pin the engine release it targets, AND that pin must
    # match the engine running this validator — you validate (and deploy) against
    # one engine, so the pack must declare that one. This is the single coupling
    # that catches engine/pack drift (the okpack-cti engine-v0.1.0 case).
    ev = pack / "engine.version"
    if not ev.is_file():
        r.fail("engine.version", "missing — pin the engine release the pack targets (e.g. v0.2.0)")
        return
    raw = ev.read_text(encoding="utf-8", errors="replace")
    # engine.version is YAML (engine/version/hermes_pin); read the keys when we
    # can, else fall back to grabbing the first vX.Y.Z token.
    ver = hpin = ""
    if yaml is not None:
        try:
            data = yaml.safe_load(raw)
        except Exception:
            data = None
        if isinstance(data, dict):
            ver = str(data.get("version") or "").strip()
            hpin = str(data.get("hermes_pin") or "").strip()
    if not ver:
        m = _VER_RE.search(raw)
        ver = m.group(0) if m else ""
    if not _VER_RE.fullmatch(ver):
        r.fail("engine.version", f"no vX.Y.Z pin found (got '{ver or raw.strip()[:40]}') — set `version: vX.Y.Z`")
        return

    # Compare against the engine this validator belongs to (single source of truth:
    # engine-manifest.yaml). If unreadable, fall back to the format-only check.
    try:
        meta = _engine_meta_mod()
        target, htag = meta.engine_release(), meta.hermes_pin()
    except Exception:
        target = htag = None
    if not target:
        r.ok("engine.version", ver)
        return
    if ver == target:
        r.ok("engine.version", f"{ver} (matches this engine)")
    elif meta.satisfies_pin(ver, target):
        # same release series — a patch-newer engine is compatible with the pin (okengine#104).
        r.ok("engine.version", f"{ver} pin · engine {target} — compatible (same release series)")
    else:
        r.fail("engine.version",
               f"pins {ver} but this engine is {target} — different release series. Reconcile the pin: "
               f"`framework upgrade <pack> --apply` (bumps engine.version + runs any migrations under a "
               f"roll-forward gate that auto-rolls-back on failure). deploy.sh does this automatically "
               f"before validating (step [0/6]) unless --no-upgrade; only pin back to {ver} if you truly "
               f"need the older engine.")
    if htag and hpin and hpin != htag:
        r.warn("engine.version hermes_pin",
               f"pins {hpin} but this engine targets Hermes {htag}")


def check_feeds(pack: Path, r: Report, probe: bool) -> None:
    raw_meta = _load_yaml(pack / "pack.yaml") or {}
    collection = raw_meta.get("collection") if isinstance(raw_meta, dict) else None
    non_ingest_overlay = (isinstance(collection, dict)
                          and collection.get("mode") == "overlay"
                          and collection.get("feeds") == "none")
    fdir = pack / "feeds"
    opmls = sorted(fdir.glob("*.opml")) if fdir.is_dir() else []  # glob-ok: pack feeds/ is a flat dir, not a sharded content namespace
    if not opmls:
        if non_ingest_overlay:
            r.info("feeds/*.opml", "none by declared collection contract (analysis overlay)")
        else:
            r.warn("feeds/*.opml", "no OPML feed lists (pack may be query/enrichment-only)")
        return
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse

    def safe_root(path: Path) -> ET.Element:
        raw = path.read_bytes()
        if len(raw) > 10 * 1024 * 1024:
            raise ET.ParseError("OPML exceeds 10 MiB safety limit")
        upper = raw[:4096].upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise ET.ParseError("DTD/entity declarations are not permitted")
        return ET.fromstring(raw)  # nosec B314
    urls: list[str] = []
    for f in opmls:
        try:
            root = safe_root(f)
        except (OSError, ET.ParseError) as e:
            r.fail(f"feeds/{f.name} parses", f"XML error: {str(e)[:120]}")
            continue
        found = [u for u in (el.attrib.get("xmlUrl", "").strip()
                             for el in root.iter("outline")) if u]
        urls += found
        r.ok(f"feeds/{f.name}", f"{len(found)} feed url(s)")
    if not urls:
        names = ", ".join(f"feeds/{f.name}" for f in opmls)
        if non_ingest_overlay:
            r.info(names, "0 active feed URLs by declared collection contract (analysis overlay; "
                   "host/connectors own collection)")
        else:
            r.warn(names, "0 active feed URLs — pack is deployable but ingest stays idle until you add "
                   "RSS/Atom <outline xmlUrl=…> entries (suggestions in feeds/*.example). Expected for a "
                   "fresh inert pack; ignore until you enable ingest.")
        return
    if probe:
        import urllib.request
        dead = []
        for u in urls:
            try:
                parsed = urlparse(u)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise ValueError("feed URL must use http(s) and include a host")
                req = urllib.request.Request(u, method="GET", headers={"User-Agent": "framework-validate/1"})
                with urllib.request.urlopen(req, timeout=12) as resp:  # nosec B310
                    if resp.status >= 400:
                        dead.append(f"{u} ({resp.status})")
            except Exception as e:
                dead.append(f"{u} ({type(e).__name__})")
        if dead:
            r.warn("feeds reachable", f"{len(dead)}/{len(urls)} unreachable: " + "; ".join(dead[:5]))
        else:
            r.ok("feeds reachable", f"{len(urls)}/{len(urls)} live")
    else:
        r.info("feeds reachable", f"{len(urls)} url(s) not probed (pass --probe-feeds)")


def check_source_connectors(pack: Path, r: Report) -> None:
    """Validate every declarative source manifest before it can be deployed.

    The runtime validator owns the grammar; framework validate is the pack-lifecycle
    adapter so a malformed permission, inline secret, or impossible retention policy
    fails at authoring/deploy time rather than at the first scheduled tick.
    """
    connector_dir = pack / "connectors"
    if not connector_dir.is_dir():
        return
    # glob-ok: connectors/ is a flat pack configuration directory, never a content namespace.
    paths = sorted((*connector_dir.glob("*.yaml"), *connector_dir.glob("*.yml")))
    if not paths:
        r.warn("connectors/", "directory exists but contains no *.yaml or *.yml manifests")
        return
    try:
        import importlib.util

        module_path = Path(__file__).resolve().parent / "cron" / "source_connector.py"
        spec = importlib.util.spec_from_file_location("okengine_source_connector", module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        r.fail("source connector validator", f"could not load engine validator: {exc}")
        return
    for path in paths:
        label = f"connectors/{path.name}"
        try:
            manifest = module.load_yaml(path)
            errors = module.validate_manifest(manifest)
        except Exception as exc:
            r.fail(label, str(exc))
            continue
        if errors:
            for error in errors:
                r.fail(label, error)
        else:
            r.ok(label, f"{manifest['mode']} connector {manifest['id']}")


def _cron_expr(d: dict) -> str:
    """Resolve a cron def's schedule expression across the supported shapes:
    {"schedule": {"expr": "..."}} | {"schedule": "..."} | {"expr": "..."}.
    Returns "" when no usable expression is present."""
    sched = d.get("schedule")
    if isinstance(sched, dict):
        return str(sched.get("expr") or "").strip()
    if isinstance(sched, str):
        return sched.strip()
    return str(d.get("expr") or "").strip()


def _model_profiles_mod():
    """Load the sibling model_profiles module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "model_profiles.py"
    spec = importlib.util.spec_from_file_location("model_profiles", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_model_profiles(pack: Path, r: Report) -> None:
    """Validate the optional model-profiles registry (okengine#151) and that every `@<profile>`
    reference the operator wrote (pack domain crons + extension-models.json) resolves — fail
    BEFORE deploy, where an undefined reference would otherwise abort the fold."""
    mp = _model_profiles_mod()
    f = pack / ".okengine" / "model-profiles.yaml"
    try:
        profiles = mp.load_profiles(pack)
    except Exception as e:                       # malformed YAML / wrong shape
        r.fail(".okengine/model-profiles.yaml", str(e)[:140])
        return
    if not f.is_file():
        # No registry is fine — but an `@`-ref with no registry can never resolve, so flag it.
        refs = _collect_model_refs(pack, mp)
        if refs:
            r.fail("model profiles", f"{sorted(refs)} referenced but .okengine/model-profiles.yaml "
                                     "is absent — define the profiles or use literal model names")
        else:
            r.info("model profiles", "none declared (lanes use literal models / the config default)")
        return
    shape_errs = mp.validate_profiles(profiles)
    if shape_errs:
        for e in shape_errs:
            r.fail("model profiles", e)
        return
    refs = _collect_model_refs(pack, mp)
    missing = sorted(n for n in refs if n not in profiles)
    if missing:
        r.fail("model profiles", f"undefined profile reference(s): {missing} "
                                 f"(defined: {sorted(profiles)})")
    else:
        r.ok("model profiles", f"{len(profiles)} profile(s); {len(refs)} reference(s) resolve")


def _collect_model_refs(pack: Path, mp) -> set[str]:
    """Profile names referenced (`@name`) across the pack's operator-facing model hooks: domain
    cron defs and the extension-models override map."""
    refs: set[str] = set()
    dc = pack / "crons" / "domain-crons.json"
    if dc.is_file():
        try:
            for d in json.loads(dc.read_text(encoding="utf-8")) or []:
                if isinstance(d, dict) and mp.is_ref(d.get("model")):
                    refs.add(mp.ref_name(d["model"]))
        except (ValueError, OSError):
            pass                                  # shape errors reported by check_crons
    em = pack / ".okengine" / "extension-models.json"
    if em.is_file():
        try:
            for v in (json.loads(em.read_text(encoding="utf-8")) or {}).values():
                if mp.is_ref(v):
                    refs.add(mp.ref_name(v))
        except (ValueError, OSError):
            pass
    return refs


def check_crons(pack: Path, r: Report) -> None:
    oc = None
    try:
        import importlib.util
        oc_path = Path(__file__).resolve().parent / "cron" / "output_contract.py"
        spec = importlib.util.spec_from_file_location("okengine_output_contract", oc_path)
        oc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oc)
    except Exception as exc:
        r.fail("cron output contracts", f"validator failed: {exc}")
    cdir = pack / "crons"
    if not cdir.is_dir():
        r.warn("crons/", "absent — pack contributes no cron defs")
        return
    dc = cdir / "domain-crons.json"
    defs = []
    if dc.is_file():
        try:
            defs = json.loads(dc.read_text(encoding="utf-8"))
            if not isinstance(defs, list):
                r.fail("crons/domain-crons.json shape", "must be a JSON array")
                defs = []
            else:
                r.ok("crons/domain-crons.json", f"{len(defs)} domain cron(s)")
        except Exception as e:
            r.fail("crons/domain-crons.json parses", f"JSON error: {str(e)[:120]}")
    else:
        r.info("crons/domain-crons.json", "absent (no domain crons)")
    etp = cdir / "engine-template-prompts.json"
    if etp.is_file():
        try:
            prompts = json.loads(etp.read_text(encoding="utf-8"))
            if not isinstance(prompts, dict):
                r.fail("crons/engine-template-prompts.json shape", "must be a JSON object")
            else:
                # An engine-template lane pairs an engine wake-gate script with a
                # pack-supplied prompt; an empty prompt = the agent wakes with no
                # instructions, so the lane is broken.
                def _prompt_text(v):
                    return v.get("prompt") if isinstance(v, dict) else v
                empties = [k for k, v in prompts.items() if not str(_prompt_text(v) or "").strip()]
                (r.fail if empties else r.ok)(
                    "crons/engine-template-prompts.json",
                    f"empty prompt(s): {empties}" if empties else f"{len(prompts)} prompt(s)")
                try:
                    contract_errors = []
                    for name, value in prompts.items():
                        if isinstance(value, dict):
                            unknown = sorted(set(value) - {"prompt", "output_contract"})
                            if unknown:
                                contract_errors.append(
                                    f"engine-template prompt {name!r} has unknown key(s): {unknown}")
                            if "output_contract" in value:
                                contract_errors.extend(oc.validate(
                                    value["output_contract"],
                                    f"engine-template prompt {name!r} output_contract"))
                    for error in contract_errors:
                        r.fail("cron output contract", error)
                    if not contract_errors:
                        r.ok("cron output contracts", "engine-template contract shapes valid")
                except Exception as exc:
                    r.fail("cron output contracts", f"validator failed: {exc}")
        except Exception as e:
            r.fail("crons/engine-template-prompts.json parses", f"JSON error: {str(e)[:120]}")
    # shape + script existence for domain defs
    sdir = cdir / "scripts"
    engine_sdir = Path(__file__).resolve().parent / "cron"
    for d in defs:
        if not isinstance(d, dict) or not d.get("name"):
            r.fail("domain cron shape", f"entry missing name: {str(d)[:80]}")
            continue
        # A cron with no usable schedule expression can't be scheduled; one with
        # neither a script nor a prompt has nothing to run — both break the deploy.
        problems = []
        if not _cron_expr(d):
            problems.append("no usable schedule expr")
        if not (d.get("script") or d.get("prompt")):
            problems.append("no script or prompt")
        if problems:
            r.fail(f"cron '{d.get('name')}'", " + ".join(problems))
        if d.get("output_contract") is not None:
            errors = (oc.validate(d["output_contract"], f"cron {d.get('name')!r} output_contract")
                      if oc is not None else ["output-contract validator unavailable"])
            for error in errors:
                r.fail("cron output contract", error)
            fixtures = d.get("adversarial_fixtures")
            if not isinstance(fixtures, list) or not fixtures or any(
                    not isinstance(item, str) or not item.strip() for item in fixtures):
                r.fail("cron adversarial fixtures",
                       f"model-writing cron {d.get('name')!r} with a contract must declare "
                       "adversarial_fixtures")
        elif not d.get("no_agent") and "okengine-write" in (d.get("enabled_toolsets") or []) \
                and not d.get("output_contract_exempt"):
            r.fail("cron output contract",
                   f"model-writing cron {d.get('name')!r} must declare output_contract")
        scr = d.get("script") or ""
        if scr:
            base = Path(scr).name
            if (engine_sdir / base).is_file():
                r.info(f"cron '{d.get('name')}' script", f"{base} supplied by the engine")
            elif not (sdir / base).is_file():
                r.warn(f"cron '{d.get('name')}' script", f"{base} not in crons/scripts/ (engine-provided?)")
    # syntax-check pack scripts in-process (compile() — no .pyc side effect, so a
    # read-only/foreign-owned scripts dir never yields a false positive).
    if sdir.is_dir():
        bad = []
        for py in sorted(sdir.glob("*.py")):  # glob-ok: pack scripts/ is a flat dir, not a sharded content namespace
            try:
                compile(py.read_text(encoding="utf-8", errors="replace"), str(py), "exec")
            except SyntaxError as e:
                bad.append(f"{py.name}:{e.lineno}")
            except OSError:
                pass  # unreadable file is not a syntax verdict
        (r.fail if bad else r.ok)("crons/scripts/*.py compile",
                                  f"syntax errors in: {bad}" if bad else "all parse")


def check_installed_domain_drift(pack: Path, r: Report) -> None:
    """Warn when a composed pack's deployable host copy no longer matches its ownership snapshot."""
    base = pack / ".okengine" / "installed-domains"
    if not base.is_dir():
        return
    try:
        import importlib.util
        path = Path(__file__).resolve().parent / "composed_pack_state.py"
        spec = importlib.util.spec_from_file_location("composed_pack_state_validate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        drift = module.all_installed_drift(pack)
    except Exception as exc:
        r.warn("composed pack drift", f"detector failed: {exc}")
        return
    if drift:
        for item in drift:
            r.warn("composed pack drift", item + "; refresh from the owning pack before deploy")
    else:
        count = len(list(base.glob("*.json")))  # glob-ok: flat installed-domain manifest directory
        r.ok("composed pack drift", f"{count} ownership manifest(s) match")


def check_env(pack: Path, r: Report, *, required: bool = True) -> None:
    ex = pack / ".env.example"
    if not ex.is_file() and required:
        r.warn(".env.example", "missing — operators won't know which secrets to set")
    elif ex.is_file():
        txt = ex.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"(ANTHROPIC_API_KEY|DEEPSEEK_API_KEY|OPENROUTER_API_KEY|GOOGLE_API_KEY)", txt):
            r.warn(".env.example model key", "no model-provider key documented")
        else:
            r.ok(".env.example")
    env = pack / ".env"
    if env.is_file():
        # a real .env must never be committed
        tracked = subprocess.run(["git", "-C", str(pack), "ls-files", "--error-unmatch", ".env"],
                                 capture_output=True, text=True)
        if tracked.returncode == 0:
            r.fail(".env not committed", ".env is git-TRACKED — secrets leak; gitignore + remove it")
        else:
            r.info(".env", "present and untracked (ok)")


def _read_dotenv(pack: Path) -> dict[str, str]:
    env = pack / ".env"
    if not env.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _is_exposed(text: str, env: dict) -> bool:
    """True when the stack is bound beyond localhost. Local-first: the host-port
    interface is OKENGINE_BIND (default 127.0.0.1); exposure is either a non-loopback
    OKENGINE_BIND in .env or a hardcoded non-loopback host in a ports mapping."""
    bind = (env.get("OKENGINE_BIND") or "").strip()
    if bind and bind not in _LOOPBACK:
        return True
    return "0.0.0.0:" in text  # a pack that hardcoded a wide bind in ports


def check_gateway_env(pack: Path, r: Report) -> None:
    """The gateway must receive the pack `.env` so model-provider keys
    (OPENROUTER_API_KEY, …) and delivery tokens reach Hermes — via `env_file` or an
    explicit model-key `environment:` entry. Without it the providers can't
    authenticate and no LLM cron runs (#22)."""
    compose = pack / "docker-compose.yml"
    if not compose.is_file() or yaml is None:
        return
    try:
        data = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    except Exception:
        return   # a malformed compose is surfaced by check_surface_auth's text scan
    gw = (data.get("services") or {}).get("gateway")
    if not isinstance(gw, dict):
        return
    if gw.get("env_file"):
        r.ok("gateway .env passthrough", "env_file")
        return
    env = gw.get("environment") or []
    env_text = " ".join(str(k) for k in env) if isinstance(env, dict) else " ".join(str(x) for x in env)
    if re.search(r"(ANTHROPIC|DEEPSEEK|OPENROUTER|GOOGLE)_API_KEY", env_text):
        r.ok("gateway .env passthrough", "explicit model-key env")
        return
    r.fail("gateway .env passthrough",
           "the gateway service has no `env_file` and passes no model API key — providers won't "
           "authenticate at runtime (no LLM crons / delivery). Add `env_file: [{path: .env, "
           "required: false}]` to the gateway service in docker-compose.yml")


def check_vault_mount(pack: Path, r: Report) -> None:
    """WIKI_PATH must be a vault root whose last segment is NOT `wiki`. The engine derives the
    page tree as `WIKI_PATH/wiki` (write_server.py, build_hot_set.py, …), so `WIKI_PATH=/opt/wiki`
    doubles to `/opt/wiki/wiki` and a stray relative write forks the vault → split-brain
    (okengine#110). The convention is `/opt/vault`; the skeleton + okpack-cti use it."""
    compose = pack / "docker-compose.yml"
    if not compose.is_file() or yaml is None:
        return
    try:
        data = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    paths = set()
    for svc in (data.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment") or []
        pairs = env.items() if isinstance(env, dict) else (
            tuple(e.split("=", 1)) for e in env if isinstance(e, str) and "=" in e)
        for k, v in pairs:
            if str(k).strip() == "WIKI_PATH":
                paths.add(str(v).strip().rstrip("/"))
    if not paths:
        r.ok("vault mount (WIKI_PATH)", "unset — defaults to /opt/vault")
        return
    bad = sorted(p for p in paths if p.rsplit("/", 1)[-1] == "wiki")
    if bad:
        r.fail("vault mount (WIKI_PATH)",
               f"WIKI_PATH={bad[0]} ends in 'wiki' — the engine appends /wiki, so the page tree "
               f"doubles to {bad[0]}/wiki and relative writes fork the vault into a split-brain "
               f"(okengine#110). Mount the vault at /opt/vault and set WIKI_PATH=/opt/vault.")
    elif len(paths) > 1:
        r.warn("vault mount (WIKI_PATH)", f"services disagree on WIKI_PATH: {sorted(paths)}")
    else:
        r.ok("vault mount (WIKI_PATH)", next(iter(paths)))


def check_surface_auth(pack: Path, r: Report) -> None:
    """Local-first guardrail (issues #20/#29): bound to localhost, the generic
    default MCP token is fine and the reader may stay open — a fresh scaffold
    passes. Once exposed beyond localhost, real secrets are REQUIRED (hard FAIL),
    so widening the bind forces the operator to set auth."""
    compose = pack / "docker-compose.yml"
    if not compose.is_file():
        return
    text = compose.read_text(encoding="utf-8", errors="replace")
    env = _read_dotenv(pack)
    has_reader = "okengine-reader" in text and "ports:" in text
    has_cockpit = "okengine-cockpit" in text and "ports:" in text
    has_mcp = "okengine-mcp" in text and "ports:" in text
    if not _is_exposed(text, env):
        r.info("network exposure", "host ports bind loopback (local-first default)")
        # False-confidence trap (okengine#208): the MCP CONTAINER binds 0.0.0.0 internally
        # (Dockerfile ENV OKENGINE_MCP_HOST=0.0.0.0 — Docker port-forwarding needs it), so
        # server.py's #50 fail-closed guard keys "exposed" on THAT, not the loopback HOST-port
        # mapping. With the built-in default token and no ALLOW_DEFAULT_TOKEN, the MCP SystemExits at
        # startup and crash-loops — even on a loopback deploy. deploy.sh avoids it (ensure-runtime
        # generates a secret token into .env); a bare `compose up` following .env.example does not.
        tok = (env.get("OKENGINE_MCP_TOKEN") or "").strip()
        allow = (env.get("OKENGINE_MCP_ALLOW_DEFAULT_TOKEN") or "").strip() == "1"
        if has_mcp and tok == "okengine-local" and not allow:
            r.warn("MCP auth", "OKENGINE_MCP_TOKEN is the built-in default 'okengine-local' — the "
                   "containerized MCP binds 0.0.0.0 and FAILS CLOSED at startup regardless of the "
                   "loopback host-port mapping (#50/#208), so a bare `docker compose up` crash-loops "
                   "it. Run deploy.sh (ensure-runtime generates a secret token), set a real "
                   "OKENGINE_MCP_TOKEN, or OKENGINE_MCP_ALLOW_DEFAULT_TOKEN=1 for a throwaway stack.")
        return
    r.warn("network exposure", "OKENGINE_BIND exposes services beyond localhost — real auth required")
    tok = (env.get("OKENGINE_MCP_TOKEN") or "").strip()
    if has_mcp and tok in ("", "okengine-local"):
        r.fail("MCP auth", "exposed beyond localhost but OKENGINE_MCP_TOKEN is unset or the "
               "built-in default 'okengine-local' — set a real secret")
    elif has_mcp:
        r.ok("MCP auth", "exposed with a custom token")
    # reader/cockpit auth is TRUST-AWARE (okengine#90 P4a): a PUBLIC reference deployment is
    # intentionally open, but a PRIVATE vault exposed without a password is a hard FAIL (both UIs
    # also fail-closes at runtime). They SHARE OKENGINE_READER_PASSWORD — the cockpit is a superset
    # of the reader and must not be laxer. Trust comes from pack.yaml.
    _trust = "private"
    if (pack / "pack.yaml").is_file():
        _trust = str((_load_yaml(pack / "pack.yaml") or {}).get("trust") or "private").strip().lower()
    _has_pw = bool((env.get("OKENGINE_READER_PASSWORD") or "").strip())
    for _label, _present in (("reader auth", has_reader), ("cockpit auth", has_cockpit)):
        if not _present:
            continue
        if not _has_pw:
            if _trust == "public":
                r.warn(_label, "exposed with no password — intended for a PUBLIC pack (anyone can read)")
            else:
                r.fail(_label, "PRIVATE pack exposed beyond localhost with no OKENGINE_READER_PASSWORD "
                       "— set a password, bind to loopback, or declare `trust: public` (#90 P4a)")
        else:
            r.ok(_label, "exposed with a password set")


def _runtime_gitignored(pack: Path) -> bool:
    """True when the pack's .gitignore excludes .hermes-data/ — i.e. this is a
    publishable *definition* repo where the runtime config is seeded at deploy,
    not committed (so a clone/CI checkout legitimately lacks config.yaml)."""
    gi = pack / ".gitignore"
    if not gi.is_file():
        return False
    for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
        if ".hermes-data" in line.split("#", 1)[0]:
            return True
    return False


def check_runtime_config(pack: Path, r: Report) -> None:
    cfg = pack / ".hermes-data" / "config.yaml"
    if not cfg.is_file():
        # Context-aware: the runtime config is deploy-time state. In a definition
        # repo (.hermes-data gitignored) its absence is expected → WARN. Elsewhere
        # (a deploy-ready dir that should have seeded it) it's a FAIL.
        if _runtime_gitignored(pack):
            r.info(".hermes-data/config.yaml", "absent in definition checkout (gitignored runtime "
                   "state); `framework init`/`pull` or `scripts/ensure-runtime.sh` must seed it "
                   "before deployment")
        else:
            r.fail(".hermes-data/config.yaml", "missing — copy the engine config template and fill deployment keys")
        return
    if yaml is None:
        r.warn(".hermes-data/config.yaml", "PyYAML unavailable; skipped parse")
        return
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception as e:
        r.fail(".hermes-data/config.yaml parses", f"YAML error: {str(e)[:140]}")
        return
    if not isinstance(data, dict):
        r.fail(".hermes-data/config.yaml shape", "top level is not a mapping")
        return
    missing = []
    if ((data.get("terminal") or {}).get("backend") != "local"):
        missing.append("terminal.backend: local")
    servers = data.get("mcp_servers") or {}
    if not isinstance(servers, dict) or "okengine" not in servers:
        missing.append("mcp_servers.okengine")
    if not isinstance(servers, dict) or "okengine-write" not in servers:
        missing.append("mcp_servers.okengine-write")
    scoped_writer_missing = (not isinstance(servers, dict)
                             or "okengine-write-source-quality" not in servers)
    if missing:
        r.fail(".hermes-data/config.yaml required keys", ", ".join(missing))
    else:
        r.ok(".hermes-data/config.yaml")
    if scoped_writer_missing:
        r.warn("mcp_servers.okengine-write-source-quality",
               "missing from an older runtime config; ensure-runtime.sh will add the "
               "server-bound job identity before containers are recreated")
    else:
        r.ok("mcp_servers.okengine-write-source-quality", "server-bound job identity declared")
    # The seeded read-MCP Authorization header must be a real token, not the
    # template placeholder — an unsubstituted `<...>` 401s the gateway agent on
    # every read-MCP call (okengine#32).
    if isinstance(servers, dict):
        auth = (((servers.get("okengine") or {}).get("headers") or {}).get("Authorization") or "")
        if "<" in auth or "from pack .env" in auth:
            r.fail("config.yaml okengine MCP auth", "Authorization still holds the template "
                   "placeholder — re-seed via ensure-runtime.sh so the header matches the read "
                   "server token (okengine#32)")
        elif auth and not auth.startswith("Bearer "):
            r.warn("config.yaml okengine MCP auth", "Authorization is not a `Bearer <token>` value")


def check_docs(pack: Path, r: Report) -> None:
    """A pack must document itself — the README an operator reads to deploy and
    understand it, plus a LICENSE. Missing README, a stub, no Deploy section, or
    a missing LICENSE is a FAIL; thin layout/structure coverage WARNs."""
    readme = pack / "README.md"
    if not readme.is_file():
        r.fail("README.md", "missing — a pack must ship a README (what it ingests, deploy, layout)")
        return
    txt = readme.read_text(encoding="utf-8", errors="replace")
    headings = [ln for ln in txt.splitlines() if ln.lstrip().startswith("## ")]
    if len(txt.strip()) < 200 or not headings:
        r.fail("README.md", "stub — document the pack (what it ingests, deploy, layout)")
        return
    # A Deploy section is MANDATORY — the operator must be told how to bring the
    # pack up. Match against heading text (a real section), not a prose mention.
    head_text = "\n".join(ln.lower() for ln in txt.splitlines() if ln.lstrip().startswith("#"))
    if not any(k in head_text for k in ("deploy", "install", "bring up", "quickstart", "getting started")):
        r.fail("README.md Deploy section",
               "no Deploy/Install/Quickstart heading — document how to bring the pack up")
    else:
        r.ok("README.md", f"{len(headings)} section(s)")
    low = txt.lower()
    if not any(k in low for k in ("layout", "services", "structure", "schema")):
        r.warn("README.md sections", "no layout/structure section found")
    # A pack must declare a license — required to publish/redistribute it.
    lic = next((pack / n for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")
                if (pack / n).is_file()), None)
    if lic and lic.read_text(encoding="utf-8", errors="replace").strip():
        r.ok("LICENSE", lic.name)
    else:
        r.fail("LICENSE", "missing — every pack must ship a license (LICENSE / LICENSE.md / COPYING)")


def check_wiki(pack: Path, r: Report) -> None:
    w = pack / "wiki"
    if not w.is_dir():
        r.warn("wiki/", "absent — the content tree the engine compiles into")
        return
    r.ok("wiki/", "present")


def _pack_meta_mod():
    """Load the sibling pack_meta module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "pack_meta.py"
    spec = importlib.util.spec_from_file_location("pack_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_pack_meta(pack: Path, r: Report) -> None:
    """Shape-check the pack's pack.yaml (identity + composition metadata). Full
    cross-pack composition validation happens at deploy across all installed
    packs; here we only validate this pack's own declaration."""
    if not (pack / "pack.yaml").is_file():
        r.warn("pack.yaml", "absent — recommended for composition (name/version/owns/requires)")
        return
    try:
        meta = _pack_meta_mod().load_pack_meta(pack)
    except Exception as e:
        r.fail("pack.yaml", f"could not load: {str(e)[:120]}")
        return
    if meta is None:
        r.fail("pack.yaml parses", "present but unparseable / not a mapping")
        return
    if meta["trust"] not in ("public", "private"):
        r.fail("pack.yaml trust", f"'{meta['trust']}' — must be public or private")
    if not (meta["owns_types"] or meta["owns_namespaces"]) and meta.get("kind") != "bundle":
        # Valid but minimal: a pack may own nothing and inherit the engine core (okengine#90);
        # it just contributes no domain ids yet. Nudge, don't fail. (A bundle owns nothing BY
        # DESIGN — okengine#181 — so it's exempt; check_bundle validates its recipe instead.)
        r.warn("pack.yaml owns", "declares no owned types/namespaces (inherits the engine core)")
    r.ok("pack.yaml", f"{meta['name']} v{meta['version']} "
         f"({meta['trust']}; owns {len(meta['owns_types'])} type(s), "
         f"{len(meta['owns_namespaces'])} namespace(s))")
    if meta.get("port_offset"):
        r.info("pack.yaml port_offset", f"{meta['port_offset']} (reader {9200 + meta['port_offset']}, "
               f"mcp {8730 + meta['port_offset']} — applied by framework pull)")
    # description/mission feed the reader/cockpit ABOUT panel, the catalog blurb and
    # `framework list` — one declaration, three surfaces (multi-surface rule). Read
    # raw: load_pack_meta normalizes composition keys only.
    raw = _load_yaml(pack / "pack.yaml") or {}
    collection = raw.get("collection")
    if collection is not None:
        if not isinstance(collection, dict):
            r.fail("pack.yaml collection", "must be a mapping")
        else:
            mode, feeds = collection.get("mode"), collection.get("feeds")
            if (mode, feeds) != ("overlay", "none"):
                r.fail("pack.yaml collection", "supported declaration is "
                       "{mode: overlay, feeds: none}; omit collection for ingest-capable packs")
            else:
                r.ok("pack.yaml collection", "analysis overlay; host/connectors own collection")
    if not str(raw.get("description") or "").strip():
        r.warn("pack.yaml description", "missing — the About panel and catalog have "
                                        "nothing to say about this deployment's purpose")
    elif "TODO" in str(raw.get("mission") or ""):
        r.warn("pack.yaml mission", "still the scaffold TODO — write the reader-facing paragraph")


def check_owns_covers_schema(pack: Path, r: Report) -> None:
    """Every NON-CORE type in schema.yaml `types:` and every pack-INTRODUCED partitioning namespace
    must be declared in pack.yaml `owns:`. compose-preview builds each secondary pack's schema
    fragment from `owns.types` / `owns.namespaces` ONLY (framework_compose_preview.analyze), so a type
    or namespace that lives in schema.yaml but is ABSENT from owns is INVISIBLE to the fail-loud
    co-install collision detector — two packs could silently claim the same undeclared type and the
    safety gate would never see it. owns is the compose contract; a schema/owns divergence is the gate
    hole this closes (invariant-audit #351). Core (engine base-schema) types/namespaces are shared and
    owned by no pack, so re-declaring them needs no owns entry."""
    if not (pack / "pack.yaml").is_file() or not (pack / "schema.yaml").is_file():
        return                                   # absence is flagged by check_pack_meta / check_schema
    try:
        meta = _pack_meta_mod().load_pack_meta(pack)
    except Exception:
        return                                   # a load fault is reported by check_pack_meta
    if meta is None or meta.get("kind") == "bundle":
        return                                   # a bundle owns nothing BY DESIGN (#181)
    sch = _load_yaml(pack / "schema.yaml")
    if not isinstance(sch, dict):
        return                                   # parse fault flagged by check_schema
    base = _load_yaml(Path(__file__).resolve().parents[1] / "config" / "base-schema.yaml") or {}
    base_types = set(base.get("types") or {}) if isinstance(base, dict) else set()
    base_ns = set((base.get("partitioning") or {}).get("namespaces") or {})
    schema_types = set(sch.get("types") or {}) if isinstance(sch.get("types"), dict) else set()
    schema_ns = set((sch.get("partitioning") or {}).get("namespaces") or {})
    owns_types = set(meta.get("owns_types") or [])
    owns_ns = set(meta.get("owns_namespaces") or [])
    # A namespace in schema.exclude is intentionally OUTSIDE this pack's OKF scope (a shared render
    # tree like `dashboards`/`operational`, or an archive) — it is deliberately NOT owned, so it needs
    # no owns entry and must not warn. exclude entries are paths (`wiki/operational/`); reduce to the
    # bare namespace leaf to compare against partitioning.namespaces.
    excluded_ns = {str(e).replace("wiki/", "").strip("/").split("/")[0]
                   for e in (sch.get("exclude") or []) if str(e).strip()}
    # WARN, not FAIL: an incomplete `owns` is a co-install collision BLIND SPOT, but it only bites a
    # pack that is actually composed with a colliding pack — and compose-preview / install-domain FAIL
    # LOUD on a real collision. A standalone pack (the common case) with `owns` narrower than its
    # schema is harmless, and a hard FAIL here would break `framework validate` (the deploy gate AND
    # pack-repo CI) for the whole existing fleet, which lags this convention. Surface it so authors
    # complete owns for safe future composition, without blocking a working deploy (invariant-audit
    # #351; severity corrected after the v0.13.1 fleet roll showed every pack tripping it).
    for t in sorted((schema_types - base_types) - owns_types):
        r.warn("pack.yaml owns.types",
               f"schema.yaml declares non-core type '{t}' but it is not in owns.types — compose-preview "
               f"builds this pack's fragment from owns, so '{t}' is invisible to the co-install "
               f"collision gate. Add '{t}' to owns.types for safe composition (harmless standalone).")
    for ns in sorted((schema_ns - base_ns) - owns_ns - excluded_ns):
        r.warn("pack.yaml owns.namespaces",
               f"schema.yaml partitions namespace '{ns}' but it is neither in owns.namespaces nor "
               f"schema.exclude — it is invisible to the co-install collision gate. Add '{ns}' to "
               f"owns.namespaces (own it) or schema.exclude (shared render tree).")


def _is_bundle(pack: Path) -> bool:
    """True iff the pack declares `kind: bundle` (owns nothing; composes other packs)."""
    if not (pack / "pack.yaml").is_file():
        return False
    try:
        meta = _pack_meta_mod().load_pack_meta(pack)
    except Exception:
        return False
    return bool(meta and meta.get("kind") == "bundle")


def check_bundle(pack: Path, r: Report) -> None:
    """Validate a `kind: bundle` pack's recipe (okengine#181): owns-nothing, a `host` base
    pack, a non-empty `compose` list (host not in it, no self/dupes), and every recipe member
    declared in `requires`. A bundle ships no schema/persona/crons/feeds/wiki, so the
    domain-content checks are skipped for it (see validate())."""
    try:
        meta = _pack_meta_mod().load_pack_meta(pack)
    except Exception as e:
        r.fail("bundle recipe", f"could not load pack.yaml: {str(e)[:120]}")
        return
    errs = _pack_meta_mod().validate_bundle_recipe(meta or {})
    if errs:
        for e in errs:
            r.fail("bundle recipe", e)
    else:
        r.ok("bundle recipe",
             f"host {meta['bundle_host']} + composes {len(meta['bundle_compose'])} pack(s): "
             f"{', '.join(meta['bundle_compose'])}")


def _discovery_mod():
    import importlib.util
    p = Path(__file__).resolve().parent / "extension_discovery.py"
    spec = importlib.util.spec_from_file_location("extension_discovery", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _schema_ext_owners(pack: Path) -> set[str]:
    """Extension ids hard-referenced as owners in the pack's schema.yaml ``owners:`` map
    (grammar: engine | pack:<name> | ext:<id>) — implicit pack->extension deps (#142 D)."""
    sp = pack / "schema.yaml"
    if not sp.is_file():
        return set()
    data = _load_yaml(sp) or {}
    owners = data.get("owners") if isinstance(data, dict) else None
    out: set[str] = set()
    for grp in ("types", "fields"):
        for owner in ((owners or {}).get(grp) or {}).values():
            if isinstance(owner, str) and owner.startswith("ext:"):
                out.add(owner[len("ext:"):])
    return out


def check_extension_requirements(pack: Path, r: Report) -> None:
    """okengine#142 (A+D): a pack can require an extension (`requires: [ext:<id>@>=ver]`),
    and a pack schema can annotate an `ext:<id>` owner. Both must resolve to an ENABLED
    extension (explicit or core-default-on) at the version floor — fail-loud BEFORE deploy
    rather than degrade silently at runtime when the operator didn't enable it."""
    pm = _pack_meta_mod()
    meta = None
    if (pack / "pack.yaml").is_file():
        try:
            meta = pm.load_pack_meta(pack)
        except Exception:
            return                              # pack.yaml errors already reported above
    ext_reqs = pm.extension_requires(meta) if meta else []
    schema_owners = _schema_ext_owners(pack)
    if not ext_reqs and not schema_owners:
        return                                  # no declared pack->extension coupling

    disc = _discovery_mod()
    resolved, errs = disc.resolve_for_pack(pack)
    for e in errs:
        r.warn("extension resolve", e)
    versions = {eid: str(rec.get("manifest", {}).get("version", "0"))
                for eid, rec in resolved.items()}

    for ext_id, spec in ext_reqs:
        if ext_id not in resolved:
            r.fail(f"requires ext:{ext_id}",
                   f"required by the pack but not enabled — "
                   f"`framework extensions enable <pack> {ext_id}` (or mark it core)")
        elif spec and not pm.satisfies(versions[ext_id], spec):
            r.fail(f"requires ext:{ext_id}@{spec}",
                   f"enabled at {versions[ext_id]} — version floor not met")
        else:
            r.ok(f"requires ext:{ext_id}", f"enabled ({versions[ext_id]})")

    for ext_id in sorted(schema_owners - {e for e, _ in ext_reqs}):
        if ext_id not in resolved:
            r.fail(f"schema owner ext:{ext_id}",
                   "a type/field is owned by this extension but it isn't enabled — "
                   "enable it or drop the owner annotation")
        else:
            r.ok(f"schema owner ext:{ext_id}", "enabled")


def check_enabled_extensions_resolve(pack: Path, r: Report) -> None:
    """Every id in <pack>/.okengine/extensions.yaml `enabled:` must still be DISCOVERED — an enabled
    id that no longer resolves (e.g. an engine upgrade renamed a tier-1 extension the operator had
    enabled) otherwise slips past `framework validate` and first hard-stops at deploy.sh step 5, AFTER
    step 4 already recreated every container (with --no-crons, never at all). check_extension_requirements
    only covers ids the pack DECLARES via requires:/schema-owner, so an enabled-only id was unguarded
    at the fail-fast gate (invariant-audit #39). This mirrors framework_extensions._cmd_validate."""
    if not (pack / ".okengine" / "extensions.yaml").is_file():
        return                                  # nothing enabled -> nothing to resolve
    disc = _discovery_mod()
    try:
        discovered, disc_errors = disc.discover(pack)
        enabled, en_errors = disc.load_enabled_state(pack)
        _, res_errors = disc.resolve_enabled(list(enabled), discovered)
    except Exception as e:                       # discovery faults are reported elsewhere; don't crash
        r.warn("enabled extensions", f"could not resolve enabled set: {e}")
        return
    # disc_errors carries discover()'s Rule-1/Rule-2 faults (notably a cross-tier duplicate id — a
    # HARD FAIL per the discovery spec). resolve_enabled() indexes discovered records into a dict
    # keyed by bare id, so a duplicate is silently last-wins and yields NO res_errors — dropping
    # disc_errors here let `framework validate` / `pull --update` report an ambiguous extension as
    # clean (invariant-audit #351). Fold them into the fail set.
    problems = list(en_errors) + list(res_errors) + list(disc_errors)
    if problems:
        for p in problems:
            r.fail("enabled extension", p if not p.startswith("FAIL") else p[5:].strip())
    elif enabled:
        r.ok("enabled extensions", f"{len(enabled)} enabled, all discovered")


def check_tokens(pack: Path, r: Report) -> None:
    """Unrendered scaffold placeholders ({{UPPER_SNAKE}}) mean a config/variable
    framework_init never filled — a guaranteed-broken deploy. Hard FAIL on any
    surviving token in a declarative pack file."""
    hits: list[str] = []
    for rel in _TOKEN_SCAN:
        p = pack / rel
        if not p.is_file():
            continue
        toks = sorted(set(_TOKEN_RE.findall(p.read_text(encoding="utf-8", errors="replace"))))
        if toks:
            hits.append(f"{rel}: {', '.join(toks[:6])}")
    if hits:
        r.fail("unrendered {{tokens}}", "; ".join(hits))
    else:
        r.ok("template tokens", "all rendered")


def check_application_profile(pack: Path, r: Report) -> None:
    """Validate an optional supported-application declaration.

    The application module owns the grammar and profile catalog. Keeping this adapter small makes
    ``framework validate`` the one author-facing conformance command instead of creating a second
    application CLI.
    """
    declaration = pack / ".okengine" / "application.yaml"
    if not declaration.is_file():
        return
    try:
        import importlib.util

        path = Path(__file__).resolve().parent / "application_profiles.py"
        spec = importlib.util.spec_from_file_location("application_profiles", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        errors = module.validate(pack, Path(__file__).resolve().parents[1])
    except Exception as exc:
        r.fail("application profile", f"validator failed: {exc}")
        return
    if errors:
        for error in errors:
            r.fail("application profile", error)
    else:
        data = _load_yaml(declaration) or {}
        r.ok("application profile", f"{data.get('profile')} {data.get('profile_version')}")


def check_policy_plane(pack: Path, r: Report) -> None:
    """Compose engine, pack, and extension policy before deployment."""
    try:
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "tools" / "policy_plane.py"
        spec = importlib.util.spec_from_file_location("okengine_policy_plane", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        policy = module.effective_policy(pack)
    except Exception as exc:
        r.fail("policy plane", str(exc))
        return
    r.ok("policy plane", f"{len(policy['rules'])} rule(s); digest {policy['digest'][:12]}")
    prompt_path = pack / "crons" / "engine-template-prompts.json"
    if prompt_path.is_file():
        try:
            prompts = json.loads(prompt_path.read_text(encoding="utf-8"))
            prompt_value = prompts.get("source-quality-backfill") or ""
            prompt = str(prompt_value.get("prompt") or "") if isinstance(prompt_value, dict) \
                else str(prompt_value)
            errors = module.check_prompt(policy, "cron:source-quality-backfill", prompt)
        except Exception as exc:
            errors = [str(exc)]
        for error in errors:
            r.fail("source-quality capability/prompt", error)
        if not errors:
            r.ok("source-quality capability/prompt", "prompt conforms to enforced field/body authority")


def validate(pack: Path, probe: bool = False) -> Report:
    r = Report()
    check_tokens(pack, r)
    if _is_bundle(pack):
        # A bundle (okengine#181) owns nothing and ships no schema/persona/crons/feeds/wiki —
        # it composes other packs. Validate identity + recipe + engine pin + docs and the
        # tracked-secret guard; a non-runtime recipe has no environment to document. The
        # domain-content checks below don't apply and would spuriously FAIL on absent files.
        check_pack_meta(pack, r)
        check_bundle(pack, r)
        check_engine_version(pack, r)
        check_docs(pack, r)
        check_env(pack, r, required=False)
        return r
    check_schema(pack, r)
    check_persona(pack, r)
    check_engine_version(pack, r)
    check_pack_meta(pack, r)
    check_owns_covers_schema(pack, r)
    check_extension_requirements(pack, r)
    check_enabled_extensions_resolve(pack, r)
    check_application_profile(pack, r)
    check_policy_plane(pack, r)
    check_feeds(pack, r, probe)
    check_source_connectors(pack, r)
    check_crons(pack, r)
    check_installed_domain_drift(pack, r)
    check_model_profiles(pack, r)
    check_env(pack, r)
    check_gateway_env(pack, r)
    check_vault_mount(pack, r)
    check_surface_auth(pack, r)
    check_runtime_config(pack, r)
    check_docs(pack, r)
    check_wiki(pack, r)
    check_subdomain_form(pack, r)
    check_compose_drift(pack, r)
    check_prompt_residue(pack, r)
    check_validator_vintage(pack, r)
    return r


_GLYPH = {"OK": "✓", "INFO": "·", "WARN": "⚠", "FAIL": "✗"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Validate an OKF domain pack before deploy.")
    ap.add_argument("pack", help="path to the domain pack directory")
    ap.add_argument("--probe-feeds", action="store_true", help="HTTP-probe feed URLs (network)")
    ap.add_argument("--quiet", action="store_true", help="only print WARN/FAIL + summary")
    args = ap.parse_args(argv)
    pack = Path(args.pack).expanduser()
    if not pack.is_dir():
        print(f"ERROR: pack dir not found: {pack}", file=sys.stderr)
        return 2
    r = validate(pack, probe=args.probe_feeds)
    print(f"framework validate — {pack}\n")
    for sev, check, detail in r.rows:
        if args.quiet and sev in ("OK", "INFO"):
            continue
        line = f"  {_GLYPH[sev]} [{sev}] {check}"
        print(f"{line}: {detail}" if detail else line)
    verdict = "FAIL" if r.n_fail else ("PASS-with-warnings" if r.n_warn else "PASS")
    print(f"\n{verdict} — {r.n_fail} fail, {r.n_warn} warn, {len(r.rows)} checks")
    return 1 if r.n_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
