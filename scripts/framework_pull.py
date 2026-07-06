#!/usr/bin/env python3
"""framework pull — fetch an existing pack definition from a repo or the catalog.

The counterpart to `framework init` (which scaffolds a NEW pack from the skeleton):
`pull` fetches a PUBLISHED pack definition, strips any runtime, checks the
engine-version pin, validates it, and leaves it **inert** — it never deploys or
enables anything.

Usage:
  framework pull <source> [dest] [--into DIR] [--ref REF] [--force] [--update]
                 [--no-validate] [--catalog URL|PATH] [--port-offset N]

  --update re-fetches into an EXISTING pack dir without clobbering your config:
  runtime + content (.env, .hermes-data/, raw/, wiki/) are untouched, new upstream
  files are added, and changed definition files (schema.yaml, CLAUDE.md, crons, …)
  are written as `<file>.upstream` next to yours for a manual diff/merge.

  <source> forms:
    okpack-foo                    a catalog name (resolved via catalog.json)
    okpacks-library:okpack-foo    the okpacks-library monorepo subdir packs/okpack-foo
    owner/repo                    a standalone pack repo
    owner/repo:packs/okpack-foo   a subdir of any repo
    https://… / git@…             an explicit git URL (whole repo)

Env: OKENGINE_CATALOG (default catalog URL/path), OKENGINE_LIBRARY (default
     jalewis/okpacks-library), OKENGINE_GIT_SSH=1 (use git@github.com for owner/repo).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = (os.environ.get("OKENGINE_CATALOG")
                   or "https://raw.githubusercontent.com/jalewis/okpacks-library/main/catalog.json")
LIBRARY_REPO = os.environ.get("OKENGINE_LIBRARY") or "jalewis/okpacks-library"

CATALOG_HELP = (
    "  fallbacks:\n"
    "    - private/dev catalog: --catalog /path/to/catalog.json  (or export OKENGINE_CATALOG=…)\n"
    "    - skip the catalog, pull a pack directly: framework pull owner/repo[:packs/<pack>]\n"
    "    - a PRIVATE GitHub catalog isn't served at the raw URL — clone the catalog repo and\n"
    "      point --catalog at the local catalog.json")


def _engine_release_from_manifest() -> str:
    """engine-manifest.yaml's engine_release — the authoritative version, always present (even on a
    no-history public snapshot where there's no git tag)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import engine_meta
        return (engine_meta.engine_release() or "").strip()
    except Exception:
        return ""


def engine_version() -> str:
    """Engine release for the pin check. Prefer a git tag (precise on a tagged checkout), then fall
    back to engine-manifest.yaml's engine_release. A no-history public snapshot has no tags, so
    git describe yields nothing there — without the manifest fallback the engine would report
    v0.0.0 and spuriously fail/warn every pack's engine.version pin (okengine#96)."""
    try:
        out = subprocess.run(["git", "-C", str(ENGINE_ROOT), "describe", "--tags",
                              "--match", "v*", "--abbrev=0"],
                             capture_output=True, text=True, timeout=10)
        tag = (out.stdout or "").strip()
        if tag:
            return tag
    except Exception:
        pass
    return _engine_release_from_manifest() or "v0.0.0"


def read_catalog(src: str = DEFAULT_CATALOG) -> tuple[dict | None, str | None]:
    """Load catalog.json from a URL or a local path. Returns (catalog, None) on
    success, (None, diagnostic) on failure — the diagnostic distinguishes
    network / HTTP status / JSON / schema / missing-file so the operator knows
    what actually failed (issue #8)."""
    try:
        if src.startswith(("http://", "https://")):
            with urllib.request.urlopen(src, timeout=15) as r:   # noqa: S310
                raw = r.read().decode("utf-8")
        else:
            p = Path(src).expanduser()
            if not p.is_file():
                return None, f"catalog file not found: {p}"
            raw = p.read_text(encoding="utf-8")
    except urllib.error.HTTPError as e:
        hint = {403: "forbidden — the catalog repo may be private",
                404: "not found — wrong path, or a private/renamed repo"}.get(e.code, f"HTTP {e.code}")
        return None, f"HTTP {e.code} reading {src} ({hint})"
    except urllib.error.URLError as e:
        return None, f"cannot reach {src} — network/DNS error: {e.reason}"
    except OSError as e:
        return None, f"cannot read {src}: {e}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"catalog at {src} is not valid JSON: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("packs"), list):
        return None, f"catalog at {src} has the wrong shape (expected an object with a 'packs' array)"
    return data, None


