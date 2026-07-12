#!/usr/bin/env python3
"""framework extensions — discover + inspect + validate a pack's extensions (#134).

The read-only discovery surface over the three tiers (engine / pack / operator).
This is the #134 slice; the mutating enable/disable verbs and cron-regen lifecycle
are #113 (which consumes this scanner).

Usage:
  scripts/framework_extensions.py list     <pack> [--json]
  scripts/framework_extensions.py inspect  <pack> <id>
  scripts/framework_extensions.py validate <pack> [--quiet]

Exit: 0 = no FAILs (WARNs allowed) · 1 = at least one FAIL · 2 = bad invocation.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(modname: str):
    p = _HERE / f"{modname}.py"
    spec = importlib.util.spec_from_file_location(modname, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _discovery():
    return _load("extension_discovery")


def _manifest():
    return _load("extension_manifest")


def _composer():
    return _load("extension_compose")


def _tokens():
    return _load("extension_tokens")


def _require_clean_discovery(extensions, errors, pack) -> list[str]:
    """A broken discovery set (duplicate id, reserved-ns, parse fault) blocks
    enable/disable — you cannot mutate enabled-state on top of an ambiguous tree."""
    return [e if str(e).startswith("FAIL") else f"FAIL: {e}" for e in errors]


def _cmd_list(args) -> int:
    disc = _discovery()
    pack = Path(args.pack).expanduser()
    extensions, errors = disc.discover(pack)
    enabled, en_errors = disc.load_enabled_state(pack)
    errors = errors + en_errors

    if args.json:
        rows = [{"id": e["id"], "tier": e["tier"], "dir": e["dir"],
                 "kind": e["manifest"].get("kind"),
                 "enabled": e["id"] in enabled} for e in extensions]
        print(json.dumps({"extensions": rows, "errors": errors}, indent=2))
        return 1 if errors else 0

    if not extensions:
        print("no extensions discovered "
              f"(engine + {pack}/extensions + {pack}/.okengine/extensions)")
    else:
        print(f"{'ID':<32} {'TIER':<9} {'KIND':<12} {'STATE'}")
        for e in sorted(extensions, key=lambda r: (r["id"], r["tier"])):
            state = "enabled" if e["id"] in enabled else "present"
            print(f"{e['id']:<32} {e['tier']:<9} "
                  f"{str(e['manifest'].get('kind','')):<12} {state}")
    for msg in errors:
        print(msg, file=sys.stderr)
    return 1 if errors else 0


def _cmd_inspect(args) -> int:
    disc = _discovery()
    pack = Path(args.pack).expanduser()
    extensions, errors = disc.discover(pack)
    match = [e for e in extensions if e["id"] == args.id]
    if not match:
        print(f"ERROR: extension '{args.id}' not discovered for pack {pack}", file=sys.stderr)
        for msg in errors:
            print(msg, file=sys.stderr)
        return 1
    e = match[0]
    enabled, _ = disc.load_enabled_state(pack)
    print(f"id:      {e['id']}")
    print(f"tier:    {e['tier']}")
    print(f"dir:     {e['dir']}")
    print(f"enabled: {e['id'] in enabled}")
    if e["id"] in enabled and isinstance(enabled[e["id"]], dict):
        cfg = enabled[e["id"]].get("config")
        if cfg:
            print(f"config:  {json.dumps(cfg)}")
    m = e["manifest"]
    for k in ("kind", "version", "name", "trust", "scope", "description"):
        if k in m:
            print(f"{k+':':<9}{m[k]}")
    m_errors, m_warnings = _manifest().validate_manifest(m)
    for w in m_warnings:
        print(f"WARN: {w}", file=sys.stderr)
    for er in m_errors:
        print(f"FAIL: {er}", file=sys.stderr)
    return 1 if m_errors else 0


def _cmd_validate(args) -> int:
    disc = _discovery()
    em = _manifest()
    pack = Path(args.pack).expanduser()
    fails: list[str] = []
    warns: list[str] = []

    extensions, disc_errors = disc.discover(pack)
    fails.extend(disc_errors)                     # dup-id, reserved-ns, parse/validate faults

    # Per-manifest warnings (errors already folded into disc_errors by the scanner).
    for e in extensions:
        _, m_warnings = em.validate_manifest(e["manifest"])
        for w in m_warnings:
            warns.append(f"{e['id']}: {w}")

    # Enabled-state must resolve against discovery (referenced-but-absent = FAIL).
    enabled, en_errors = disc.load_enabled_state(pack)
    fails.extend(en_errors)
    _, res_errors = disc.resolve_enabled(list(enabled), extensions)
    fails.extend(res_errors)

    if not args.quiet:
        for w in warns:
            print(f"WARN: {w}")
        for f in fails:
            print(f if f.startswith("FAIL") else f"FAIL: {f}")
        if not fails:
            print(f"OK: {len(extensions)} extension(s) discovered, "
                  f"{len(enabled)} enabled, no conflicts")
    return 1 if fails else 0


def _cmd_stage_panels(args) -> int:
    """Compose enabled extensions' reader_panels (okengine#160) -> <pack>/.okengine/
    reader-panels.json ({page_type: binding}) for the reader's type-bound panels. Self-declared
    panels (generated pages with their own `panel:` frontmatter, e.g. viz) don't need this."""
    import json as _json
    disc = _discovery()
    comp = _load("extension_compose")
    pack = Path(args.pack).expanduser()
    extensions, derr = disc.discover(pack)
    eff, eff_err = disc.effective_enabled(pack, extensions)
    resolved, res_err = disc.resolve_enabled(sorted(eff), extensions)
    errs = derr + eff_err + res_err
    if errs:
        for e in errs:
            print(e if str(e).startswith("FAIL") else f"FAIL: {e}", file=sys.stderr)
        return 1
    panels, perr = comp.collect_reader_panels(resolved)
    if perr:
        for e in perr:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    out = pack / ".okengine" / "reader-panels.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_json.dumps(panels, indent=2) + "\n", encoding="utf-8")
    print(f"  staged {len(panels)} reader-panel binding(s) -> {out}")
    return 0


