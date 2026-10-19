"""Pack identity and composition validation services."""
from __future__ import annotations

from pathlib import Path

from scripts.framework_validate_report import Report


class MetadataChecks:
    def __init__(self, *, engine_root: Path, load_yaml, pack_meta):
        self.engine_root = engine_root
        self.load_yaml = load_yaml
        self.pack_meta = pack_meta

    def check_pack_meta(self, pack: Path, r: Report) -> None:
        """Shape-check the pack's pack.yaml (identity + composition metadata). Full
        cross-pack composition validation happens at deploy across all installed
        packs; here we only validate this pack's own declaration."""
        if not (pack / "pack.yaml").is_file():
            r.warn("pack.yaml", "absent — recommended for composition (name/version/owns/requires)")
            return
        try:
            meta = self.pack_meta().load_pack_meta(pack)
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
        # The write servers read the literal `name` (okengine.pack_principal), never the directory
        # basename load_pack_meta defaults to: a nameless pack.yaml validated clean here while every
        # write server refused to start after the recreate (okengine#1027).
        from okengine.pack_principal import declared_pack
        if not declared_pack(pack):
            r.fail("pack.yaml name", "no `name`: every write server refuses to start without the "
                   "pack it writes for (okengine#1027). Add `name: <pack>` to pack.yaml.")
        r.ok("pack.yaml", f"{meta['name']} v{meta['version']} "
             f"({meta['trust']}; owns {len(meta['owns_types'])} type(s), "
             f"{len(meta['owns_namespaces'])} namespace(s))")
        if meta.get("port_offset"):
            r.info("pack.yaml port_offset", f"{meta['port_offset']} (reader {9200 + meta['port_offset']}, "
                   f"mcp {8730 + meta['port_offset']} — applied by framework pull)")
        # description/mission feed the reader/cockpit ABOUT panel, the catalog blurb and
        # `framework list` — one declaration, three surfaces (multi-surface rule). Read
        # raw: load_pack_meta normalizes composition keys only.
        raw = self.load_yaml(pack / "pack.yaml") or {}
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


    def check_owns_covers_schema(self, pack: Path, r: Report) -> None:
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
            meta = self.pack_meta().load_pack_meta(pack)
        except Exception:
            return                                   # a load fault is reported by check_pack_meta
        if meta is None or meta.get("kind") == "bundle":
            return                                   # a bundle owns nothing BY DESIGN (#181)
        sch = self.load_yaml(pack / "schema.yaml")
        if not isinstance(sch, dict):
            return                                   # parse fault flagged by check_schema
        base = self.load_yaml(self.engine_root / "config" / "base-schema.yaml") or {}
        base_types = set(base.get("types") or {}) if isinstance(base, dict) else set()
        base_ns = set((base.get("partitioning") or {}).get("namespaces") or {})
        schema_types = set(sch.get("types") or {}) if isinstance(sch.get("types"), dict) else set()
        schema_ns = set((sch.get("partitioning") or {}).get("namespaces") or {})
        supported_reshard = {"day", "second-letter", "not-applicable", None}
        for ns, cfg in ((sch.get("partitioning") or {}).get("namespaces") or {}).items():
            if not isinstance(cfg, dict):
                continue
            value = cfg.get("reshard_by")
            if value not in supported_reshard:
                r.fail(
                    "schema.yaml reshard_by",
                    f"namespace '{ns}' declares unsupported reshard_by '{value}' — supported "
                    "values are day, second-letter, and not-applicable",
                )
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
        # A COMPOSED host declares its guests' types and namespaces because install-domain merged
        # them in; they are owned, just not by this pack. Two records already say so:
        # .okengine/installed-domains/<pack>.json (owned_namespaces, written since #270) and
        # composed-schema.yaml's per-type `owner:`. Without consulting them, the only way to
        # silence this warning is to add a guest's type to the HOST's owns.types — a false claim,
        # and exactly the double-declared ownership okengine#811 found on a live deployment, where
        # the host claimed 6 of a guest's 8 types and no gate noticed (okengine#816).
        guest_types, guest_ns = _installed_domain_ownership(pack, sch)
        contested = (owns_types & guest_types) | (owns_ns & guest_ns)
        if contested:
            r.warn("pack.yaml owns vs installed domains",
                   f"this pack's owns and an installed domain's BOTH claim: {sorted(contested)} — "
                   f"composed packs must own disjointly; relinquish them here or transfer "
                   f"ownership deliberately (okengine#811)")
        owns_types = owns_types | guest_types
        owns_ns = owns_ns | guest_ns
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


    def _is_bundle(self, pack: Path) -> bool:
        """True iff the pack declares `kind: bundle` (owns nothing; composes other packs)."""
        if not (pack / "pack.yaml").is_file():
            return False
        try:
            meta = self.pack_meta().load_pack_meta(pack)
        except Exception:
            return False
        return bool(meta and meta.get("kind") == "bundle")


    def check_bundle(self, pack: Path, r: Report) -> None:
        """Validate a `kind: bundle` pack's recipe (okengine#181): owns-nothing, a `host` base
        pack, a non-empty `compose` list (host not in it, no self/dupes), and every recipe member
        declared in `requires`. A bundle ships no schema/persona/crons/feeds/wiki, so the
        domain-content checks are skipped for it (see validate())."""
        try:
            meta = self.pack_meta().load_pack_meta(pack)
        except Exception as e:
            r.fail("bundle recipe", f"could not load pack.yaml: {str(e)[:120]}")
            return
        errs = self.pack_meta().validate_bundle_recipe(meta or {})
        if errs:
            for e in errs:
                r.fail("bundle recipe", e)
        else:
            r.ok("bundle recipe",
                 f"host {meta['bundle_host']} + composes {len(meta['bundle_compose'])} pack(s): "
                 f"{', '.join(meta['bundle_compose'])}")


def _installed_domain_ownership(pack: Path, schema: dict) -> "tuple[set, set]":
    """(types, namespaces) owned by packs that install-domain has composed into this host.

    Namespaces come from each `.okengine/installed-domains/<pack>.json` (`owned_namespaces`,
    written since #270); types from the composed schema's per-type `owner:`, which install-domain
    stamps. A host with neither record yields empty sets, so a standalone pack behaves exactly as
    before this existed.
    """
    import json as _json

    base = pack / ".okengine" / "installed-domains"
    installed, namespaces = set(), set()
    for path in sorted(base.glob("*.json")) if base.is_dir() else []:  # glob-ok: flat manifest dir
        try:
            manifest = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        installed.add(str(manifest.get("pack") or path.stem))
        owned = manifest.get("owned_namespaces")
        if isinstance(owned, (dict, list)):
            namespaces |= {str(n) for n in owned}
    if not installed:
        return set(), set()
    types = {str(name) for name, spec in (schema.get("types") or {}).items()
             if isinstance(spec, dict) and str(spec.get("owner") or "") in installed}
    return types, namespaces