def _giturl(owner_repo: str) -> str:
    # a LOCAL checkout is a first-class source (OKENGINE_LIBRARY=/path/to/library,
    # or a catalog whose `repo` is a path): pre-publish testing must pull the pack
    # content ACTUALLY UNDER TEST — pulling by name previously always cloned the
    # public GitHub repo even with a local catalog, so a releases-stale snapshot
    # is what got tested (the exact mismatch external users hit when publishes lag).
    p = Path(owner_repo).expanduser()
    if p.is_dir():
        return str(p.resolve())            # git clone handles plain local paths
    if os.environ.get("OKENGINE_GIT_SSH") == "1":
        return f"git@github.com:{owner_repo}.git"
    return f"https://github.com/{owner_repo}.git"


def resolve(source: str, catalog: dict | None, catalog_err: str | None = None) -> tuple[dict, bool]:
    """Resolve <source> -> ({name, giturl, subdir, ref}, curated). `curated` is
    True only when it came from the catalog."""
    # 1. explicit git URL -> whole repo
    if source.startswith(("http://", "https://", "git@", "ssh://")):
        name = source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        return {"name": name, "giturl": source, "subdir": "", "ref": None}, False
    # 2. okpacks-library:<pack>  (the library shorthand)
    if source.startswith(("okpacks-library:", "okpacks:")):
        pack = source.split(":", 1)[1]
        return {"name": pack, "giturl": _giturl(LIBRARY_REPO),
                "subdir": f"packs/{pack}", "ref": None}, False
    # 3. owner/repo[:subdir]
    base = source.split(":", 1)[0]
    if "/" in base:
        owner_repo, _, subdir = source.partition(":")
        name = (subdir.rstrip("/").rsplit("/", 1)[-1] if subdir
                else owner_repo.rsplit("/", 1)[-1])
        return {"name": name, "giturl": _giturl(owner_repo),
                "subdir": subdir, "ref": None}, False
    # 4. a bare catalog name
    for p in ((catalog or {}).get("packs") or []):
        if p.get("name") == source:
            return {"name": source, "giturl": _giturl(p["repo"]),
                    "subdir": p.get("subdir") or "", "ref": p.get("ref")}, True
    if catalog is None:
        raise SystemExit(f"ERROR: cannot resolve '{source}' — the catalog could not be read:\n"
                         f"  {catalog_err}\n{CATALOG_HELP}")
    names = ", ".join(p.get("name", "?") for p in (catalog.get("packs") or [])) or "(none)"
    raise SystemExit(f"ERROR: '{source}' is not in the catalog (have: {names}).\n"
                     f"  Use owner/repo (or owner/repo:subdir) for an arbitrary pack.")


def _git(args: list[str]) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


def fetch(spec: dict, dest: Path, force: bool) -> None:
    if dest.exists() and any(dest.iterdir()) and not force:
        raise SystemExit(f"ERROR: {dest} exists and is not empty — pass --force to overwrite.")
    ref = spec["ref"]
    branch = ["--branch", ref] if ref else []
    try:
        if not spec["subdir"]:
            if dest.exists():
                shutil.rmtree(dest)
            _git(["clone", "--depth", "1", *branch, spec["giturl"], str(dest)])
        else:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td) / "repo"
                _git(["clone", "--depth", "1", *branch, spec["giturl"], str(work)])
                src = work / spec["subdir"]
                if not src.is_dir():
                    raise SystemExit(f"ERROR: subdir '{spec['subdir']}' not found in {spec['giturl']}")
                shutil.copytree(src, dest, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or "").strip().splitlines()[-1:] or [""]
        raise SystemExit(f"ERROR: git clone failed — {msg[0]}\n"
                         f"  (private repo? authenticate via gh/ssh, or set OKENGINE_GIT_SSH=1)")
    # strip runtime so what lands is a clean DEFINITION
    for junk in (".hermes-data", ".env", "raw"):
        p = dest / junk
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    for pc in dest.rglob("__pycache__"):
        shutil.rmtree(pc, ignore_errors=True)


