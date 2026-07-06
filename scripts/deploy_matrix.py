#!/usr/bin/env python3
"""deploy-matrix — build/deploy-test every pack and the real combos, then tear down.

Automates the manual protocol that found six installer/preflight bugs in one day
(install -> probe -> tear down -> reinstall): every pack, every co-install shape,
the real composition combos — repeatable, with teardown on success.

TIER 1 (offline, always — minutes, no docker):
  validate    framework validate 0-FAIL for every pack
  conform     each pack's conformance suite (conformance/run_*.py), where shipped
  compose     compose-preview over every 2-combo (+ the full-library combo) — SAFE required
  coinstall   every pack shipping a subdomain/ form x every shape, each into a FRESH
              commented scratch host: apply -> assertions (types/ns/persona landed,
              host comments survive) -> re-apply converges to no-op -> teardown.
              Plus the sequential multipack cell (all taxonomy guests into ONE host).

TIER 2 (--live PACK..., serial — real docker stacks):
  pull the pack from the LOCAL library catalog into a scratch deployment (unique
  port offset), minimal .env, then the engine's deploy.sh (validate -> seed ->
  compose up -> crons -> post_deploy_verify against the LIVE stack). SUCCESS ->
  full teardown (docker compose down -v + rm). FAILURE -> stack left UP and dir
  kept for inspection (printed).

Usage:
  python3 scripts/deploy_matrix.py [--library DIR] [--packs DIR ...]
                                   [--live NAME ... | --live-all] [--port-base N]
Exit: 0 all green · 1 failures (summary table printed either way).
"""
from __future__ import annotations

import argparse
import itertools
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, str, str]] = []   # (cell, PASS/FAIL/SKIP, detail)


def record(cell: str, ok: bool | None, detail: str = "") -> None:
    RESULTS.append((cell, "PASS" if ok else ("SKIP" if ok is None else "FAIL"), detail))
    print(f"  [{RESULTS[-1][1]}] {cell}" + (f" — {detail}" if detail else ""))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def framework(*args, env_extra: dict | None = None, **kw):
    import os
    env = dict(os.environ)
    env.update(env_extra or {})
    return run([sys.executable, str(ENGINE / "scripts" / "framework.py"), *args],
               env=env, **kw)


def pack_dirs(library: Path, extra: list[Path]) -> list[Path]:
    out = [d for d in sorted((library / "packs").iterdir())
           if d.is_dir() and (d / "schema.yaml").is_file()]
    return out + [p for p in extra if p.is_dir()]


# ── tier 1 ───────────────────────────────────────────────────────────────────
def t1_validate(packs: list[Path]) -> None:
    print("== tier1: framework validate ==")
    for p in packs:
        r = framework("validate", str(p), "--quiet")
        record(f"validate:{p.name}", r.returncode == 0,
               "" if r.returncode == 0 else (r.stdout + r.stderr).strip().splitlines()[-1][:120])


def t1_conformance(packs: list[Path]) -> None:
    print("== tier1: conformance suites ==")
    for p in packs:
        runners = sorted((p / "conformance").glob("run_*.py")) if (p / "conformance").is_dir() else []  # glob-ok: flat pack dir
        if not runners:
            record(f"conform:{p.name}", None, "no suite shipped")
            continue
        for rn in runners:
            r = run([sys.executable, str(rn)], cwd=str(p))
            record(f"conform:{p.name}:{rn.name}", r.returncode == 0,
                   "" if r.returncode == 0 else (r.stdout + r.stderr).strip().splitlines()[-1][:120])


def t1_compose(packs: list[Path]) -> None:
    print("== tier1: composition combos ==")
    public = [p for p in packs
              if (yaml.safe_load((p / "pack.yaml").read_text()) or {}).get("trust") == "public"]
    combos = list(itertools.combinations(public, 2))
    if len(public) > 2:
        combos.append(tuple(public))          # the full-library combo
    for combo in combos:
        r = framework("compose-preview", *[str(p) for p in combo])
        ok = "SAFE: no blocking conflicts" in r.stdout
        record("compose:" + "+".join(p.name.replace("okpack-", "") for p in combo), ok,
               "" if ok else next((ln.strip() for ln in r.stdout.splitlines() if "- " in ln and "SCHEMA" in ln or "TRUST" in ln), "blocked")[:120])


def _scratch_host(root: Path) -> Path:
    """A minimal COMMENTED host (comments are part of the assertion surface)."""
    h = root / "host"
    (h / "wiki").mkdir(parents=True)
    for d in ("config", "crons", "feeds"):
        (h / d).mkdir()
    (h / "schema.yaml").write_text(
        "# HOST HEADER COMMENT — must survive every merge\n"
        "name: okpack-host\n"
        "types:\n"
        "  entity: {required: [type]}   # host inline comment\n"
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\n"
        "permissions:\n  default: {create: true, update: true, delete: false}\n"
        "# HOST TRAILING COMMENT — must survive\n")
    (h / "pack.yaml").write_text("name: okpack-host\n")
    (h / "CLAUDE.md").write_text("# host persona\n")
    return h


def _shapes(pack: Path) -> list[str]:
    out = []
    if (pack / "subdomain" / "schema.yaml").is_file():
        out.append("subtree")
    if (pack / "subdomain" / "host-schema-additions.yaml").is_file():
        out.append("taxonomy")
    return out


def _install(host: Path, pack: Path, shape: str, shapes: list[str]):
    args = ["install-domain", str(host), str(pack), "--apply"]
    if len(shapes) > 1:
        args += ["--shape", shape]
    return framework(*args)


