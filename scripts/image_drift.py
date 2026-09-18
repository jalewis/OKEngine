#!/usr/bin/env python3
"""image_drift.py — prove each engine-built image was built from THIS source, by inspecting it.

The fourth deploy surface. The other three are covered:
  baked vs staged          deployment_checks.check_write_path_libs
  source vs staged         scripts/staged_drift.py
  source vs release state  scripts/engine_source_guard.py
  image vs source          THIS (okengine#590)

The gap it closes, measured 2026-08-15: `okcti-review-write` -- write_server.py with
OKENGINE_WRITE_REVIEW_ONLY=1, mounting the vault read-write -- had been running an image built
three weeks and 20 commits earlier, missing write-path ENFORCEMENT the gateway had already gained
(reject entity writes citing non-existent source pages, reject dangling source paths, source
identity by URL). Nothing reported it. An invariant enforced at one boundary and not the other is
not enforced, and here the two boundaries were the same file at two ages.

Why content hashing rather than a build label: the gateway image carries org.okengine.git_sha
because build-engine-image.sh runs `docker build --label`. These images are built by
`docker compose build` from a docker-compose.yml that belongs to the PACK, so the engine cannot add
`build.labels` without editing every pack. The file that lands in the image is the one thing the
engine does control, so compare that.

Containers are resolved by compose labels, never by name: names are per-deployment
(`okcti-cockpit`, `<pack>-cockpit`) and matching on them would silently check nothing on a
pack whose containers are named differently -- a green tick over an empty set.

Exit 1 on drift, and on UNDETECTABLE: a service whose file cannot be read, or a run that compared
nothing at all, is not a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:                                    # pragma: no cover - yaml is a hard dep here
    yaml = None                                        # pragma: no cover - same import guard


def file_hash(path: Path) -> str:
    """sha256 of a file, or "" if it cannot be read. "" never compares equal to a real digest."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# Run the SAME content-only algorithm on the source host and inside the image.
# Cache files are created after COPY and must not make an otherwise identical
# extensions tree look drifted. All other files (including new operation.yaml
# definitions and symlinks) contribute their relative path and contents.
_TREE_HASH_PROGRAM = r'''
import hashlib
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
excluded = {item.strip("/") for item in sys.argv[2:] if item.strip("/")}
if not root.is_dir():
    raise SystemExit(2)
skip_dirs = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
entries = []
for path in root.rglob("*"):
    rel = path.relative_to(root)
    if rel.as_posix() in excluded or any(
        rel.as_posix().startswith(item.rstrip("/") + "/") for item in excluded
    ):
        continue
    if any(part in skip_dirs for part in rel.parts) or path.suffix in {".pyc", ".pyo"}:
        continue
    if path.is_symlink():
        kind, payload = b"L", os.readlink(path).encode("utf-8", errors="surrogateescape")
    elif path.is_file():
        kind, payload = b"F", hashlib.sha256(path.read_bytes()).digest()
    elif path.is_dir():
        continue
    else:
        raise SystemExit(3)
    entries.append((rel.as_posix().encode("utf-8", errors="surrogateescape"), kind, payload))
digest = hashlib.sha256()
for name, kind, payload in sorted(entries):
    digest.update(len(name).to_bytes(8, "big") + name)
    digest.update(kind + len(payload).to_bytes(8, "big") + payload)
print(digest.hexdigest())
'''


def load_manifest(path: Path) -> dict[str, dict[str, str]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    services = raw.get("services")
    if not isinstance(services, dict):
        return {}
    out: dict[str, dict] = {}
    for name, spec in services.items():
        if not (isinstance(spec, dict) and spec.get("source") and spec.get("container")):
            continue
        entry: dict = {"source": str(spec["source"]), "container": str(spec["container"])}
        # `probes` is the primary pair plus any `also:` rows. ONE representative file is a weak
        # proxy for "built from this source": measured on the okengine#596 incident, the stale
        # read-MCP's server.py was byte-identical across six weeks while the base-schema.yaml and
        # search libs baked beside it were not, so a single-probe check would have called it
        # clean. A service may therefore name additional files, and ANY of them differing is
        # drift. The primary pair stays a plain source/container so existing readers are unchanged.
        probes = [entry.copy()]
        for extra in spec.get("also") or []:
            if not isinstance(extra, dict):
                continue
            if extra.get("source") and extra.get("container"):
                probes.append({"source": str(extra["source"]),
                               "container": str(extra["container"])})
            elif extra.get("source_dir") and extra.get("container_dir"):
                tree_probe = {"source_dir": str(extra["source_dir"]),
                              "container_dir": str(extra["container_dir"])}
                if extra.get("exclude"):
                    tree_probe["exclude"] = [str(item) for item in extra["exclude"]]
                probes.append(tree_probe)
        entry["probes"] = probes
        out[str(name)] = entry
    return out


def _run(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _valid_digest(out: str | None) -> str:
    digest = out.split()[0] if out and out.split() else ""
    return digest if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower()) else ""


def tree_hash(path: Path, exclude: list[str] | None = None) -> str:
    """Content digest of a source tree, or empty when the tree cannot be read."""
    return _valid_digest(_run(
        [sys.executable, "-c", _TREE_HASH_PROGRAM, str(path), *(exclude or [])]
    ))


def container_tree_hash(container: str, path: str, exclude: list[str] | None = None) -> str:
    """Run the identical tree digest in a running image, without writing there."""
    return _valid_digest(_run([
        "docker", "exec", container, "python", "-c", _TREE_HASH_PROGRAM, path,
        *(exclude or []),
    ]))


def running_containers(project: str) -> dict[str, str]:
    """{compose service -> container id} for one compose project, via labels not names."""
    out = _run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                "--format", "{{.ID}} {{.Label \"com.docker.compose.service\"}}"])
    if not out:
        return {}
    found: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            found[parts[1]] = parts[0]
    return found


