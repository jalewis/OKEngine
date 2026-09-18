"""Pack extension dependency validation services."""
from __future__ import annotations

from pathlib import Path

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
        at the fail-fast gate (invariant-audit #39). This mirrors framework_extensions._cmd_validate."""
        if not (pack / ".okengine" / "extensions.yaml").is_file():
            return                                  # nothing enabled -> nothing to resolve
        disc = self.discovery()
        try:
            discovered, disc_errors = disc.discover(pack)
            enabled, en_errors = disc.load_enabled_state(pack)
            _, res_errors = disc.resolve_enabled(list(enabled), discovered)
            disabled = (disc._load_disabled(pack)
                        if hasattr(disc, "_load_disabled") else set())
        except Exception as e:                       # discovery faults are reported elsewhere; don't crash
            r.warn("enabled extensions", f"could not resolve enabled set: {e}")
            return
        # disc_errors carries discover()'s Rule-1/Rule-2 faults (notably a cross-tier duplicate id — a
        # HARD FAIL per the discovery spec). resolve_enabled() indexes discovered records into a dict
        # keyed by bare id, so a duplicate is silently last-wins and yields NO res_errors — dropping
        # disc_errors here let `framework validate` / `pull --update` report an ambiguous extension as
        # clean (invariant-audit #351). Fold them into the fail set.
        discovered_ids = {record.get("id") for record in discovered}
        disabled_errors = [
            f"FAIL: disabled extension '{ext_id}' is not discovered in any tier"
            for ext_id in sorted(disabled - discovered_ids)
        ]
        problems = (list(en_errors) + list(res_errors) + list(disc_errors)
                    + disabled_errors)
        if problems:
            for p in problems:
                r.fail("enabled extension", p if not p.startswith("FAIL") else p[5:].strip())
        elif enabled:
            r.ok("enabled extensions", f"{len(enabled)} enabled, all discovered")

    def _inquiry_lib(self, pack: Path):
        """Import inquiry_lib from the DISCOVERED okengine.inquiry record, not a hard-coded
        engine path: the extension may legitimately be supplied at the pack or operator tier,
        and validating against a different copy than the one that will run is exactly the
        baked-vs-staged drift this repo guards elsewhere. Returns None when not enabled."""
        import importlib.util

        disc = self.discovery()
        try:
            discovered, _ = disc.discover(pack)
            enabled, _ = disc.load_enabled_state(pack)
            resolved, _ = disc.resolve_enabled(list(enabled), discovered)
        except Exception:
            return None                             # discovery faults are reported by the check above
        record = resolved.get("okengine.inquiry")
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
        problems = list(contract_errors) + lib.connector_errors(inquiries, connector_ids)
        for problem in problems:
            r.fail("inquiry", problem)
        if not problems:
            open_count = sum(1 for i in inquiries if i.is_open)
            r.ok("inquiry", f"{len(inquiries)} declared ({open_count} open), "
                            "every term set has a reachable connector")
