#!/usr/bin/env python3
"""tested_digests.py — prove every running engine image is one CI tested (okengine#1048).

CI's `release-images` lane builds the release images once, pushes them to the project registry and
runs the smoke/e2e/resilience/performance lanes against those exact digests. It records them in
`artifacts/release-images/tested-digests.json` (ci/release_images.py). Until then nothing compared
that record with what a host runs, and every host image was a host-side rebuild: the v0.15.13
gateway (hermes-agent:okengine-v0.15.13-faaf5f08, sha256:b9dae488) never ran in CI at all.

This check runs from the deployment dir (post_deploy_verify.sh, deploy.sh step 6). For each running
container of the compose project whose image is an engine image, it accepts the image when CI
tested it:

* its image ID is a config digest CI recorded (the classic image store, where a pulled image's ID
  is its config digest), or a manifest digest CI recorded (the containerd store, where it is the
  manifest digest); or
* one of its RepoDigests is a manifest digest CI pushed (an image pulled from the release registry).

An engine image is one whose repository's last path segment is an engine-built repository or
`hermes-agent`, the gateway repository named by OKENGINE_GATEWAY_IMAGE, or a repository the list
itself names. Anything else (postgres) is SKIPPED and named, never silently dropped.

Exit status:
  0  every running engine container runs an image CI tested
  1  FAIL: an engine container runs an untested image, or the supplied list is unreadable or
     malformed (a broken record vouches for nothing)
  2  UNDETECTABLE, never a pass: no list supplied (--list or OKENGINE_TESTED_DIGESTS; a FAIL
     instead under --require / OKENGINE_REQUIRE_TESTED_DIGESTS=1), docker could not be asked, an image's digests could not be read, or no engine container is running

Standard library only, so it runs on a bare deploy-host python3. ENGINE_REPOSITORIES and
repository() mirror scripts/engine_images.py; tests/test_tested_digests.py pins the agreement.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from subprocess import SubprocessError
from typing import NamedTuple

ENV_VAR = "OKENGINE_TESTED_DIGESTS"
#: Exported by `deploy.sh --release-digests` (okengine#1048 phase 3): a deployment rolled from a
#: release's tested digests must prove it, so there a missing list is a FAIL, not UNDETECTABLE.
REQUIRE_VAR = "OKENGINE_REQUIRE_TESTED_DIGESTS"
SCHEMA = 1
ENGINE_REPOSITORIES = frozenset({
    "okengine-reader", "okengine-mcp", "okengine-review-write", "okengine-operation-runner",
    "okengine-cockpit", "okengine-projection",
})
GATEWAY_REPOSITORIES = frozenset({"hermes-agent"})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_INSPECT = '{{index .Config.Labels "com.docker.compose.service"}} {{.Image}} {{.Config.Image}}'

Runner = Callable[[list[str]], "str | None"]


class Tested(NamedTuple):
    ids: frozenset[str]
    digests: frozenset[str]
    repositories: frozenset[str]
    pipeline_id: str
    commit_sha: str


def _run(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def repository(reference: str) -> str:
    """`name[:tag][@digest]` -> `name`, keeping a registry port (`host:5000/name`)."""
    name = reference.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return name[: len(name) - len(last)] + last.split(":", 1)[0]


def load(path: Path) -> Tested:
    """The tested-digest list ci/release_images.py writes; ValueError when it vouches for nothing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read tested-digest list {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a schema-{SCHEMA} tested-digest list")
    images = data.get("images")
    if not isinstance(images, dict) or not images:
        raise ValueError(f"{path} lists no tested images")
    ids, digests, repositories = set(), set(), set()
    for service, entry in images.items():
        image = entry.get("image") if isinstance(entry, dict) else None
        image_id = entry.get("id") if isinstance(entry, dict) else None
        name, _, digest = image.partition("@") if isinstance(image, str) else ("", "", "")
        if not (name and _DIGEST.fullmatch(digest) and isinstance(image_id, str)
                and _DIGEST.fullmatch(image_id)):
            raise ValueError(f"{path}: {service!r} is not a repository@sha256 image with a sha256 id")
        ids.add(image_id)
        digests.add(digest)
        repositories.add(name)
    return Tested(frozenset(ids), frozenset(digests), frozenset(repositories),
                  str(data.get("pipeline_id", "?")), str(data.get("commit_sha", "?")))


def collect(run: Runner) -> list[tuple[str, str, str]] | None:
    """(service, image id, created-from reference) per container of the compose project in the
    current directory; None when docker could not be asked or answered something unparseable."""
    ids = run(["docker", "compose", "ps", "-q"])
    if ids is None:
        return None
    containers = ids.split()
    if not containers:
        return []
    out = run(["docker", "inspect", "--format", _INSPECT, *containers])
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            return None
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def repo_digests(image_id: str, run: Runner) -> list[str] | None:
    """The manifest digests an image was pulled or pushed as; None when they cannot be read."""
    out = run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_id])
    if out is None:
        return None
    try:
        values = json.loads(out)
    except ValueError:
        return None
    # A locally built image has no registry digest: `[]`, or `null` for a nil list. Both mean
    # "never pushed or pulled", which is an answer (untested), not an unreadable image.
    if values is None:
        return []
    if not isinstance(values, list):
        return None
    return [value.partition("@")[2] for value in values if isinstance(value, str)]


