"""Pack extension dependency validation services."""
from __future__ import annotations

from pathlib import Path

from scripts.framework_validate_pack import check_fragment_type_roles
from scripts.framework_validate_report import Report


class ExtensionChecks:
    def __init__(self, *, load_yaml, pack_meta, discovery):
        self.load_yaml = load_yaml
        self.pack_meta = pack_meta
        self.discovery = discovery

    def _schema_ext_owners(self, pack: Path) -> set[str]:
        """Extension ids hard-referenced as owners in the pack's schema.yaml ``owners:`` map
        (grammar: engine | pack:<name> | ext:<id>) — implicit pack->extension deps (#142 D)."""
        sp = pack / "schema.yaml"
        if not sp.is_file():
            return set()
        data = self.load_yaml(sp) or {}
        owners = data.get("owners") if isinstance(data, dict) else None
        out: set[str] = set()
        for grp in ("types", "fields"):
            for owner in ((owners or {}).get(grp) or {}).values():
                if isinstance(owner, str) and owner.startswith("ext:"):
                    out.add(owner[len("ext:"):])
        return out


    def check_extension_requirements(self, pack: Path, r: Report) -> None:
        """okengine#142 (A+D): a pack can require an extension (`requires: [ext:<id>@>=ver]`),
        and a pack schema can annotate an `ext:<id>` owner. Both must resolve to an ENABLED
        extension (explicit or core-default-on) at the version floor — fail-loud BEFORE deploy
        rather than degrade silently at runtime when the operator didn't enable it."""
        pm = self.pack_meta()
        meta = None
        if (pack / "pack.yaml").is_file():
            try:
                meta = pm.load_pack_meta(pack)
            except Exception:
                return                              # pack.yaml errors already reported above
        ext_reqs = pm.extension_requires(meta) if meta else []
        schema_owners = self._schema_ext_owners(pack)
        if not ext_reqs and not schema_owners:
            return                                  # no declared pack->extension coupling

        disc = self.discovery()
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


    def check_enabled_extensions_resolve(self, pack: Path, r: Report) -> None:
        """Every id in <pack>/.okengine/extensions.yaml `enabled:` must still be DISCOVERED — an enabled
        id that no longer resolves (e.g. an engine upgrade renamed a tier-1 extension the operator had
        enabled) otherwise slips past `framework validate` and first hard-stops at deploy.sh step 5, AFTER
        step 4 already recreated every container (with --no-crons, never at all). check_extension_requirements
        only covers ids the pack DECLARES via requires:/schema-owner, so an enabled-only id was unguarded
        at the fail-fast gate (invariant-audit #39). This mirrors framework_extensions._cmd_validate.

        Discovery itself runs even with NO enabled map (okengine#991): resolve_for_pack, which the
        deploy's stage-plan calls, reports discover()'s faults (reserved okengine.* id squatted by a
        pack/operator tier, cross-tier duplicate id, broken manifest) whatever is enabled, so gating
        them on extensions.yaml let a fresh pack validate green and dead-end at deploy step 5."""
        disc = self.discovery()
        try:
            discovered, disc_errors = disc.discover(pack)
            if (pack / ".okengine" / "extensions.yaml").is_file():
                enabled, en_errors = disc.load_enabled_state(pack)
                _, res_errors = disc.resolve_enabled(list(enabled), discovered)
                disabled_errors = disc.stale_disabled_errors(pack, discovered)
            else:                                   # nothing enabled -> only discovery can fault
                enabled, en_errors, res_errors, disabled_errors = {}, [], [], []
        except Exception as e:                       # discovery faults are reported elsewhere; don't crash
            r.warn("enabled extensions", f"could not resolve enabled set: {e}")
            return
        # disc_errors carries discover()'s Rule-1/Rule-2 faults (notably a cross-tier duplicate id — a
        # HARD FAIL per the discovery spec). resolve_enabled() indexes discovered records into a dict
        # keyed by bare id, so a duplicate is silently last-wins and yields NO res_errors — dropping
        # disc_errors here let `framework validate` / `pull --update` report an ambiguous extension as
        # clean (invariant-audit #351). Fold them into the fail set.
        # A `disabled:` id no tier discovers (okengine#1042 [32]): `extensions validate` FAILs it
        # too, and the line names `extensions forget`, the CLI path that clears it.
        # The discovered fragments' owned-type roles (okengine#1032 [24]), on the one discovery.
        check_fragment_type_roles(discovered, self.load_yaml, r)
        problems = (list(en_errors) + list(res_errors) + list(disc_errors)
                    + disabled_errors)
        if problems:
            for p in problems:
                r.fail("enabled extension", p if not p.startswith("FAIL") else p[5:].strip())
        elif enabled:
            r.ok("enabled extensions", f"{len(enabled)} enabled, all discovered")
        # An enabled extension must also be one the deploy will load (okengine#994 [14]).
        self.check_operator_trust_acceptance(pack, r)

    def check_operator_trust_acceptance(self, pack: Path, r: Report) -> None:
        """okengine#994 [14]: an enabled operator-tier `trust: in-gateway` extension must carry a
        recorded trust acceptance. Regen and script staging refuse one without it, so this is the
        earliest gate: deploy step 1 FAILs naming the one-time accept command, before step 4
        recreates any container, instead of the lane silently vanishing at step 5."""
        disc = self.discovery()
        resolved = self._effective_extensions(pack)
        try:
            needing = [ext_id for ext_id in sorted(resolved)
                       if disc.requires_trust_acceptance(resolved[ext_id])]
            if not needing:
                return                              # nothing operator-tier in-gateway is enabled
            blocked = disc.unaccepted_trust(pack, resolved)
        except Exception as e:                      # a discovery fault is reported by the check above
            r.warn("operator trust", f"could not check operator trust acceptance: {e}")
            return
        for ext_id, error in blocked.items():
            r.fail(f"operator trust {ext_id}", error.removeprefix("FAIL:").strip())
        accepted = [ext_id for ext_id in needing if ext_id not in blocked]
        if accepted:
            r.ok("operator trust", f"{len(accepted)} operator in-gateway extension(s) accepted")

    def _effective_extensions(self, pack: Path) -> dict:
        """id -> record for the EFFECTIVE set (explicit opt-ins ∪ core default-ons), the set the
        deploy actually runs. The explicit set alone is wrong for a core extension: it is active in
        every pack that never listed it, and a gate that consults only the explicit list skips it
        there entirely — validating a different composition than ships (invariant-audit #63).
        Empty on a discovery fault; those are reported by the check above."""
        try:
            resolved, _ = self.discovery().resolve_for_pack(pack)
        except Exception:
            return {}
        return resolved

    def _inquiry_lib(self, pack: Path):
        """Import inquiry_lib from the DISCOVERED okengine.inquiry record, not a hard-coded
        engine path: the extension may legitimately be supplied at the pack or operator tier,
        and validating against a different copy than the one that will run is exactly the
        baked-vs-staged drift this repo guards elsewhere. Returns None when not enabled."""
        import importlib.util

        record = self._effective_extensions(pack).get("okengine.inquiry")
        if not record:
            return None
        lib_path = Path(record.get("dir") or "") / "inquiry_lib.py"
        if not lib_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("okengine_inquiry_lib", lib_path)
        module = importlib.util.module_from_spec(spec)
        # Register BEFORE exec: the module defines dataclasses, and dataclass field resolution
        # looks the defining module up in sys.modules by __module__. An unregistered module
        # makes that lookup return None and the class construction raise.
        import sys as _sys
        _sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def check_inquiries(self, pack: Path, r: Report) -> None:
        """okengine#746: a declared research topic must have reachable INGRESS.

        The schema fragment already makes `question`/`status`/`terms` required, so the write
        path rejects a structurally incomplete inquiry. What a schema cannot express is whether
        the connector an inquiry names actually exists in this deployment. That gap fails
        SILENTLY at runtime — the collect lane skips the inquiry, the dossier renders empty, and
        an operator reads the empty dossier as "nothing is happening in this field" rather than
        "I misspelled the connector". Catch it at the earliest gate instead.
        """
        lib = self._inquiry_lib(pack)
        if lib is None:
            return                                  # extension not enabled -> nothing to check
        wiki = pack / "wiki"
        if not (wiki / lib.NS).is_dir():
            return                                  # enabled but no inquiries declared yet
        inquiries, contract_errors = lib.load_inquiries(wiki)
        if not inquiries:
            return
        connector_ids: set[str] = set()
        connector_dir = pack / "connectors"
        if connector_dir.is_dir():
            # glob-ok: a flat pack configuration directory, never a content namespace.
            for path in sorted((*connector_dir.glob("*.yaml"), *connector_dir.glob("*.yml"))):
                data = self.load_yaml(path) or {}
                cid = str(data.get("id") or "") if isinstance(data, dict) else ""
                if cid:
                    connector_ids.add(cid)
        problems = (list(contract_errors) + lib.connector_errors(inquiries, connector_ids)
                    + lib.field_errors(inquiries, wiki))
        for problem in problems:
            r.fail("inquiry", problem)
        if not problems:
            open_count = sum(1 for i in inquiries if i.is_open)
            r.ok("inquiry", f"{len(inquiries)} declared ({open_count} open), "
                            "every term set has a reachable connector")
        # inquiry-analyze answers standing questions as okengine.assessments pages, and the two
        # extensions are enabled independently. Without assessments the lane has no type to write,
        # so a declared question would never be answered while collection looks healthy. WARN,
        # not FAIL: collection and the dossier are useful on their own (okengine#854).
        # Declared lacuna fields with no lacuna to consume them are a declaration nothing reads.
        if any(i.is_open and i.fields for i in inquiries) and \
                "okengine.lacuna" not in self._effective_extensions(pack):
            r.warn("inquiry", "open inquiries declare `fields` but okengine.lacuna is not enabled — "
                              "nothing maps them. `framework extensions enable <pack> okengine.lacuna`")
        if any(i.is_open for i in inquiries) and \
                "okengine.assessments" not in self._effective_extensions(pack):
            r.warn("inquiry", "open inquiries declared but okengine.assessments is not enabled — "
                              "inquiry-analyze cannot answer their standing questions (collection "
                              "and the dossier still run). `framework extensions enable <pack> "
                              "okengine.assessments`")
