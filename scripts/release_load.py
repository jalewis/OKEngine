#!/usr/bin/env python3
"""release_load.py — load the release's CI-tested images by digest; never build (okengine#1048).

`deploy.sh --release-digests <tested-digests.json>` runs this before `docker compose up --no-build`.
For every engine image the deployment runs (the compose-built services, from engine_images.py, and
the gateway) it:

1. refuses a list that is not for this engine checkout's exact, clean HEAD, or that lacks an image
   the deployment runs;
2. loads each image by digest from the project registry through a pinned, containerized loader
   (LOADER_IMAGE: crane). The loader only fetches and streams a tarball to stdout; it gets no docker
   socket, and the host's own `docker load` imports it. Registry credentials reach the loader only
   through `docker run --env-file <host-local file>`, never argv;
3. verifies the loaded image's config ID equals the list's `id`, for EVERY image, before any local
   tag is moved; a mismatch FAILs and nothing is tagged;
4. tags each verified image with the reference this deployment pins (the scoped engine tag and the
   gateway image), so `docker compose up --no-build` runs exactly those bytes;
5. only then removes crane's placeholder name (`<repo>:i-was-a-digest`). It is a freshly loaded
   image's ONLY tag, and `docker rmi` of an image's last tag deletes the image, so it may go only
   once a real tag holds the image. A failed run leaves it, and the loaded bytes, in place.

The credential file (REGISTRY_ENV_FILE, default ~/.config/okengine/registry.env) is host-local and
must sit outside the engine checkout and the deployment, mode 0600, holding
OKENGINE_REGISTRY_USER=<deploy token username> and OKENGINE_REGISTRY_TOKEN=<read_registry token>.

The loader container does not see the deploy host's /etc/hosts, where a registry name may be the
only record (okengine#1058: the first digest roll failed with `lookup <registry>: no such host`).
The registry host is resolved HERE, on the deploy host (getaddrinfo honors /etc/hosts), and handed
to the loader as `--add-host <name>:<address>`; an IP-literal registry needs none. An unresolvable
name is a FAIL with the reason, before anything is fetched.

`--preflight` runs every check above that needs no tag moved (the list, the revision, the required
images, the credentials, the registry name) and then proves the loader can reach the registry with
those credentials: `crane digest` of one image per registry that is not already present locally
must return the list's manifest digest. deploy.sh runs it before it writes any pin, so a resolution
or credential failure FAILs with `.env` and the image override untouched.

Exit 0 when every image is loaded, verified and tagged (or, with --preflight, when the registry is
reachable); 1 otherwise (FAIL, with the reason).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from subprocess import SubprocessError
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_images  # noqa: E402
import tested_digests  # noqa: E402

#: crane v0.22.1 (commit 8a72a424), the `debug` variant for its busybox shell. Digest-pinned:
#: tests/test_release_load.py fails on a tag-only reference, and there is no override.
LOADER_IMAGE = ("gcr.io/go-containerregistry/crane:debug"
                "@sha256:e78770b31258a3846f878036d9c1f63fbe4c871f9f56990bf77fd95c013e3c1b")
ENV_FILE_VAR = "OKENGINE_REGISTRY_ENV_FILE"
DEFAULT_ENV_FILE = "~/.config/okengine/registry.env"
CREDENTIAL_KEYS = ("OKENGINE_REGISTRY_USER", "OKENGINE_REGISTRY_TOKEN")
#: engine repository -> the release-images service name CI records it under. It must agree with
#: engine_images.ENGINE_DOCKERFILES, ci/candidate_wheel.DOCKERFILES, ci/release_images.SERVICES and
#: ci/release_record.PRODUCT; tests/test_release_load.py pins the agreement (okengine#1057 [5] [9]
#: [10]). It is not derived from ci/: a deploy host runs this with a bare python3.
CI_SERVICE = {
    "okengine-reader": "reader", "okengine-mcp": "okengine-mcp", "okengine-cockpit": "cockpit",
    "okengine-review-write": "review-write", "okengine-projection": "projection",
    "okengine-operation-runner": "operation-runner",
}
GATEWAY = "gateway"
# The password reaches crane on stdin from a shell builtin; argv carries only the registry host
# and the digest reference. HOME=/tmp: crane writes its login config there, inside the container.
_LOADER_SCRIPT = ('printf %s "$OKENGINE_REGISTRY_TOKEN" | crane auth login "$1" '
                  '-u "$OKENGINE_REGISTRY_USER" --password-stdin >/dev/null && '
                  'crane pull --insecure "$2" /dev/stdout')
_PREFLIGHT_SCRIPT = ('printf %s "$OKENGINE_REGISTRY_TOKEN" | crane auth login "$1" '
                     '-u "$OKENGINE_REGISTRY_USER" --password-stdin >/dev/null && '
                     'crane digest --insecure "$2"')
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_LOADED = re.compile(r"Loaded image(?: ID)?: (\S+)")


class LoadError(ValueError):
    """A FAIL: the deployment must not proceed onto unverified images."""


def _git(engine_dir: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(engine_dir), *args], capture_output=True,
                              text=True, check=True, timeout=60).stdout.strip()
    except (OSError, SubprocessError) as exc:
        raise LoadError(f"cannot read the engine revision in {engine_dir}: {exc}") from exc


def check_revision(data: Mapping, engine_dir: Path) -> None:
    # git walks up from -C: an engine dir that is not itself a checkout would report the HEAD of
    # whatever repository encloses it.
    top = _git(engine_dir, "rev-parse", "--show-toplevel")
    if Path(top).resolve() != engine_dir.resolve():
        raise LoadError(f"cannot read the engine revision in {engine_dir}: it is not a git checkout "
                        f"root (git resolved {top})")
    head = _git(engine_dir, "rev-parse", "HEAD")
    if data.get("commit_sha") != head:
        raise LoadError(f"tested-digest list is for commit {data.get('commit_sha')!r}, but the engine "
                        f"checkout is at {head}; deploy from a checkout of the tested release tag")
    if _git(engine_dir, "status", "--porcelain"):
        raise LoadError("the engine checkout has uncommitted changes; a release deploys a clean tag")


def required_images(pack: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """{engine repository -> CI service name} for every compose-built engine image this deployment
    runs. The gateway, which every deployment runs, is handled beside it."""
    env = engine_images._pack_env(pack, environ)
    services = engine_images.engine_services(engine_images.compose_files(pack, env))
    unmapped = sorted({repo for repo in services.values() if repo not in CI_SERVICE})
    if unmapped:
        raise LoadError(f"no CI release service is mapped for engine image(s) {', '.join(unmapped)}; "
                        "release_load.CI_SERVICE must name every engine repository")
    return {repo: CI_SERVICE[repo] for repo in services.values()}


def credential_file(environ: Mapping[str, str], forbidden: list[Path]) -> Path:
    raw = environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE
    path = Path(os.path.expanduser(raw)).resolve()
    name = f"{ENV_FILE_VAR} ({path})"
    try:
        info = path.stat()
    except OSError as exc:
        raise LoadError(f"registry credentials missing: {name} is not readable ({exc.strerror}); "
                        f"create it with {' and '.join(CREDENTIAL_KEYS)}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise LoadError(f"registry credentials: {name} is not a regular file")
    for root in forbidden:
        if path.is_relative_to(root.resolve()):
            raise LoadError(f"registry credentials: {name} is inside {root}; keep it host-local")
    if info.st_mode & 0o077:
        raise LoadError(f"registry credentials: {name} is readable by others; chmod 600 it")
    keys = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            keys[key.strip()] = bool(value.strip())
    absent = [key for key in CREDENTIAL_KEYS if not keys.get(key)]
    if absent:
        raise LoadError(f"registry credentials: {name} has no value for {', '.join(absent)}")
    return path


def _docker(*args: str) -> Optional[str]:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    return proc.stdout.strip() if proc.returncode == 0 else None


def local_id(reference: str) -> Optional[str]:
    return _docker("image", "inspect", "--format", "{{.Id}}", reference)


def registry_host(registry: str) -> str:
    """`host[:port]` or `[v6]:port` -> the host part."""
    if registry.startswith("["):
        return registry[1:].partition("]")[0]
    return registry.rpartition(":")[0] if registry.count(":") == 1 else registry


def host_mapping(registry: str) -> list[str]:
    """`["--add-host", "<name>:<address>"]` for a registry named by host name, resolved on THIS
    host (getaddrinfo honors /etc/hosts, which the loader container does not see); [] for an
    IP-literal registry. An IPv4 address is preferred: a container network often has no IPv6."""
    host = registry_host(registry)
    try:
        ipaddress.ip_address(host)
        return []
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise LoadError(f"cannot resolve the registry host {host!r} on this deploy host ({exc}); "
                        "add it to DNS or /etc/hosts") from exc
    addresses = [info[4][0] for info in infos]
    ipv4 = [address for address in addresses if "." in address and ":" not in address]
    return ["--add-host", f"{host}:{(ipv4 or addresses)[0]}"]


def loader_argv(image: str, env_file: Path, script: str) -> list[str]:
    """The loader's `docker run`: credentials by --env-file, the registry name mapped to the deploy
    host's address, and no socket, no mounts."""
    registry = image.split("/", 1)[0]
    return ["docker", "run", "--rm", "--env-file", str(env_file), "-e", "HOME=/tmp",
            *host_mapping(registry), "--entrypoint", "/busybox/sh", LOADER_IMAGE, "-c", script,
            "loader", registry, image]