def _layer_runtime(dest: Path) -> None:
    """Seed a fresh runtime config (the deploy-time bits a committed definition
    omits), so the pulled pack is deploy-ready — same as `framework init`. Any
    committed .hermes-data was already stripped above (don't trust shipped runtime)."""
    (dest / ".hermes-data" / "qmd").mkdir(parents=True, exist_ok=True)
    (dest / ".hermes-data" / ".gitkeep").write_text("", encoding="utf-8")
    tmpl = ENGINE_ROOT / "config" / "config.yaml.template"
    if tmpl.is_file():
        shutil.copy(tmpl, dest / ".hermes-data" / "config.yaml")


def _apply_port_offset(dest: Path, offset: int) -> None:
    """Shift the pulled pack's published host ports by `offset` so it doesn't
    collide with another stack on the host. 9200 (reader) / 8730 (mcp) are the
    fixed container ports; only the published host port and the gateway's MCP url
    move. Idempotent for an already-offset pack (we rewrite by the fixed container
    port, not the current host port)."""
    if not offset:
        return
    rport, mport = 9200 + offset, 8730 + offset
    compose = dest / "docker-compose.yml"
    if compose.is_file():
        t = compose.read_text(encoding="utf-8")

        def _seq(base):
            # SEQUENTIAL host ports per container port: several services publish
            # container 9200 (reader + cockpit since v0.8.0) — collapsing them all
            # onto base+offset made both bind the same host port, and compose died
            # mid-up with half a stack running (found by the deploy-matrix live
            # tier). File order is stable, so this stays idempotent on re-pull.
            n = [0]
            def repl(m):
                port = base + offset + n[0]
                n[0] += 1
                return f":{port}:{base}"
            return repl
        t = re.sub(r":\d+:9200\b", _seq(9200), t)   # reader/cockpit host:container
        t = re.sub(r":\d+:8730\b", _seq(8730), t)   # mcp host:container
        # container_name must be instance-unique too: a pinned name makes the pack
        # single-instance-per-host — the deploy-matrix live tier collided with the
        # PRODUCTION instance's containers ("name already in use", stack died
        # half-up). Suffix by offset; idempotent for the same offset.
        suf = f"-o{offset}"
        def _uname(m):
            name = m.group(1)
            return m.group(0) if name.endswith(suf) else f"container_name: {name}{suf}"
        t = re.sub(r"container_name:\s*([A-Za-z0-9._-]+)", _uname, t)
        compose.write_text(t, encoding="utf-8")
    cfg = dest / ".hermes-data" / "config.yaml"
    if cfg.is_file():
        c = cfg.read_text(encoding="utf-8")
        c = re.sub(r"(localhost:)8730\b", rf"\g<1>{mport}", c)   # gateway -> read MCP url
        cfg.write_text(c, encoding="utf-8")
    print(f"  ports: reader {rport}, mcp {mport} (offset {offset})")


def _pack_meta_mod():
    """Load the sibling pack_meta module by path (no package assumptions)."""
    import importlib.util
    p = ENGINE_ROOT / "scripts" / "pack_meta.py"
    spec = importlib.util.spec_from_file_location("pack_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _jitter_crons(dest: Path) -> None:
    """Expand @jitter:* schedule sentinels in the pulled pack's domain crons into
    concrete, per-install random schedules, so installs don't synchronize once
    feeds.opml is populated (empty feeds make zero upstream calls regardless)."""
    import importlib.util
    p = ENGINE_ROOT / "scripts" / "cron_jitter.py"
    spec = importlib.util.spec_from_file_location("cron_jitter", p)
    cj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cj)
    n = cj.expand_file(dest / "crons" / "domain-crons.json")
    if n:
        print(f"    jittered {n} domain cron schedule(s) (random minute per install)")


