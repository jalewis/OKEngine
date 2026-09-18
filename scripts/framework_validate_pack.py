"""Pack schema, source, and conformance validation services."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from scripts.framework_validate_report import Report
from okengine.schema_exclusions import exclusion_globs_from_schema, excluded_namespaces_from_schema


class PackChecks:
    def __init__(
        self, *, engine_root: Path, yaml_module, load_yaml, engine_meta, version_re, placeholders
    ):
        self.engine_root = engine_root
        self.yaml = yaml_module
        self.load_yaml = load_yaml
        self.engine_meta = engine_meta
        self.version_re = version_re
        self.placeholders = placeholders

    def check_schema(self, pack: Path, r: Report) -> None:
        sp = pack / "schema.yaml"
        if not sp.is_file():
            r.fail("schema.yaml", "missing — the pack's contract is required")
            return
        if self.yaml is None:
            r.warn("schema.yaml", "PyYAML unavailable; skipped parse")
            return
        sch = self.load_yaml(sp)
        if sch is None:
            r.fail("schema.yaml parses", "empty or unparseable YAML")
            return
        if not isinstance(sch, dict):
            r.fail("schema.yaml shape", "top level is not a mapping")
            return
        r.ok("schema.yaml parses")
        try:
            excluded = excluded_namespaces_from_schema(sch)
            globs = exclusion_globs_from_schema(sch)
        except ValueError as exc:
            r.fail("schema.exclude", str(exc))
        else:
            r.ok("schema.exclude", f"{len(excluded)} namespace(s), {len(globs)} page glob(s)")
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
            bad = [
                t
                for t, d in types.items()
                if not isinstance(d, dict) or "type" not in (d.get("required") or ["type"])
            ]
            if bad:
                r.warn("schema.types[].required", f"types whose required list omits `type`: {bad}")
            else:
                r.ok("schema.types", f"{len(types)} types")
        for block in ("partitioning", "hot_set"):
            (r.ok if isinstance(sch.get(block), dict) else r.warn)(
                f"schema.{block}",
                "" if isinstance(sch.get(block), dict) else "absent (engine defaults apply)",
            )
        # Each partitioned namespace's `strategy` must be one okf_migrate KNOWS — a typo (`by_date`) or
        # invented value silently degraded to flat in _new_key while every 'is this partitioned?' matcher
        # tests `strategy != "flat"` and treated it as partitioned, forking canonicals (invariant-audit
        # #25). Gate it at validate — the earliest gate — so it never reaches the drain.
        _VALID_STRATEGIES = {"flat", "by-letter", "by-date", "by-type"}
        part_ns = (
            ((sch.get("partitioning") or {}).get("namespaces") or {})
            if isinstance(sch, dict)
            else {}
        )
        if isinstance(part_ns, dict):
            for ns, cfg in part_ns.items():
                strat = (cfg or {}).get("strategy", "flat") if isinstance(cfg, dict) else "flat"
                if strat not in _VALID_STRATEGIES:
                    r.fail(
                        f"partitioning.{ns}.strategy",
                        f"unknown strategy {strat!r} — valid: {sorted(_VALID_STRATEGIES)} "
                        f"(an unknown value silently degrades to flat while drains treat it as partitioned)",
                    )
        for block in ("permissions", "review", "tier"):
            if isinstance(sch.get(block), dict):
                r.info(f"schema.{block}", "declared (G2/G3/G4 policy)")
        if "strict_types" in sch and not isinstance(sch.get("strict_types"), bool):
            r.fail(
                "schema.strict_types", "must be a boolean (true closes the composed type taxonomy)"
            )
        local_type_names = set(types) if isinstance(types, dict) else set()
        # Pack schemas are additive fragments over the engine-owned core. Taxonomy
        # inputs may target core types without illegally redeclaring those types.
        base = self.load_yaml(self.engine_root / "config" / "base-schema.yaml") or {}
        base_types = base.get("types") if isinstance(base, dict) else {}
        effective_type_names = local_type_names | (
            set(base_types) if isinstance(base_types, dict) else set()
        )
        self._check_engine_inputs(sch, effective_type_names, r)

    def _check_engine_inputs(self, sch: dict, type_names: set, r: Report) -> None:
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
                unknown = (
                    sorted({str(v) for v in aliases.values()} - type_names) if type_names else []
                )
                shadow = sorted({str(k) for k in aliases} & type_names)
                if unknown:
                    r.warn("schema.type_aliases", f"alias target(s) not in `types:` {unknown}")
                if shadow:
                    # FAIL, not WARN — same severity as coinstall_preflight and the
                    # deployment-validate lane. An alias key that IS a declared type makes
                    # the normalization drains silently retype canonical pages; a pack-side
                    # WARN here let exactly that reach a live deployment (the digest-alias
                    # class caught on the v0.9.0 readiness sweep).
                    r.fail(
                        "schema.type_aliases",
                        f"alias key(s) SHADOW declared types "
                        f"(drains would silently retype pages — retire the "
                        f"alias or the type): {shadow}",
                    )
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
                    f"key(s) not in `types:` {unknown}"
                    if unknown
                    else f"{len(hints)} hinted type(s)",
                )
        # list-shaped; the type-referencing ones warn on undeclared entries
        for key, refs_types in (
            ("operational_types", True),
            ("classify_catchall", True),
            ("depth_critical_types", True),
            ("protected_fields", False),
        ):
            val = sch.get(key)
            if val is None:
                continue
            if not isinstance(val, list):
                r.fail(f"schema.{key}", "must be a list")
                continue
            if refs_types and type_names:
                unknown = sorted({str(x) for x in val} - type_names)
                (r.warn if unknown else r.info)(
                    f"schema.{key}",
                    f"entr(ies) not in `types:` {unknown}" if unknown else f"{len(val)} entr(ies)",
                )
            else:
                r.info(f"schema.{key}", f"{len(val)} entr(ies)")

    def check_compose_drift(self, pack: Path, r: Report) -> None:
        """#169 class 2: pack composes are hand-copied skeleton snapshots and drift (one live
        pack shipped its reader with ZERO auth env). Checks fail-safe env plumbing and that
        pack.yaml's port_offset agrees with the bound ports."""
        cf = pack / "docker-compose.yml"
        if not cf.is_file():
            return
        try:
            comp = self.yaml.safe_load(cf.read_text()) or {}
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
                    r.fail(
                        "compose drift",
                        f"{svc} missing {var} plumbing — the auth/trust "
                        "fail-safe is skeleton-standard (okengine#90 P4a)",
                    )
        meta = self.load_yaml(pack / "pack.yaml") or {}
        if meta.get("port_offset"):
            import re as _re

            # Scan only ACTUAL bindings: drop full-line comments first, so a commented-out example
            # (e.g. a doc line showing the un-offset mcp port `# ports: [...:8730:8730]`) is not
            # mistaken for a live binding and does not trip the drift check.
            raw = "\n".join(
                ln for ln in cf.read_text().splitlines() if not ln.lstrip().startswith("#")
            )
            base_hits = sorted(
                {p for p in _re.findall(r":(\d{4,5}):\d+", raw) if p in ("9200", "9201", "8730")}
            )
            if base_hits:
                r.fail(
                    "compose drift",
                    f"pack.yaml declares port_offset {meta['port_offset']} but "
                    f"compose binds un-offset base port(s) {base_hits}",
                )

    def check_prompt_residue(self, pack: Path, r: Report) -> None:
        """#169 class 4 (structural form): prompts referencing `type:` tokens or [[ns/ link
        prefixes the pack's schema doesn't declare — sibling-domain residue from cloned cron
        trees. Prose-noun residue still needs a human read."""
        import re as _re

        sch = self.load_yaml(pack / "schema.yaml") or {}
        types = set(sch.get("types") or {}) | {
            "source",
            "concept",
            "prediction",
            "finding",
            "dashboard",
            "briefing",
            "trend",
            "entity",
            "gap",
            "term",
            "lacuna",
            "battle-card",
            "daily-brief",
            "weekly-review",
            "marketing-pulse",
            # first-party extension-owned types (fragments compose them in when enabled)
            "messaging-brief",
            "value-prop-snapshot",
            "forecast-review",
            "portfolio-watch",
        }
        nss = set((sch.get("partitioning") or {}).get("namespaces") or {}) | {
            "entities",
            "sources",
            "concepts",
            "predictions",
            "findings",
            "briefings",
            "trends",
            "dashboards",
            "operational",
            "raw",
            "gaps",
            "glossary",
            "lacuna",
            "marketing",
            "reports",
            "dailies",
            "doctrine",
            "config",
        }
        blob = ""
        for f in ("crons/domain-crons.json", "crons/engine-template-prompts.json"):
            p = pack / f
            if p.is_file():
                blob += p.read_text(encoding="utf-8", errors="replace")
        for s in sorted(
            {m for m in _re.findall(r"type:\s*([a-z][a-z0-9-]{2,})", blob) if m not in types}
        )[:6]:
            r.warn(
                "prompt residue",
                f"prompts reference `type: {s}` — not in this pack's schema (sibling residue?)",
            )
        for s in sorted(
            {m for m in _re.findall(r"\[\[([a-z][a-z0-9-]{2,})/", blob) if m not in nss}
        )[:6]:
            r.warn("prompt residue", f"prompts link [[{s}/ — namespace not declared here")

    def check_validator_vintage(self, pack: Path, r: Report) -> None:
        """#169 class 3: several vintages of vendored validate.py give different verdicts on one
        contract. The stamp alone cannot detect that, and demonstrably did not (okengine#606).

        FOUR distinct contents shipped under `VALIDATE_VERSION = "2026.07.3"` — the engine skeleton
        plus three vintages across the pack repos — each carrying a fix the others
        lacked (the okengine#178 @jitter-base check was in six copies and missing from four;
        https-only feed probing was in the pack copies and not the skeleton; string-form schedules
        were handled only by the skeleton). The stamp is hand-maintained, so a fix that skips the
        bump is invisible to a stamp comparison by construction.

        So the stamp is now treated as a LABEL and the content as the fact:

          - no stamp                     -> WARN (pre-consolidation vintage; refresh)
          - stamp differs from skeleton  -> WARN (honest staleness; refresh)
          - stamp MATCHES, content does not -> FAIL

        Only the last is new, and it is the defect class itself: a copy asserting it is the
        skeleton's vintage while being a different program. A pack that is merely behind still
        warns exactly as before, so refreshing the fleet does not become a precondition for this
        check landing.

        Bundle packs are exempt: a bundle owns no types and ships no schema.yaml, so it carries a
        recipe validator that is legitimately a different file (okengine#181).
        """
        vp = pack / "validate.py"
        if not vp.is_file():
            return
        import re as _re

        meta_path = pack / "pack.yaml"
        if meta_path.is_file():
            # Regex, not self.yaml.safe_load: `yaml` is optional in this module (it can be None), and a
            # bundle misread as a normal pack would FAIL on a file that is correctly different.
            if _re.search(
                r"^kind:\s*[\"']?bundle[\"']?\s*$",
                meta_path.read_text(encoding="utf-8", errors="replace"),
                _re.M,
            ):
                return
        text = vp.read_text(encoding="utf-8", errors="replace")
        m = _re.search(r'VALIDATE_VERSION\s*=\s*"([^"]+)"', text)
        skel = Path(__file__).resolve().parent.parent / "templates/pack/skeleton/validate.py"
        skel_text = skel.read_text(encoding="utf-8") if skel.is_file() else None
        sm = _re.search(r'VALIDATE_VERSION\s*=\s*"([^"]+)"', skel_text) if skel_text else None
        if not m:
            r.warn(
                "validator vintage",
                "vendored validate.py has no VALIDATE_VERSION stamp — "
                "pre-consolidation vintage; refresh from the skeleton",
            )
        elif sm and m.group(1) != sm.group(1):
            r.warn(
                "validator vintage", f"validate.py {m.group(1)} vs skeleton {sm.group(1)} — refresh"
            )
        elif sm and text != skel_text:
            r.fail(
                "validator vintage",
                f"validate.py claims vintage {m.group(1)} but its content differs from the "
                "skeleton's — a fix landed in one copy without a stamp bump. Re-copy "
                "templates/pack/skeleton/validate.py verbatim (it is name-agnostic by design).",
            )

    def check_subdomain_form(self, pack: Path, r: Report) -> None:
        """Single-source rule for the co-install form (authoring-a-pack §8): every type a
        subdomain/ schema or host-schema-additions file declares must exist in the pack's
        MAIN schema with the same required fields — the co-install form is DERIVED from the
        standalone schema, and drift between the two forms is a shipped bug."""
        sub = pack / "subdomain"
        if not sub.is_dir():
            r.info("subdomain form", "none shipped (standalone-only pack; see authoring-a-pack §8)")
            return
        main = self.load_yaml(pack / "schema.yaml") or {}
        mtypes = main.get("types") or {}
        # glob-ok: pack subdomain/ dir is flat, not a sharded vault namespace
        for f in sorted(sub.glob("*.yaml")):
            d = self.load_yaml(f)
            if d is None:
                r.fail("subdomain form", f"{f.name}: unparseable")
                continue
            stypes = d.get("types") or {}
            for tname, tdef in stypes.items():
                if tname not in mtypes:
                    r.fail(
                        "subdomain form",
                        f"{f.name}: type '{tname}' not in the main schema — "
                        "the co-install form must be derived, not divergent",
                    )
                else:
                    mreq = set((mtypes[tname] or {}).get("required") or [])
                    sreq = set((tdef or {}).get("required") or [])
                    if mreq != sreq:
                        r.fail(
                            "subdomain form",
                            f"{f.name}: type '{tname}' required-fields drift "
                            f"(main {sorted(mreq)} vs form {sorted(sreq)})",
                        )
            if stypes:
                r.ok("subdomain form", f"{f.name}: {len(stypes)} type(s), all ⊆ main schema")
            # a subdomain schema (the walk-up contract that LANDS) that declares types but
            # no partitioning leaves the installer with no dirs to create and the subtree's
            # namespace guard a NO-OP — found live on the first automated subtree install
            if (
                f.name == "schema.yaml"
                and stypes
                and not ((d.get("partitioning") or {}).get("namespaces"))
            ):
                r.warn(
                    "subdomain form",
                    f"{f.name}: declares types but no "
                    "partitioning.namespaces — the installer creates no "
                    "dirs and the subtree namespace guard won't enforce",
                )
        if (
            not (sub / "INSTALL-ALONGSIDE.md").is_file()
            and not list(sub.glob("INSTALL*.md"))  # glob-ok: flat pack dir
            and not (sub / "README.md").is_file()
        ):
            r.warn(
                "subdomain form", "subdomain/ ships no INSTALL doc — the probes ARE the contract"
            )

    def check_persona(self, pack: Path, r: Report) -> None:
        cp = pack / "CLAUDE.md"
        if not cp.is_file():
            r.fail("CLAUDE.md (persona)", "missing — cron agents read this at $WIKI_PATH/CLAUDE.md")
            return
        txt = cp.read_text(encoding="utf-8", errors="replace")
        if len(txt.strip()) < 80:
            r.fail("CLAUDE.md (persona)", "effectively empty")
            return
        unfilled = [ph for ph in self.placeholders if ph in txt]
        if unfilled:
            r.warn(
                "CLAUDE.md filled in",
                f"{len(unfilled)} scaffold placeholder(s) remain — fill before deploy",
            )
        else:
            r.ok("CLAUDE.md (persona)")

    def check_engine_version(self, pack: Path, r: Report) -> None:
        # Required: the pack must pin the engine release it targets, AND that pin must
        # match the engine running this validator — you validate (and deploy) against
        # one engine, so the pack must declare that one. This is the single coupling
        # that catches engine/pack drift (the okpack-cti engine-v0.1.0 case).
        ev = pack / "engine.version"
        if not ev.is_file():
            r.fail(
                "engine.version", "missing — pin the engine release the pack targets (e.g. v0.2.0)"
            )
            return
        raw = ev.read_text(encoding="utf-8", errors="replace")
        # engine.version is YAML (engine/version/hermes_pin); read the keys when we
        # can, else fall back to grabbing the first vX.Y.Z token.
        ver = hpin = ""
        if self.yaml is not None:
            try:
                data = self.yaml.safe_load(raw)
            except Exception:
                data = None
            if isinstance(data, dict):
                ver = str(data.get("version") or "").strip()
                hpin = str(data.get("hermes_pin") or "").strip()
        if not ver:
            m = self.version_re.search(raw)
            ver = m.group(0) if m else ""
        if not self.version_re.fullmatch(ver):
            r.fail(
                "engine.version",
                f"no vX.Y.Z pin found (got '{ver or raw.strip()[:40]}') — set `version: vX.Y.Z`",
            )
            return

        # Compare against the engine this validator belongs to (single source of truth:
        # engine-manifest.yaml). If unreadable, fall back to the format-only check.
        try:
            meta = self.engine_meta()
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
            r.ok(
                "engine.version", f"{ver} pin · engine {target} — compatible (same release series)"
            )
        else:
            r.fail(
                "engine.version",
                f"pins {ver} but this engine is {target} — different release series. Reconcile the pin: "
                f"`framework upgrade <pack> --apply` (bumps engine.version + runs any migrations under a "
                f"roll-forward gate that auto-rolls-back on failure). deploy.sh does this automatically "
                f"before validating (step [0/6]) unless --no-upgrade; only pin back to {ver} if you truly "
                f"need the older engine.",
            )
        if htag and hpin and hpin != htag:
            r.warn(
                "engine.version hermes_pin", f"pins {hpin} but this engine targets Hermes {htag}"
            )

    def check_feeds(self, pack: Path, r: Report, probe: bool) -> None:
        raw_meta = self.load_yaml(pack / "pack.yaml") or {}
        collection = raw_meta.get("collection") if isinstance(raw_meta, dict) else None
        non_ingest_overlay = (
            isinstance(collection, dict)
            and collection.get("mode") == "overlay"
            and collection.get("feeds") == "none"
        )
        fdir = pack / "feeds"
        # glob-ok: pack feeds/ is a flat dir, not a sharded content namespace
        opmls = sorted(fdir.glob("*.opml")) if fdir.is_dir() else []
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
            found = [
                u for u in (el.attrib.get("xmlUrl", "").strip() for el in root.iter("outline")) if u
            ]
            urls += found
            r.ok(f"feeds/{f.name}", f"{len(found)} feed url(s)")
        if not urls:
            names = ", ".join(f"feeds/{f.name}" for f in opmls)
            if non_ingest_overlay:
                r.info(
                    names,
                    "0 active feed URLs by declared collection contract (analysis overlay; "
                    "host/connectors own collection)",
                )
            else:
                r.warn(
                    names,
                    "0 active feed URLs — pack is deployable but ingest stays idle until you add "
                    "RSS/Atom <outline xmlUrl=…> entries (suggestions in feeds/*.example). Expected for a "
                    "fresh inert pack; ignore until you enable ingest.",
                )
            return
        if probe:
            import urllib.request

            dead = []
            for u in urls:
                try:
                    parsed = urlparse(u)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                        raise ValueError("feed URL must use http(s) and include a host")
                    req = urllib.request.Request(
                        u, method="GET", headers={"User-Agent": "framework-validate/1"}
                    )
                    with urllib.request.urlopen(req, timeout=12) as resp:  # nosec B310
                        if resp.status >= 400:
                            dead.append(f"{u} ({resp.status})")
                except Exception as e:
                    dead.append(f"{u} ({type(e).__name__})")
            if dead:
                r.warn(
                    "feeds reachable",
                    f"{len(dead)}/{len(urls)} unreachable: " + "; ".join(dead[:5]),
                )
            else:
                r.ok("feeds reachable", f"{len(urls)}/{len(urls)} live")
        else:
            r.info("feeds reachable", f"{len(urls)} url(s) not probed (pass --probe-feeds)")

    def check_source_connectors(self, pack: Path, r: Report) -> None:
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
