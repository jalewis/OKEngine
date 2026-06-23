#!/usr/bin/env python3
"""framework init — scaffold a new OKF domain pack.

Single source of truth: the pack template is `templates/pack/skeleton/` (the SAME
skeleton `templates/pack/new-pack.sh` renders — there is exactly one template).
This command renders that skeleton with the engine's defaults and then layers on
the deploy-only runtime bits the *published* git skeleton intentionally omits
(`.hermes-data/config.yaml` + `qmd/`), so the result is ready to `docker compose
up` against the engine.

Usage:
  scripts/framework_init.py <dest-dir> [--domain "Display Name"] [--feeds feeds.opml]
                            [--delivery telegram|local] [--port-offset N] [--no-compose]
Refuses to overwrite a non-empty dest.
"""
from __future__ import annotations

import argparse
import re
import secrets
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SKELETON = ENGINE_ROOT / "templates" / "pack" / "skeleton"

# Match only {{UPPER_SNAKE}} so we never trip over escaped braces in code
# (e.g. a Python f-string's {{...}}), matching new-pack.sh's leftover check.
_TOKEN_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")


def _engine_meta():
    """Load the sibling engine_meta module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "engine_meta.py"
    spec = importlib.util.spec_from_file_location("engine_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def engine_version() -> str:
    # The engine manifest is authoritative — this is the SAME value
    # `framework validate` requires a pack to pin, so a fresh scaffold always
    # matches the engine it was scaffolded from. Fall back to the git tag only if
    # the manifest can't be read.
    try:
        v = _engine_meta().engine_release()
        if v:
            return v
    except Exception:
        pass
    try:
        out = subprocess.run(["git", "-C", str(ENGINE_ROOT), "describe", "--tags",
                              "--match", "v*", "--abbrev=0"],
                             capture_output=True, text=True, timeout=10)
        return (out.stdout or "").strip() or "v0.3.0"
    except Exception:
        return "v0.3.0"


def hermes_pin() -> str:
    try:
        return _engine_meta().hermes_pin() or "v2026.6.5"
    except Exception:
        return "v2026.6.5"


def _tokens(dest: Path, domain: str, offset: int) -> dict[str, str]:
    """The full token vocabulary the skeleton expects (see templates/pack/PLACEHOLDERS.md).
    Derived from the dest dir name + engine defaults; mirrors new-pack.sh."""
    pack = dest.name
    short = pack[len("okpack-"):] if pack.startswith("okpack-") else pack
    title = domain or f"{short} knowledge vault"
    blurb = (f"Agent-curated {title} for the OKEngine framework — ingests open "
             "feeds into a compounding, cross-linked knowledge graph.")
    return {
        "PACK": pack,
        "DOMAIN": short,
        "TITLE": title,
        "BLURB": blurb,
        "ENGINE_VERSION": engine_version(),
        "HERMES_PIN": hermes_pin(),
        "PORT_OFFSET": str(offset),
        "READER_PORT": str(9200 + offset),
        "MCP_PORT": str(8730 + offset),
        "ENV_PREFIX": re.sub(r"[^A-Z0-9]", "_", pack.upper()),
        "PACK_UNDERSCORE": pack.replace("-", "_"),
        "BRIEF_HOUR": "13",
        "OWNER": "REPLACE_OWNER",
        "LICENSE_YEAR": str(date.today().year),
        "CRON_ID_1": secrets.token_hex(6),
        "CRON_ID_2": secrets.token_hex(6),
    }


def _render(dest: Path, tokens: dict[str, str]) -> None:
    """Copy skeleton/ -> dest, rename templated filenames, substitute every
    {{TOKEN}}, and fail loudly if any {{UPPER_SNAKE}} token survives."""
    shutil.copytree(SKELETON, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    pu = tokens["PACK_UNDERSCORE"]
    # rename any templated filenames (e.g. {{PACK_UNDERSCORE}}_feed_fetch.py)
    for p in list(dest.rglob("*")):
        if p.is_file() and "{{PACK_UNDERSCORE}}" in p.name:
            p.rename(p.with_name(p.name.replace("{{PACK_UNDERSCORE}}", pu)))
    repl = {f"{{{{{k}}}}}": v for k, v in tokens.items()}
    for p in dest.rglob("*"):
        if not p.is_file():
            continue
        try:
            s = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        n = s
        for k, v in repl.items():
            n = n.replace(k, v)
        if n != s:
            p.write_text(n, encoding="utf-8")
    leftover = []
    for p in dest.rglob("*"):
        if not p.is_file():
            continue
        try:
            if _TOKEN_RE.search(p.read_text(encoding="utf-8")):
                leftover.append(str(p.relative_to(dest)))
        except (UnicodeDecodeError, IsADirectoryError):
            pass
    if leftover:
        raise SystemExit("error: unsubstituted tokens remain in: " + ", ".join(sorted(leftover)))


def _layer_runtime(dest: Path, offset: int = 0) -> None:
    """Add the deploy-only bits the published git skeleton omits: a fresh runtime
    data dir + the engine's config.yaml template (filled in by the operator). When
    a port offset is in play, the seeded config's MCP url must move with the
    compose ports so the gateway reaches the read MCP on the offset host port."""
    (dest / ".hermes-data" / "qmd").mkdir(parents=True, exist_ok=True)
    (dest / ".hermes-data" / ".gitkeep").write_text("", encoding="utf-8")
    tmpl = ENGINE_ROOT / "config" / "config.yaml.template"
    cfg = dest / ".hermes-data" / "config.yaml"
    if tmpl.is_file():
        shutil.copy(tmpl, cfg)
        if offset:
            c = cfg.read_text(encoding="utf-8")
            c = re.sub(r"(localhost:)8730\b", rf"\g<1>{8730 + offset}", c)
            cfg.write_text(c, encoding="utf-8")


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"  ? {prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _jitter_crons(dest: Path) -> None:
    """Expand @jitter:* schedule sentinels in the scaffolded domain crons into
    concrete, per-install random schedules — the herd defense for once feeds go
    live (empty feeds make zero upstream calls regardless)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cron_jitter", ENGINE_ROOT / "scripts" / "cron_jitter.py")
    cj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cj)
    n = cj.expand_file(dest / "crons" / "domain-crons.json")
    if n:
        print(f"  jittered {n} domain cron schedule(s) (random minute per install)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dest", nargs="?", default="", help="directory to create for the new pack")
    ap.add_argument("--domain", default="", help="display title (default: derived from dest name)")
    ap.add_argument("--feeds", default="", help="optional OPML file to seed feeds/feeds.opml")
    ap.add_argument("--delivery", choices=["telegram", "local"], default="telegram")
    ap.add_argument("--interactive", action="store_true", help="prompt for inputs")
    ap.add_argument("--no-compose", action="store_true", help="drop docker-compose.yml")
    ap.add_argument("--port-offset", type=int, default=0,
                    help="add to reader(9200)/mcp(8730) ports to avoid host collisions")
    args = ap.parse_args(argv)

    # Interactive when asked, or when no dest given and we have a TTY.
    interactive = args.interactive or (not args.dest and sys.stdin.isatty())
    if interactive:
        print("framework init — scaffold a new OKF domain pack\n")
        args.dest = args.dest or _ask("Destination directory (e.g. ../okpack-fin)")
        if not args.dest:
            print("ERROR: a destination is required.", file=sys.stderr)
            return 1
        args.domain = args.domain or _ask("Domain display title", Path(args.dest).name)
        args.delivery = _ask("Delivery (telegram/local)", args.delivery)
        args.feeds = args.feeds or _ask("Seed feeds OPML path (or blank)", "")
        po = _ask("Port offset (0 unless another pack runs on this host)", str(args.port_offset))
        try:
            args.port_offset = int(po)
        except ValueError:
            args.port_offset = 0
    if not args.dest:
        ap.error("dest is required (or run with --interactive / a TTY)")

    if not SKELETON.is_dir():
        print(f"ERROR: pack template not found at {SKELETON}", file=sys.stderr)
        return 1

    dest = Path(args.dest).expanduser()
    if dest.exists() and any(dest.iterdir()):
        print(f"ERROR: {dest} exists and is not empty — refusing to overwrite.", file=sys.stderr)
        return 1

    tokens = _tokens(dest, args.domain, args.port_offset)
    _render(dest, tokens)
    _jitter_crons(dest)
    _layer_runtime(dest, args.port_offset)

    if args.no_compose:
        (dest / "docker-compose.yml").unlink(missing_ok=True)
    if args.feeds and Path(args.feeds).is_file():
        shutil.copy(args.feeds, dest / "feeds" / "feeds.opml")

    ver = tokens["ENGINE_VERSION"]
    print(f"✓ scaffolded domain pack '{tokens['TITLE']}' at {dest}  (engine {ver}"
          f"{'' if args.no_compose else ', + docker-compose.yml'})")
    if not args.no_compose and args.port_offset:
        print(f"  ports: reader {tokens['READER_PORT']}, mcp {tokens['MCP_PORT']}")
    print("  next: edit schema.yaml / CLAUDE.md / feeds/, fill .hermes-data/config.yaml,")
    print("        cp .env.example .env, run `python3 validate.py`,")
    print("        then follow docs/deploy-a-new-domain.md §2 to deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