def _seed_about(pack: Path, target: dict) -> None:
    """Copy an extension's ``about.md`` to ``<pack>/wiki/<owned-ns>/_about.md`` for each
    namespace it owns, so the reader shows a description card for the namespace (what it is,
    why, what its pages contain) — even before the first content page exists. The reader
    renders any ``wiki/<ns>/_about.md`` generically; ``_``-prefixed so it stays out of the
    ledger. Idempotent + non-clobbering: writes only when the target is absent, so an
    operator's edits to the description are never overwritten."""
    import yaml
    extdir = Path(target["dir"])
    about = extdir / "about.md"
    if not about.is_file():
        return
    namespaces: list[str] = []
    for sf in (target["manifest"].get("schema") or []):
        try:
            frag = yaml.safe_load((extdir / sf).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        namespaces += list(((frag.get("owns") or {}).get("namespaces") or []))
    text = about.read_text(encoding="utf-8")
    for ns in namespaces:
        dest = pack / "wiki" / ns / "_about.md"
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(f"  seeded namespace description: wiki/{ns}/_about.md")


def _cmd_enable(args) -> int:
    disc = _discovery()
    em = _manifest()
    comp = _composer()
    pack = Path(args.pack).expanduser()

    extensions, disc_errors = disc.discover(pack)
    fails = _require_clean_discovery(extensions, disc_errors, pack)

    target = next((e for e in extensions if e["id"] == args.id), None)
    if target is None:
        print(f"FAIL: extension '{args.id}' not discovered for pack {pack}", file=sys.stderr)
        for f in fails:
            print(f, file=sys.stderr)
        return 1

    # 1. manifest must validate.
    m_errors, m_warnings = em.validate_manifest(target["manifest"])
    fails += [f"{args.id}: {e}" for e in m_errors]

    # 2. declared extension dependencies must be discovered + already enabled.
    enabled, en_err = disc.load_enabled_state(pack)
    fails += en_err
    deps = (target["manifest"].get("requires") or {}).get("extensions") or []
    have = {e["id"] for e in extensions}
    for dep in deps:
        if dep not in have:
            fails.append(f"{args.id}: requires extension '{dep}' which is not discovered")
        elif dep not in enabled:
            fails.append(f"{args.id}: requires extension '{dep}' which is not enabled")

    # 3. trust gate (okengine#124): no OS sandboxing/signing exists yet, so running CODE
    # IN THE GATEWAY (trust: in-gateway, full access, no isolation) is only safe for code
    # whose author you already trust at that level — the engine (first-party) and the pack
    # author (whose crons/persona already run in-gateway). The OPERATOR tier
    # (<pack>/.okengine/extensions/) is the drop-in home for third-party/paid extensions;
    # an in-gateway one there is untrusted code with full gateway reach — refuse unless the
    # operator explicitly accepts it. sidecar (isolated via scoped MCP #132 + own
    # container) and declarative (no code) extensions are allowed for any tier.
    trust = target["manifest"].get("trust")
    if target["tier"] == "operator" and trust == "in-gateway" \
            and not getattr(args, "allow_untrusted", False):
        fails.append(
            f"{args.id}: refusing an operator-tier `in-gateway` extension — it runs "
            "potentially-untrusted third-party code IN the gateway with full access, and OS "
            "sandboxing/signing is not yet available (okengine#124). Repackage as "
            "`trust: sidecar` (isolated, scoped MCP), or pass --allow-untrusted to accept "
            "the risk for code you trust.")

    # 4. dry-run composition with the target added — fail-before-runtime (no write on error).
    # Compose against the EFFECTIVE set (explicit opt-ins ∪ core default-ons), NOT just the explicit
    # opt-ins: write_composed_schema/resolve_for_pack and the whole deploy use the effective set, so a
    # dry-run over the narrower explicit set could pass while the real composition (with a core
    # extension's schema fragment) conflicts — the fail-before-runtime guarantee validating a
    # different composition than ships (invariant-audit #63).
    eff_enabled, eff_err = disc.effective_enabled(pack, extensions)
    fails += eff_err
    want_ids = set(eff_enabled) | {args.id}
    resolved, res_err = disc.resolve_enabled(want_ids, extensions)
    fails += res_err
    _, comp_err, comp_warn = comp.compose(resolved)
    fails += comp_err
    # schema composition must be sound too (okengine#133): a bad/conflicting fragment
    # fails here, before any state change.
    fails += comp.compose_check(pack, resolved)

    for w in m_warnings + comp_warn:
        print(f"WARN: {w}")
    if fails:
        for f in fails:
            print(f if str(f).startswith("FAIL") else f"FAIL: {f}", file=sys.stderr)
        print("not enabled (validation failed before any state change)", file=sys.stderr)
        return 1

    # Seed the namespace description page(s) so the reader self-describes this extension (even
    # before its first content page). Idempotent + non-clobbering — runs on the already-enabled
    # path too, so a deployment enabled before the extension shipped an about.md picks it up on
    # the next enable.
    _seed_about(pack, target)

    if args.id in enabled:
        print(f"already enabled: {args.id}")
        return 0
    errs = disc.set_enabled(pack, args.id, True)
    if errs:
        for e in errs:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    # Mint a scoped MCP token from the manifest capabilities (okengine#132). The
    # store (sha256 + scopes) is what the MCP servers enforce; the plaintext goes to
    # the gitignored secrets file for a sidecar's env injection (okengine#135).
    tok = _tokens()
    read_scopes, write_scopes = tok.scopes_from_manifest(target["manifest"])
    tok.mint(pack, args.id, read_scopes, write_scopes)
    serr = comp.write_composed_schema(pack)        # regenerate composed-schema.yaml (#133)
    for e in serr:
        print(f"WARN: composed-schema regen: {e}", file=sys.stderr)
    print(f"enabled: {args.id}")
    print("redeploy to apply — regen folds its cron job into cron-plus-jobs.json "
          "(generated-from-source); scoped MCP token minted; composed schema regenerated.")
    return 0


def _cmd_stage_plan(args) -> int:
    """Machine-readable staging plan for deploy-cron-scripts.sh: one
    `<id>\\t<src-dir>` line per enabled in-gateway extension whose scripts must be
    staged into <SCRIPTS_ROOT>/<id>/. Errors go to stderr + exit 1 (deploy gate)."""
    comp = _composer()
    pack = Path(args.pack).expanduser()
    targets, errors = comp.staging_targets(pack)
    for t in targets:
        print(f"{t['id']}\t{t['dir']}")
    if not errors:
        # Record the EFFECTIVE set (opt-ins + core default-ons) as a GENERATED
        # artifact the reader/cockpit About panel reads. The enabled-state file
        # alone under-reports: it lists opt-ins only, so core extensions were
        # invisible to About (found live: a fleet running 3 showed 1).
        try:
            recs, _ = comp.effective_records(pack)  # broader than targets: sidecar/UI-only too
            okd = pack / ".okengine"
            okd.mkdir(exist_ok=True)
            import yaml as _y
            (okd / "extensions-effective.yaml").write_text(
                "# GENERATED by `framework extensions stage-plan` (deploy-cron-scripts) —\n"
                "# the EFFECTIVE extension set (opt-ins + core default-ons) with manifest\n"
                "# identity for the About panel. Do not hand-edit; change state via\n"
                "# `framework extensions enable|disable`.\n"
                + _y.safe_dump({"effective": recs}, sort_keys=False, allow_unicode=True),
                encoding="utf-8")
        except OSError as e:
            print(f"WARN: could not record extensions-effective.yaml ({e})", file=sys.stderr)
    for e in errors:
        print(f"FAIL: {e}" if not str(e).startswith("FAIL") else e, file=sys.stderr)
    return 1 if errors else 0


def _cmd_purge(args) -> int:
    """Delete the pages an extension produced, by provenance stamp (okengine#127).
    Destructive — the extension must be DISABLED first, and it is dry-run unless --yes.
    Disable preserves pages; purge is the separate explicit removal."""
    disc = _discovery()
    comp = _composer()
    pack = Path(args.pack).expanduser()
    enabled, en_err = disc.load_enabled_state(pack)
    if en_err:
        for e in en_err:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    if args.id in enabled:
        print(f"FAIL: {args.id} is still enabled — `extensions disable {args.id}` before purge",
              file=sys.stderr)
        return 1
    targets = comp.purge_targets(pack, args.id)
    if not targets:
        print(f"no pages stamped extension_id={args.id} — nothing to purge")
        return 0
    if not args.yes:
        print(f"would purge {len(targets)} page(s) owned by {args.id} "
              f"(re-run with --yes to delete):")
        for t in targets:
            print(f"  wiki/{t}")
        return 0
    n = 0
    for t in targets:
        try:
            (pack / "wiki" / t).unlink()
            n += 1
        except OSError as e:
            print(f"WARN: {t}: {e}", file=sys.stderr)
    print(f"purged {n} page(s) owned by {args.id}")
    return 0


def _cmd_sidecar_generate(args) -> int:
    """Materialize the deploy artifacts for enabled sidecar extensions (okengine#135):
    <pack>/.okengine/generated/sidecars.compose.yml (a compose override with each
    sidecar service + injected scoped token/MCP env) and a per-extension trigger.sh.
    The override holds tokens -> written 0600. A no-op when no sidecars are enabled."""
    import yaml
    comp = _composer()
    pack = Path(args.pack).expanduser()
    override, wrappers, errors = comp.sidecar_compose_override(pack)
    if errors:
        for e in errors:
            print(f"FAIL: {e}" if not str(e).startswith("FAIL") else e, file=sys.stderr)
        return 1
    if not override["services"]:
        print("no enabled sidecar extensions")
        return 0
    gen = pack / ".okengine" / "generated"
    gen.mkdir(parents=True, exist_ok=True)
    compose_file = gen / "sidecars.compose.yml"
    compose_file.write_text(yaml.safe_dump(override, sort_keys=False), encoding="utf-8")
    compose_file.chmod(0o600)                      # holds injected tokens
    for ext_id, text in wrappers.items():
        d = gen / ext_id
        d.mkdir(parents=True, exist_ok=True)
        wf = d / comp.TRIGGER_NAME
        wf.write_text(text, encoding="utf-8")
        wf.chmod(0o755)
    print(f"generated {len(override['services'])} sidecar service(s) + wrapper(s) -> {gen}")
    return 0


def _cmd_disable(args) -> int:
    disc = _discovery()
    pack = Path(args.pack).expanduser()
    enabled, en_err = disc.load_enabled_state(pack)
    if en_err:
        for e in en_err:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    if args.id not in enabled:
        print(f"not enabled: {args.id} (nothing to do)")
        return 0
    errs = disc.set_enabled(pack, args.id, False)
    if errs:
        for e in errs:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    _tokens().revoke(pack, args.id)            # revoke the scoped MCP token (okengine#132)
    serr = _composer().write_composed_schema(pack)    # regenerate composed schema without it (#133)
    if serr:
        # write_composed_schema writes NOTHING on error, so the artifact still carries the
        # just-disabled extension's owned types/namespaces — reporting clean success here would leave
        # the enforced write path governed by a stale schema (invariant-audit #38). The canonical
        # trigger is ANOTHER enabled extension left unresolvable by an engine/pack update; surface it.
        for e in serr:
            print(f"FAIL: composed-schema regen: {e}", file=sys.stderr)
        print(f"disabled: {args.id} in state, but the composed schema was NOT regenerated (it still "
              f"reflects the old enabled set). Resolve the errors above and re-run "
              f"`framework extensions disable`/`enable` (or fix the offending extension), then "
              f"redeploy.", file=sys.stderr)
        return 1
    print(f"disabled: {args.id}")
    print("redeploy to apply — its cron job drops from the generated fleet. Pages it "
          "wrote are PRESERVED (orphaned); use `extensions purge` (#127) to remove them.")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="framework extensions", add_help=True,
                                 description="Discover/inspect/validate pack extensions.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list discovered extensions + enabled state")
    p_list.add_argument("pack")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_insp = sub.add_parser("inspect", help="show one extension's manifest")
    p_insp.add_argument("pack")
    p_insp.add_argument("id")
    p_insp.set_defaults(func=_cmd_inspect)

    p_val = sub.add_parser("validate", help="validate discovery + enabled-state")
    p_val.add_argument("pack")
    p_val.add_argument("--quiet", action="store_true")
    p_val.set_defaults(func=_cmd_validate)

    p_en = sub.add_parser("enable", help="enable an extension (validates, then writes state)")
    p_en.add_argument("pack")
    p_en.add_argument("id")
    p_en.add_argument("--allow-untrusted", action="store_true",
                      help="accept the risk of a non-first-party in-gateway extension (okengine#124)")
    p_en.set_defaults(func=_cmd_enable)

    p_dis = sub.add_parser("disable", help="disable an extension (preserves its pages)")
    p_dis.add_argument("pack")
    p_dis.add_argument("id")
    p_dis.set_defaults(func=_cmd_disable)

    p_stage = sub.add_parser("stage-plan",
                             help="print <id>\\t<dir> staging plan (for deploy)")
    p_stage.add_argument("pack")
    p_stage.set_defaults(func=_cmd_stage_plan)

    p_panels = sub.add_parser("stage-panels",
                              help="compose enabled reader_panels -> <pack>/.okengine/reader-panels.json")
    p_panels.add_argument("pack")
    p_panels.set_defaults(func=_cmd_stage_panels)

    p_side = sub.add_parser("sidecar-generate",
                            help="write compose override + trigger wrappers for sidecars")
    p_side.add_argument("pack")
    p_side.set_defaults(func=_cmd_sidecar_generate)

    p_purge = sub.add_parser("purge",
                             help="delete a disabled extension's produced pages (destructive)")
    p_purge.add_argument("pack")
    p_purge.add_argument("id")
    p_purge.add_argument("--yes", action="store_true", help="actually delete (default: dry-run)")
    p_purge.set_defaults(func=_cmd_purge)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