def _resolve_offset(cli_offset: int | None, dest: Path) -> tuple[int, str]:
    """Effective host-port offset: an explicit --port-offset wins; otherwise the
    pulled pack's declared pack.yaml `port_offset`; otherwise 0. Returns
    (offset, source)."""
    if cli_offset is not None:
        return cli_offset, "--port-offset"
    try:
        meta = _pack_meta_mod().load_pack_meta(dest)
    except Exception:
        meta = None
    if meta and meta.get("port_offset"):
        return int(meta["port_offset"]), "pack.yaml"
    return 0, ""


def _engine_check(dest: Path) -> None:
    ev = dest / "engine.version"
    if not ev.is_file():
        return
    pinned = ""
    for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("version:"):
            pinned = line.split(":", 1)[1].strip()
            break
    if not pinned:
        return
    cur = engine_version()
    sat = _engine_meta_mod().satisfies_pin(pinned, cur)   # patch-tolerant (okengine#104)
    if sat is False:
        print(f"  ⚠ engine.version {pinned}  ≠  engine {cur} (different release series) — review "
              f"docs/deploy-a-new-domain.md §3 (engine upgrade) before deploy")
    elif pinned.lstrip("engine-") == cur.lstrip("engine-"):
        print(f"  ✓ engine.version {pinned}  ==  engine {cur}")
    else:
        print(f"  ✓ engine.version {pinned}  ~  engine {cur} (same release series — compatible)")