def container_hash(container: str, path: str) -> str:
    """sha256 of a file inside a container, or "" if it cannot be read."""
    out = _run(["docker", "exec", container, "sha256sum", path])
    if not out:
        return ""
    return _valid_digest(out)


def compare(manifest: dict, containers: dict, engine_dir: Path) -> dict:
    """Classify each service. `unreadable` is deliberately NOT folded into `drifted`: drift means
    the image was built from other source, unreadable means the check could not be performed, and
    an operator chasing the first when it is the second looks in the wrong place."""
    result = {"in_sync": [], "drifted": [], "unreadable": [], "not_running": []}
    for service, spec in sorted(manifest.items()):
        container = containers.get(service)
        if not container:
            result["not_running"].append(service)
            continue
        verdicts = []
        for probe in spec.get("probes") or [spec]:
            if "source_dir" in probe and "container_dir" in probe:
                excluded = probe.get("exclude") or []
                src = tree_hash(engine_dir / probe["source_dir"], excluded)
                dst = container_tree_hash(container, probe["container_dir"], excluded)
            else:
                src = file_hash(engine_dir / probe["source"])
                dst = container_hash(container, probe["container"])
            verdicts.append("unreadable" if not src or not dst
                            else "in_sync" if src == dst else "drifted")
        # Worst verdict wins, and `unreadable` outranks `in_sync`: one probe agreeing does not
        # make up for another that could not be read.
        if "drifted" in verdicts:
            result["drifted"].append(service)
        elif "unreadable" in verdicts:
            result["unreadable"].append(service)
        else:
            result["in_sync"].append(service)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="docker compose project name")
    parser.add_argument("--engine-dir", required=True)
    parser.add_argument("--manifest", default=None)
    args = parser.parse_args(argv)

    engine_dir = Path(args.engine_dir).resolve()
    manifest_path = Path(args.manifest) if args.manifest else engine_dir / "config" / "image-provenance.yaml"
    manifest = load_manifest(manifest_path)
    if not manifest:
        print(f"image drift: UNDETECTABLE — no service manifest at {manifest_path}; "
              "nothing was compared, which is not a pass", file=sys.stderr)
        return 1

    containers = running_containers(args.project)
    result = compare(manifest, containers, engine_dir)

    # ATTEMPTED, not "successfully compared". An unreadable service WAS inspected and the answer
    # was "cannot tell" — a per-service finding the operator needs BY NAME. Excluding it here sent
    # it down the "compared nothing" path, so the report read `0 of 1` and never said which service
    # or why, collapsing "pointed at the wrong compose project" into "this file could not be read":
    # two faults with different fixes. Both still exit 1; only the message differs, and the message
    # is the entire value of the check.
    checked = len(result["in_sync"]) + len(result["drifted"]) + len(result["unreadable"])
    for service in result["in_sync"]:
        print(f"  in sync    {service}")
    for service in result["not_running"]:
        print(f"  not running {service} (skipped)")
    if not checked:
        print(f"image drift: UNDETECTABLE — 0 of {len(manifest)} service(s) could be compared "
              f"in project {args.project!r}; a check that measured nothing is not a pass",
              file=sys.stderr)
        return 1

    print(f"image drift: {checked} service(s) compared in project {args.project!r}")
    if not result["drifted"] and not result["unreadable"]:
        return 0
    for service in result["drifted"]:
        print(f"  DRIFTED    {service} — the running image was not built from this source",
              file=sys.stderr)
    for service in result["unreadable"]:
        print(f"  UNDETECTABLE {service} — its file could not be read on one side", file=sys.stderr)
    print("The deploy reported success; the running image disagrees. Rebuild and roll it.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