def is_engine(reference: str, tested: Tested, environ: Mapping[str, str]) -> bool:
    repo = repository(reference)
    gateway = environ.get("OKENGINE_GATEWAY_IMAGE", "")
    return (repo.rsplit("/", 1)[-1] in ENGINE_REPOSITORIES | GATEWAY_REPOSITORIES
            or repo in tested.repositories or (bool(gateway) and repo == repository(gateway)))


def judge(rows: list[tuple[str, str, str]], tested: Tested, run: Runner,
          environ: Mapping[str, str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"tested": [], "untested": [], "unreadable": [], "skipped": []}
    for service, image_id, reference in sorted(rows):
        label = f"{service} ({reference} = {image_id[:19]})"
        if not is_engine(reference, tested, environ):
            result["skipped"].append(f"{service} ({reference}): not an engine image")
            continue
        if image_id in tested.ids or image_id in tested.digests:
            result["tested"].append(label)
            continue
        digests = repo_digests(image_id, run)
        if digests is None:
            result["unreadable"].append(label)
        elif tested.digests.intersection(digests):
            result["tested"].append(label)
        else:
            result["untested"].append(label)
    return result


def main(argv: list[str] | None = None, run: Runner | None = None,
         environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="running engine images vs CI's tested digests")
    parser.add_argument("--list", default=environ.get(ENV_VAR, ""),
                        help=f"tested-digests.json from the release pipeline (default: ${ENV_VAR})")
    parser.add_argument("--require", action="store_true", default=environ.get(REQUIRE_VAR) == "1",
                        help=f"a missing list is a FAIL (default: ${REQUIRE_VAR}=1)")
    args = parser.parse_args(argv)
    if not args.list and args.require:
        print(f"tested digests: FAIL — no tested-digest list supplied, but this deployment was rolled "
              f"with --release-digests ({REQUIRE_VAR}=1)", file=sys.stderr)
        return 1
    if not args.list:
        print(f"tested digests: UNDETECTABLE — no tested-digest list supplied (--list or {ENV_VAR}); "
              "nothing proves the running images are the ones CI tested", file=sys.stderr)
        return 2
    try:
        tested = load(Path(args.list))
    except ValueError as exc:
        print(f"tested digests: FAIL — {exc}", file=sys.stderr)
        return 1
    # `_run` looked up now, not bound as a default: the docker boundary stays replaceable.
    run = run or _run
    rows = collect(run)
    if rows is None:
        print("tested digests: UNDETECTABLE — docker compose ps/inspect could not be read",
              file=sys.stderr)
        return 2
    result = judge(rows, tested, run, environ)
    engine = len(result["tested"]) + len(result["untested"]) + len(result["unreadable"])
    for kind in ("untested", "unreadable", "skipped", "tested"):
        for item in result[kind]:
            print(f"  {kind.upper()} {item}")
    if not engine:
        print("tested digests: UNDETECTABLE — no running engine container in this compose project",
              file=sys.stderr)
        return 2
    print(f"tested digests: {len(result['tested'])} of {engine} running engine container(s) run an "
          f"image CI tested (pipeline {tested.pipeline_id}, commit {tested.commit_sha[:12]})")
    if result["untested"]:
        return 1
    return 2 if result["unreadable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