def _engine_meta_mod():
    """Load the sibling engine_meta module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "engine_meta.py"
    spec = importlib.util.spec_from_file_location("engine_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _validate(dest: Path) -> int:
    spec = importlib.util.spec_from_file_location(
        "framework_validate", ENGINE_ROOT / "scripts" / "framework_validate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    rc = m.main([str(dest), "--quiet"])
    return rc


def _install_domain(host_dest: Path, guest: Path) -> int:
    """Run `framework install-domain <host_dest> <guest> --apply` in-process (import by path,
    the same pattern as _validate). Folds a guest pack's types/namespaces/crons/aliases into
    the composed host vault. Returns the subcommand's exit code (0 = installed)."""
    spec = importlib.util.spec_from_file_location(
        "framework_install_domain", ENGINE_ROOT / "scripts" / "framework_install_domain.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.main([str(host_dest), str(guest), "--apply"])


def _load_meta_safe(dest: Path):
    try:
        return _pack_meta_mod().load_pack_meta(dest)
    except Exception:
        return None


def _resolve_member(name: str, bundle_spec: dict, catalog: dict | None) -> dict:
    """Resolve a bundle recipe member (host or a compose pack) to a fetch spec. Prefer the
    catalog (honors that pack's own repo/subdir/ref); otherwise treat it as a SIBLING of the
    bundle in the same monorepo — swap the last path segment of the bundle's subdir. This
    covers the library case (all okpack-* live under packs/ of one repo) without a catalog."""
    for p in ((catalog or {}).get("packs") or []):
        if p.get("name") == name:
            return {"name": name, "giturl": _giturl(p["repo"]),
                    "subdir": p.get("subdir") or "", "ref": p.get("ref")}
    subdir = bundle_spec.get("subdir") or ""
    if subdir:
        parent = subdir.rsplit("/", 1)[0] if "/" in subdir else ""
        return {"name": name, "giturl": bundle_spec["giturl"],
                "subdir": f"{parent}/{name}" if parent else name, "ref": bundle_spec.get("ref")}
    raise SystemExit(
        f"ERROR: bundle member '{name}' is not in the catalog and can't be derived as a "
        f"sibling of the bundle (the bundle was pulled as a whole repo, not a monorepo subdir).\n"
        f"  Add '{name}' to the catalog, or pull the bundle via okpacks-library:<bundle>.")


def _expand_bundle(meta: dict, bundle_spec: dict, dest: Path, catalog: dict | None) -> None:
    """okengine#181: a `kind: bundle` pack is a RECIPE, not a vault. Turn `dest` into the
    composed vault in place — fetch the recipe's `host` pack over the thin bundle dir (it
    becomes the base vault), then `install-domain --apply` each `compose` pack onto it. This
    automates the proven manual composition (the okcti assembly)."""
    errs = _pack_meta_mod().validate_bundle_recipe(meta)
    if errs:
        raise SystemExit("ERROR: malformed bundle recipe in pack.yaml:\n  " + "\n  ".join(errs))
    host, compose = meta["bundle_host"], meta["bundle_compose"]
    print(f"  ⧉ bundle: host {host} + {len(compose)} composed pack(s): {', '.join(compose)}")
    print(f"    ↓ host {host}")
    fetch(_resolve_member(host, bundle_spec, catalog), dest, force=True)
    for member in compose:
        m_spec = _resolve_member(member, bundle_spec, catalog)
        with tempfile.TemporaryDirectory() as td:
            guest = Path(td) / member
            fetch(m_spec, guest, force=True)
            print(f"    ⊕ install-domain {member}")
            rc = _install_domain(dest, guest)
            if rc != 0:
                raise SystemExit(
                    f"ERROR: install-domain of bundle member '{member}' failed (exit {rc}) — "
                    f"the recipe does not compose cleanly onto {host}.")
    print(f"  ✓ bundle composed into {dest} (host {host} + {len(compose)} pack(s))")


# Operator-owned trees an in-place update must NEVER touch (runtime, secrets,
# content) — also skipped when surfacing/clearing `.upstream` files.
_UPDATE_PRESERVE = {".env", ".hermes-data", "raw", "wiki", ".git"}


def _update_in_place(upstream: Path, dest: Path) -> dict:
    """Side-by-side update of an existing pack. Brings in NEW upstream definition
    files, surfaces CHANGED ones as `<file>.upstream` (NEVER overwrites the
    operator's file), and never touches runtime/content (.env, .hermes-data, raw,
    wiki, .git). Returns {added, changed, unchanged}."""
    # Clear stale `.upstream` from a prior update (outside the preserved trees).
    for old in dest.rglob("*.upstream"):
        if old.relative_to(dest).parts[0] not in _UPDATE_PRESERVE:
            old.unlink()
    added: list[str] = []
    changed: list[str] = []
    unchanged = 0
    for src in sorted(upstream.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(upstream)
        if rel.parts[0] in _UPDATE_PRESERVE or "__pycache__" in rel.parts or src.suffix == ".pyc":
            continue
        target = dest / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            added.append(rel.as_posix())
        elif src.read_bytes() != target.read_bytes():
            shutil.copy2(src, target.with_name(target.name + ".upstream"))
            changed.append(rel.as_posix())
        else:
            unchanged += 1
    return {"added": added, "changed": changed, "unchanged": unchanged}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="framework pull", description=__doc__)
    ap.add_argument("source")
    ap.add_argument("dest", nargs="?", default="")
    ap.add_argument("--into", default="", help="place inside this packs/ dir (for composition)")
    ap.add_argument("--ref", default="", help="branch/tag to pull (overrides the catalog ref)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--update", action="store_true",
                    help="update an EXISTING pack in place: keep .env/.hermes-data/raw/wiki, add new "
                         "upstream files, and write changed ones as <file>.upstream for manual merge")
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG)
    ap.add_argument("--port-offset", type=int, default=None,
                    help="add to reader(9200)/mcp(8730) host ports (overrides the pack's "
                         "declared pack.yaml port_offset)")
    args = ap.parse_args(argv)

    catalog, catalog_err = read_catalog(args.catalog)
    spec, curated = resolve(args.source, catalog, catalog_err)
    if args.ref:
        spec["ref"] = args.ref

    if args.into:
        dest = Path(args.into).expanduser() / spec["name"]
    elif args.dest:
        dest = Path(args.dest).expanduser()
    else:
        dest = Path.cwd() / spec["name"]

    if not curated:
        print(f"  ⚠ uncurated pack (not in the catalog). Its crons run their own "
              f"prompts/scripts on deploy.\n    It ships inert — review crons/ + "
              f"CLAUDE.md before you enable anything.")
    where = spec["giturl"] + (f" : {spec['subdir']}" if spec["subdir"] else "")

    if args.update:
        if not dest.is_dir() or not ((dest / "pack.yaml").is_file() or (dest / "schema.yaml").is_file()):
            raise SystemExit(f"ERROR: --update needs an existing pack dir (with pack.yaml/schema.yaml): {dest}")
        print(f"  ↻ update {spec['name']}  in {dest}  ←  {where}")
        with tempfile.TemporaryDirectory() as td:
            clone = Path(td) / "clone"
            branch = ["--branch", spec["ref"]] if spec["ref"] else []
            try:
                _git(["clone", "--depth", "1", *branch, spec["giturl"], str(clone)])
            except subprocess.CalledProcessError as e:
                msg = ((e.stderr or "").strip().splitlines() or [""])[-1]
                raise SystemExit(f"ERROR: git clone failed — {msg}")
            up = clone / spec["subdir"] if spec["subdir"] else clone
            if not up.is_dir():
                raise SystemExit(f"ERROR: subdir '{spec['subdir']}' not found in {spec['giturl']}")
            s = _update_in_place(up, dest)
        print(f"  + {len(s['added'])} new · ~ {len(s['changed'])} changed (.upstream written) · "
              f"= {s['unchanged']} unchanged")
        if s["added"]:
            print("    new:     " + ", ".join(s["added"][:8]) + (" …" if len(s["added"]) > 8 else ""))
        if s["changed"]:
            print("    review:  " + ", ".join(f"{x}.upstream" for x in s["changed"][:8])
                  + (" …" if len(s["changed"]) > 8 else ""))
            print("    → diff each *.upstream against your file, merge, then delete the .upstream copies.")
        print("    preserved: .env / .hermes-data/ / raw/ / wiki/ untouched.")
        _engine_check(dest)
        if not args.no_validate:
            _validate(dest)
        return 0

    print(f"  ↓ {spec['name']}  ←  {where}{' @ ' + spec['ref'] if spec['ref'] else ''}")
    fetch(spec, dest, args.force)

    # okengine#181: if what we pulled is a `kind: bundle` recipe, expand it in place — the host
    # pack becomes the base vault and each compose pack is install-domain'd on. Capture the
    # bundle's declared port_offset FIRST (expansion clobbers dest/pack.yaml with the host's).
    bundle_meta = _load_meta_safe(dest)
    is_bundle = bool(bundle_meta and bundle_meta.get("kind") == "bundle")
    bundle_offset = int(bundle_meta.get("port_offset") or 0) if is_bundle else 0
    if is_bundle:
        _expand_bundle(bundle_meta, spec, dest, catalog)

    _jitter_crons(dest)
    _layer_runtime(dest)
    # For a bundle, the RECIPE's port_offset governs the composed host (the host pack's own
    # default is irrelevant here); an explicit --port-offset still wins over both.
    cli_offset = args.port_offset
    if cli_offset is None and is_bundle and bundle_offset:
        cli_offset = bundle_offset
    offset, src = _resolve_offset(cli_offset, dest)
    _apply_port_offset(dest, offset)
    if offset and src == "pack.yaml":
        print(f"    (offset {offset} is the pack's declared default — override with --port-offset)")
    elif offset and is_bundle and cli_offset == bundle_offset and args.port_offset is None:
        print(f"    (offset {offset} is the bundle recipe's default — override with --port-offset)")
    print(f"  ✓ fetched into {dest} (definition; runtime reset + config.yaml seeded)")
    _engine_check(dest)

    if not args.no_validate:
        _validate(dest)
    if not (dest / "pack.yaml").is_file():
        print("  ⚠ no pack.yaml — not a recognized pack (or a pre-v0.2.0 pack; see "
              "docs/authoring-a-pack.md §2a)")
    print(f"\n  next: edit schema.yaml / CLAUDE.md / feeds in {dest}, cp .env.example .env,\n"
          f"        then docs/authoring-a-pack.md §7 to deploy. (ships inert — opt in to enable)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