def load(image: str, env_file: Path) -> tuple[str, Optional[str]]:
    """Stream `image` (repo@sha256:...) through the loader into `docker load`: (the loaded ID,
    crane's placeholder name for the caller to remove once real tags hold the image, or None)."""
    loader = subprocess.Popen(loader_argv(image, env_file, _LOADER_SCRIPT),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        loaded = subprocess.run(["docker", "load"], stdin=loader.stdout, capture_output=True,
                                text=True, timeout=3600)
    finally:
        loader.stdout.close()
        loader_err = loader.stderr.read().decode(errors="replace")
        loader.stderr.close()
        loader_rc = loader.wait(timeout=3600)
    if loader_rc != 0:
        raise LoadError(f"loader could not fetch {image}: {loader_err.strip()[-500:]}")
    if loaded.returncode != 0:
        raise LoadError(f"docker load failed for {image}: {loaded.stderr.strip()[-500:]}")
    match = _LOADED.search(loaded.stdout)
    if not match:
        raise LoadError(f"docker load reported no image for {image}")
    reference = match.group(1)
    if reference.startswith("sha256:"):
        return reference, None
    image_id = local_id(reference)
    if not image_id:
        raise LoadError(f"cannot read the image docker load reported for {image}")
    return image_id, (reference if reference.endswith(":i-was-a-digest") else None)


def verify_all(ci_services: set[str], images: Mapping, env_file: Path,
               placeholders: list[str]) -> dict[str, str]:
    """{CI service -> verified local image ID}; LoadError before anything is tagged. Appends each
    load's placeholder name to `placeholders`, to be removed only after tagging."""
    verified: dict[str, str] = {}
    for ci_service in sorted(ci_services):
        entry = images[ci_service]
        expected = entry["id"]
        image_id = local_id(expected)
        if not image_id:
            image_id, placeholder = load(entry["image"], env_file)
            if placeholder:
                placeholders.append(placeholder)
        if image_id != expected:
            raise LoadError(f"{ci_service}: loaded config ID {image_id} is not the tested {expected}")
        verified[ci_service] = image_id
    return verified


def reach(image: str, env_file: Path) -> None:
    """Prove the loader can log in to `image`'s registry and finds its manifest: `crane digest`
    must print the digest the list pins. LoadError otherwise, with the loader's own reason."""
    try:
        proc = subprocess.run(loader_argv(image, env_file, _PREFLIGHT_SCRIPT), capture_output=True,
                              text=True, timeout=300)
    except (OSError, SubprocessError) as exc:
        raise LoadError(f"loader preflight could not run for {image}: {exc}") from exc
    if proc.returncode != 0:
        raise LoadError(f"loader cannot reach the registry for {image}: "
                        f"{proc.stderr.strip()[-500:]}")
    found = _DIGEST.findall(proc.stdout)
    pinned = image.rpartition("@")[2]
    if not found or found[-1] != pinned:
        raise LoadError(f"loader preflight: the registry reports {found[-1] if found else 'no digest'}"
                        f" for {image}, not {pinned}")


def preflight(ci_services: set[str], images: Mapping, env_file: Path) -> list[str]:
    """Reach each registry once, through an image this deploy must fetch; images already present
    by config ID need no registry (a rollback onto local images must not depend on it). Returns the
    images probed."""
    probed: dict[str, str] = {}
    for ci_service in sorted(ci_services):
        entry = images[ci_service]
        registry = entry["image"].split("/", 1)[0]
        if registry not in probed and not local_id(entry["id"]):
            probed[registry] = entry["image"]
    for image in probed.values():
        reach(image, env_file)
    return sorted(probed.values())


def _checked(list_path: Path, pack: Path, engine_dir: Path,
             environ: Mapping[str, str]) -> tuple[dict, dict[str, str], set[str], Path]:
    """Everything that needs no registry and moves no tag: (list, required images, CI services,
    credential file); LoadError otherwise."""
    try:
        data = json.loads(list_path.read_text(encoding="utf-8"))
        tested_digests.load(list_path)
    except (OSError, ValueError) as exc:
        raise LoadError(f"tested-digest list unusable: {exc}") from exc
    check_revision(data, engine_dir)
    needed = required_images(pack, environ)
    ci_services = set(needed.values()) | {GATEWAY}
    absent = sorted(ci for ci in ci_services if ci not in data["images"])
    if absent:
        raise LoadError(f"tested-digest list has no CI-tested image for: {', '.join(absent)}")
    env_file = credential_file(environ, [engine_dir, pack])
    return data, needed, ci_services, env_file


def run_preflight(list_path: Path, pack: Path, engine_dir: Path,
                  environ: Mapping[str, str]) -> list[str]:
    data, _needed, ci_services, env_file = _checked(list_path, pack, engine_dir, environ)
    return preflight(ci_services, data["images"], env_file)


def run(list_path: Path, pack: Path, engine_dir: Path, gateway_image: str, engine_tag: str,
        environ: Mapping[str, str]) -> dict[str, str]:
    data, needed, ci_services, env_file = _checked(list_path, pack, engine_dir, environ)
    placeholders: list[str] = []
    verified = verify_all(ci_services, data["images"], env_file, placeholders)
    targets = {f"{repo}:{engine_tag}": ci_service for repo, ci_service in needed.items()}
    targets[gateway_image] = GATEWAY
    tags = {}
    for target, ci_service in sorted(targets.items()):
        if _docker("tag", verified[ci_service], target) is None:
            raise LoadError(f"cannot tag {verified[ci_service]} as {target}")
        tags[target] = verified[ci_service]
    # Every verified image now holds a real tag, so untagging the placeholder only untags. Its
    # failure is harmless (a stray name on the tested bytes), so it does not fail the deploy.
    for placeholder in placeholders:
        _docker("rmi", placeholder)
    return tags


def main(argv: Optional[list[str]] = None, environ: Optional[Mapping[str, str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("list", type=Path)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--engine-dir", type=Path, required=True)
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--engine-tag", required=True)
    parser.add_argument("--preflight", action="store_true",
                        help="check the list, credentials and registry reachability; load nothing")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    if args.preflight:
        try:
            probed = run_preflight(args.list, args.pack, args.engine_dir, environ)
        except (LoadError, ValueError, OSError) as exc:
            print(f"release-digests: FAIL — {exc}", file=sys.stderr)
            return 1
        print(f"release-digests: preflight passed; registry reachable for {len(probed)} image(s) "
              "to fetch" if probed else
              "release-digests: preflight passed; every tested image is already present locally")
        return 0
    try:
        tags = run(args.list, args.pack, args.engine_dir, args.gateway_image, args.engine_tag, environ)
    except (LoadError, ValueError, OSError) as exc:
        print(f"release-digests: FAIL — {exc}", file=sys.stderr)
        return 1
    for target, image_id in sorted(tags.items()):
        print(f"  loaded {target} = {image_id[:19]}")
    print(f"release-digests: {len(tags)} image reference(s) pinned to CI-tested digests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
