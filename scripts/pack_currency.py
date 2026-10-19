"""Is each pack installed in a deployment behind its source? (okengine#896)

An engine roll restamps a deployment's `engine.version`, so `framework validate` reports the pin
current while the PACK itself can sit months behind the library: on one fleet five of six
deployments were, one of them on six composed packs, and a breaking rename had never run. Nothing
compared the installed pack with where it came from.

Installed version: the migration state record wins over `pack.yaml` (framework_upgrade's rule: a
reconcile can land the new pack.yaml while its migrations are still pending). Composed packs are
read from `.okengine/installed-domains/<pack>.json`.

Sources are operator-configured, never guessed: `OKENGINE_PACK_SOURCES` lists, in precedence order
(os.pathsep-separated), local pack-repo checkouts (`packs/<name>/pack.yaml`, or a single-pack repo
with `pack.yaml` at its root) and/or `catalog.json` files. The default public catalog is NOT
consulted: it tracks published snapshots, which can lag the internal library by releases, and a
private pack is in no catalog at all. With no source for a pack the answer is UNDETECTABLE --
never "current".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from subprocess import CalledProcessError
from typing import Optional

import yaml
from yaml import YAMLError

from scripts import framework_pull, framework_upgrade

SOURCES_ENV = "OKENGINE_PACK_SOURCES"
SOURCE_REF_ENV = "OKENGINE_PACK_SOURCE_REF"
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)\Z")
_SECTION = re.compile(r"^##\s+v?(\d+\.\d+\.\d+)\b")


def _vtuple(version: object) -> Optional[tuple[int, int, int]]:
    m = _VERSION.match(str(version or "").strip().lstrip("v"))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def installed_packs(pack_dir: Path) -> list[dict]:
    """Every pack installed in the deployment at `pack_dir`: the host pack, then each composed one."""
    out = []
    host = _yaml(pack_dir / "pack.yaml")
    if host.get("name"):
        out.append({"name": str(host["name"]), "role": "host",
                    "installed": framework_upgrade.installed_pack_version(
                        pack_dir, str(host["name"]), fallback=str(host.get("version") or ""))})
    base = pack_dir / ".okengine" / "installed-domains"
    for record in sorted(base.glob("*.json")) if base.is_dir() else ():  # glob-ok: flat manifest dir
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # JSONDecodeError is a ValueError
            data = {}
        name = str(data.get("pack") or record.stem)
        out.append({"name": name, "role": "composed",
                    "installed": framework_upgrade.installed_pack_version(
                        pack_dir, name, fallback=str(data.get("pack_version") or ""))})
    return out


def _git(root: Path, *args: str) -> Optional[str]:
    """stdout of a local, non-networked git command, or None when git is absent or it fails."""
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              check=True).stdout
    except (OSError, CalledProcessError):
        return None


def _repo_files(root: Path) -> tuple[str, dict[str, str]]:
    """(label, {relpath: text}) for every pack manifest and changelog in a pack repo checkout.

    A git checkout is read at its fetched SOURCE_REF (origin/main), never its working tree: a
    checkout left on an old branch reported a pack two releases AHEAD of the library and a stale
    private one as current, the very drift this exists to catch. The label names the commit, so a
    stale fetch is visible in the finding itself."""
    ref = os.environ.get(SOURCE_REF_ENV) or "origin/main"
    # Only a repository's own top level is read through git: `git -C` on a plain directory that
    # merely sits INSIDE some other checkout would answer for that enclosing repository.
    top = (_git(root, "rev-parse", "--show-toplevel") or "").strip()
    own = bool(top) and Path(top).samefile(root)
    sha = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") if own else None
    if sha:
        names = (_git(root, "ls-tree", "-r", "--name-only", ref) or "").splitlines()
        wanted = [n for n in names if n in ("pack.yaml", "CHANGELOG.md")
                  or re.fullmatch(r"packs/[^/]+/(pack\.yaml|CHANGELOG\.md)", n)]
        date = (_git(root, "log", "-1", "--format=%cs", sha.strip()) or "").strip()
        return (f"{root}@{ref} {sha.strip()[:8]} {date}".rstrip(),
                {n: _git(root, "show", f"{ref}:{n}") or "" for n in wanted})
    files = {}
    for path in [root / "pack.yaml", root / "CHANGELOG.md",
                 *(root / "packs").glob("*/pack.yaml"),  # glob-ok: pack repo layout
                 *(root / "packs").glob("*/CHANGELOG.md")]:  # glob-ok: pack repo layout
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return f"{root} (working tree: not a git checkout with {ref})", files


def source_index(sources: list[str]) -> dict[str, dict]:
    """name -> {version, source, changelog}; the first source that knows a pack wins."""
    index: dict[str, dict] = {}
    for src in sources:
        root = Path(src).expanduser()
        if root.is_dir():
            label, files = _repo_files(root)
            for rel, text in sorted(files.items()):
                if not rel.endswith("pack.yaml"):
                    continue
                try:
                    meta = yaml.safe_load(text)
                except YAMLError:
                    meta = None
                if isinstance(meta, dict) and meta.get("name"):
                    log = rel[: -len("pack.yaml")] + "CHANGELOG.md"
                    index.setdefault(str(meta["name"]), {
                        "version": str(meta.get("version") or ""), "source": label,
                        "changelog": files.get(log, "")})
            continue
        catalog, _err = framework_pull.read_catalog(src)
        for entry in (catalog or {}).get("packs") or []:
            if entry.get("name"):
                index.setdefault(str(entry["name"]), {
                    "version": str(entry.get("version") or ""), "source": src, "changelog": ""})
    return index


def migration_impacts(changelog: str, installed: tuple, available: tuple) -> list[str]:
    """The `Migration impact:` lines of every release in (installed, available]."""
    impacts, current = [], None
    for line in changelog.splitlines():
        section = _SECTION.match(line)
        if section:
            current = _vtuple(section.group(1))
        elif current and installed < current <= available and line.startswith("Migration impact:"):
            impacts.append(f"{'.'.join(map(str, current))}: {line.split(':', 1)[1].strip()}")
    return impacts


def assess(pack_dir: Path, sources: list[str]) -> list[tuple[str, str]]:
    """[(level, message)] with level ok|warn, one per installed pack (or one UNDETECTABLE)."""
    if not sources:
        return [("warn", f"UNDETECTABLE: no pack sources configured; set {SOURCES_ENV} to the "
                         "library and private pack-repo checkouts (or catalog files) to compare "
                         "installed packs with them")]
    index = source_index(sources)
    findings = []
    for pack in installed_packs(pack_dir):
        name, have = pack["name"], _vtuple(pack["installed"])
        src = index.get(name)
        want = _vtuple(src["version"]) if src else None
        if src is None:
            findings.append(("warn", f"{name}: UNDETECTABLE, no configured source knows this pack"))
        elif have is None or want is None:
            findings.append(("warn", f"{name}: UNDETECTABLE, unparseable version (installed "
                                     f"{pack['installed']!r}, source {src['version']!r})"))
        elif have < want:
            impacts = migration_impacts(src["changelog"], have, want)
            note = f"; migration impact: {' | '.join(impacts)}" if impacts else ""
            findings.append(("warn", f"{name} ({pack['role']}) is BEHIND: installed "
                                     f"{pack['installed']}, source {src['version']} "
                                     f"({src['source']}){note}"))
        elif have == want:
            findings.append(("ok", f"{name} ({pack['role']}) {pack['installed']} matches its source"))
        else:
            findings.append(("warn", f"{name} ({pack['role']}) is AHEAD of its source: installed "
                                     f"{pack['installed']}, source {src['version']} "
                                     f"({src['source']}); upstream the local change or fix the source"))
    return findings


def configured_sources() -> list[str]:
    return [s for s in os.environ.get(SOURCES_ENV, "").split(os.pathsep) if s.strip()]


def check(pack_dir: Path, report) -> None:
    """framework validate's pack-currency rows: WARN when behind, ahead or undetectable; a pack
    behind its source still deploys, but the engine-pin check passes on every roll regardless."""
    try:
        findings = assess(pack_dir, configured_sources())
    except framework_upgrade.MigrationStateError as exc:
        # The installed version comes from the migration state; a torn state file is unknown,
        # never "nothing recorded" (okengine#991). Report it; the rest of validate still runs.
        report.fail("pack currency", str(exc))
        return
    for level, message in findings:
        (report.ok if level == "ok" else report.warn)("pack currency", message)