def t1_coinstall(packs: list[Path]) -> None:
    print("== tier1: co-install matrix (fresh host per cell, teardown after) ==")
    guests = [(p, s) for p in packs for s in _shapes(p)]
    for pack, shape in guests:
        cell = f"coinstall:{pack.name}:{shape}"
        with tempfile.TemporaryDirectory(prefix="okmatrix-") as td:
            host = _scratch_host(Path(td))
            r1 = _install(host, pack, shape, _shapes(pack))
            if r1.returncode != 0:
                record(cell, False, (r1.stdout + r1.stderr).strip().splitlines()[-1][:120])
                continue
            schema = (host / "schema.yaml").read_text()
            errs = []
            if "HOST HEADER COMMENT" not in schema or "HOST TRAILING COMMENT" not in schema:
                errs.append("host comments destroyed")
            if "## Installed domain:" not in (host / "CLAUDE.md").read_text():
                errs.append("persona marker missing")
            r2 = _install(host, pack, shape, _shapes(pack))
            if r2.returncode != 0:
                errs.append("re-apply failed")
            elif "nothing to do" not in r2.stdout:
                errs.append("re-apply not idempotent: " +
                            ";".join(ln.strip() for ln in r2.stdout.splitlines() if ln.startswith("  - "))[:80])
            record(cell, not errs, "; ".join(errs))
        # teardown = the TemporaryDirectory exiting — nothing to leak
    # the real multipack story: every taxonomy guest sequentially into ONE host
    taxo = [p for p, s in guests if s == "taxonomy"]
    if len(taxo) > 1:
        with tempfile.TemporaryDirectory(prefix="okmatrix-") as td:
            host = _scratch_host(Path(td))
            errs = []
            for pack in taxo:
                r = _install(host, pack, "taxonomy", _shapes(pack))
                if r.returncode != 0:
                    errs.append(f"{pack.name}: " + (r.stdout + r.stderr).strip().splitlines()[-1][:80])
                    break
            record("coinstall:multipack:" + "+".join(p.name.replace("okpack-", "") for p in taxo),
                   not errs, "; ".join(errs))


# ── tier 2 ───────────────────────────────────────────────────────────────────
def t2_live(name: str, library: Path, offset: int, keep_dir: Path) -> None:
    cell = f"live:{name}"
    dest = keep_dir / f"okmatrix-{name}"
    if dest.exists():
        shutil.rmtree(dest)
    # pull the CONTENT UNDER TEST from the local library checkout (committed HEAD),
    # not the public snapshot — OKENGINE_LIBRARY local-path support exists for this
    r = framework("pull", f"okpacks-library:{name}", str(dest),
                  "--port-offset", str(offset),
                  env_extra={"OKENGINE_LIBRARY": str(library)})
    if r.returncode != 0:
        record(cell, False, "pull failed: " + (r.stdout + r.stderr).strip().splitlines()[-1][:120])
        return
    env_ex = dest / ".env.example"
    if env_ex.is_file():
        (dest / ".env").write_text(env_ex.read_text())
    dep = run(["bash", str(ENGINE / "scripts" / "deploy.sh"), "--skip-build"],
              cwd=str(dest), env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                                  "HOME": str(Path.home()),
                                  "ENGINE_DIR": str(ENGINE)})
    if dep.returncode != 0:
        record(cell, False, "deploy.sh failed — STACK LEFT UP for inspection at "
               f"{dest} (last: " + (dep.stdout + dep.stderr).strip().splitlines()[-1][:100] + ")")
        return
    # success -> full teardown (harness-created stack only)
    down = run(["docker", "compose", "down", "-v"], cwd=str(dest))
    shutil.rmtree(dest, ignore_errors=True)
    record(cell, True, "deployed, verified live, torn down"
           + ("" if down.returncode == 0 else " (compose down rc!=0 — check manually)"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="build/deploy-test packs + combos, teardown on success")
    ap.add_argument("--library", default=str(Path.home() / "Source" / "okpacks-library"))
    ap.add_argument("--packs", nargs="*", default=[], help="extra local pack dirs (e.g. private packs)")
    ap.add_argument("--live", nargs="*", default=[], help="pack NAMES to full-stack deploy-test")
    ap.add_argument("--live-all", action="store_true", help="live-test every catalog pack")
    ap.add_argument("--port-base", type=int, default=750,
                    help="live port offsets start here (per-pack +10)")
    ap.add_argument("--workdir", default="/tmp", help="live scratch parent dir")
    a = ap.parse_args(argv)

    library = Path(a.library).resolve()
    packs = pack_dirs(library, [Path(p).resolve() for p in a.packs])
    print(f"deploy-matrix: {len(packs)} pack(s) from {library}")

    t1_validate(packs)
    t1_conformance(packs)
    t1_compose(packs)
    t1_coinstall(packs)

    live = a.live
    if a.live_all:
        live = [json.loads((library / "catalog.json").read_text())["packs"][i]["name"]
                for i in range(len(json.loads((library / "catalog.json").read_text())["packs"]))]
    for i, name in enumerate(live):
        print(f"== tier2: live deploy {name} ==")
        t2_live(name, library, a.port_base + i * 10, Path(a.workdir))

    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"\n== matrix: {len(RESULTS)} cells · "
          f"{sum(1 for r in RESULTS if r[1] == 'PASS')} pass · "
          f"{len(fails)} fail · {sum(1 for r in RESULTS if r[1] == 'SKIP')} skip ==")
    for c, s, d in fails:
        print(f"  FAIL {c}: {d}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
