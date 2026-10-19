#!/usr/bin/env python3
"""engine_images.py — immutable, deployment-scoped tags for the compose-built engine images.

okengine#1033 / v0.15.11 audit [27]. The pack compose builds six engine images from ENGINE_DIR
(reader, read-MCP, review-write, operation-runner, cockpit, projection) and used to name them with
bare repositories (`image: okengine-reader`), that is `:latest`. The Docker image store is global
to the daemon, so every co-installed pack shared those six tags:

* an rc canary, a one-pack deploy from another checkout, or a local smoke build moved the tag, and
  every OTHER pack's next `docker compose up -d` (a recreate, a reboot) silently recreated its
  containers onto that code, with no deploy of its own;
* even a fleet roll from ONE checkout made them disagree: Compose stamps the project name into the
  labels of every image it builds, so five packs built the same source into five different image
  IDs under one tag (measured on the v0.15.11 roll: same `Created`, five reader IDs). After the
  roll, four of the five ran an image their own tag no longer named.

The gateway already avoided this: deploy.sh pins `hermes-agent:okengine-<release>-<sha>` through
`OKENGINE_GATEWAY_IMAGE`. This does the same for the sibling images, plus the deployment, because
their builds are per-project (above):

    <repo>:okengine-<release>-<sha>[-dirty].<compose project>

deploy.sh persists it as OKENGINE_ENGINE_IMAGE_TAG in the pack `.env` and adds one `image:` line
per engine-built service to the engine-generated `docker-compose.okengine-image.yml`, so existing
packs (whose docker-compose.yml still says `image: okengine-reader`) move onto the scoped tag on
their next deploy.sh without editing the pack's own compose file. The override names ONLY services
the deployment already defines: an override entry for an absent service would create it.

`drift` is the post-deploy check for the other half of the hazard: a running container whose image
ID is not what its compose image reference resolves to now. That is a tag moved under a running
container, and its next recreate changes the code it runs. It also reports (okengine#1042):

* SHARED [12]: an engine image whose tag is not this deployment's scoped tag (`:unpinned`, `:latest`,
  another project's tag). The tag resolves fine, so the ID comparison alone calls it in sync.
* UNATTRIBUTED [14]: an engine image deploy.sh did not build. The override stamps
  `org.okengine.engine_image_tag` from OKENGINE_ENGINE_BUILD_TAG, which only deploy.sh exports, so a
  bare `docker compose up` that rebuilds a pruned image from whatever ENGINE_DIR holds now carries
  `unattributed` instead of the tag it claims.
* services absent from `docker compose config` [24] (a profile started by hand) are judged by
  the image reference their own container was created from, not skipped.

`claim` [8] refuses a deploy whose compose project already belongs to another directory: the
project name (and so the tag) is the directory basename unless COMPOSE_PROJECT_NAME says
otherwise, and two deployments that share one would recreate each other's containers.
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

import yaml
from subprocess import SubprocessError
from yaml import YAMLError

sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
from dotenv_lib import read_compose_env  # noqa: E402  (sibling lib, not a package)

#: Dockerfile (relative to ENGINE_DIR) -> the image repository it builds.
ENGINE_DOCKERFILES = {
    "okengine-reader/Dockerfile": "okengine-reader",
    "okengine-mcp/Dockerfile": "okengine-mcp",
    "okengine-mcp/Dockerfile.review": "okengine-review-write",
    "okengine-operations/Dockerfile": "okengine-operation-runner",
    "okengine-cockpit/Dockerfile": "okengine-cockpit",
    "okengine-projection/Dockerfile": "okengine-projection",
}
ENGINE_REPOSITORIES = frozenset(ENGINE_DOCKERFILES.values())
TAG_VAR = "OKENGINE_ENGINE_IMAGE_TAG"
#: Exported by deploy.sh for its own `compose up --build` only, never persisted to `.env`.
BUILD_VAR = "OKENGINE_ENGINE_BUILD_TAG"
LABEL = "org.okengine.engine_image_tag"
UNATTRIBUTED = "unattributed"
#: A Dockerfile path shaped like an engine image's; one missing from ENGINE_DOCKERFILES is refused.
_ENGINE_DOCKERFILE = re.compile(r"okengine-[^/]+/Dockerfile(\.[^/]+)?")
OVERRIDE = "docker-compose.okengine-image.yml"
SIDECARS = ".okengine/generated/sidecars.compose.yml"
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
#: A git revision as deploy.sh reports it: a (short) hex sha, `-dirty` for an uncommitted tree.
_SHA = re.compile(r"[0-9a-f]{4,40}(-dirty)?")


def declared_name(pack: Path, env: Mapping[str, str]) -> str | None:
    """The top-level `name:` the deployment's compose files declare (a later file overrides an
    earlier one, as Compose merges them), or None. Files that are absent or unreadable declare
    nothing here: Compose itself refuses a missing COMPOSE_FILE entry. An interpolated name is
    refused rather than guessed (okengine#1044 [26])."""
    separator = env.get("COMPOSE_PATH_SEPARATOR") or ":"
    listed = env.get("COMPOSE_FILE")
    parts = ([p for p in listed.split(separator) if p] if listed
             else ["docker-compose.yml", "docker-compose.override.yml"])
    found = None
    for part in parts:
        path = Path(part).expanduser()
        path = path if path.is_absolute() else pack / path
        try:
            doc = load_compose(path)
        except (OSError, UnicodeDecodeError, YAMLError):
            continue
        value = doc.get("name") if isinstance(doc, dict) else None
        if value not in (None, ""):
            found = str(value)
    if found is not None and "$" in found:
        raise ValueError(f"the compose `name:` {found!r} is interpolated; set COMPOSE_PROJECT_NAME "
                         "in the pack .env so the project (and its image tag) can be derived")
    return found


def project_name(pack: Path, env: Mapping[str, str]) -> str:
    """The Compose project this deployment runs as, in Compose's order: COMPOSE_PROJECT_NAME, the
    compose files' top-level `name:`, then the directory name as spelled (Compose does not resolve
    a symlinked project directory). Normalized the way Compose normalizes it (lowercase; only a-z,
    0-9, `_` and `-`). okengine#1044 [26]: this read only the env and the RESOLVED name, so a
    `name:` shared by two packs, or a link named differently from its target, made the claim and
    the scoped tag name a project other than the one Compose runs."""
    raw = (env.get("COMPOSE_PROJECT_NAME") or declared_name(pack, env)
           or Path(os.path.abspath(pack)).name)
    name = re.sub(r"[^a-z0-9_-]", "", raw.lower()).lstrip("_-")
    if not name:
        raise ValueError(f"cannot derive a compose project name for {pack}")
    return name


def image_tag(release: str, sha: str, project: str) -> str:
    """`okengine-<release>-<sha>.<project>`: immutable per revision, never shared across packs."""
    if not _SHA.fullmatch(sha):
        # `unknown` (git rev-parse failed) would be shared by every unattributable engine tree.
        raise ValueError(f"not a git revision: {sha!r}; an engine image tag must name its source")
    tag = f"okengine-{release}-{sha}.{project}"
    if not release or not _TAG.fullmatch(tag):
        raise ValueError(f"not a valid immutable image tag: {tag!r}")
    return tag


def compose_files(pack: Path, env: Mapping[str, str]) -> list[Path]:
    """The compose files `docker compose` reads for this pack, minus the engine override itself and
    the sidecar override (never part of the default project)."""
    separator = env.get("COMPOSE_PATH_SEPARATOR") or ":"
    parts = [p for p in (env.get("COMPOSE_FILE") or "docker-compose.yml").split(separator) if p]
    skip = {(pack / OVERRIDE).resolve(), (pack / SIDECARS).resolve()}
    out = []
    for part in parts:
        path = Path(part).expanduser()
        path = (path if path.is_absolute() else pack / path).resolve()
        if path in skip:
            continue
        if not path.is_file():
            # Compose itself refuses a missing COMPOSE_FILE entry; an override built without it
            # would leave that file's engine services on whatever tag it names.
            raise ValueError(f"COMPOSE_FILE names {part!r}, which does not exist ({path})")
        out.append(path)
    return out


class _ComposeLoader(yaml.SafeLoader):
    """Compose merge tags (`!reset`, `!override`) carry no image data; load them as plain nodes."""


def _plain(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _plain)


def repository(reference: str) -> str:
    """`name[:tag][@digest]` -> `name`, keeping a registry port (`host:5000/name`)."""
    name = reference.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return name[: len(name) - len(last)] + last.split(":", 1)[0]


def _dockerfile_repository(build) -> str | None:
    """The engine repository a compose `build:` produces, or None for a pack-owned build."""
    if isinstance(build, dict):
        context = str(build.get("context") or ".")
        dockerfile = str(build.get("dockerfile") or "Dockerfile")
    else:
        context, dockerfile = str(build), "Dockerfile"
    dockerfile = dockerfile.removeprefix("./")
    candidates = (dockerfile, f"{Path(context).name}/{dockerfile}")
    for candidate in candidates:
        if candidate in ENGINE_DOCKERFILES:
            return ENGINE_DOCKERFILES[candidate]
    for candidate in candidates:
        if _ENGINE_DOCKERFILE.fullmatch(candidate):
            # okengine#1042 [25]: an engine Dockerfile this map does not know would build onto the
            # shared `:latest` tag with no override entry. Refuse rather than leave it unpinned.
            raise ValueError(f"{candidate} looks like an engine Dockerfile but is not in "
                             "engine_images.ENGINE_DOCKERFILES; add it there")
    return None


def load_compose(path: Path) -> object:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)  # nosec B506 - SafeLoader subclass


def engine_services(paths: list[Path]) -> dict[str, str]:
    """engine_services_in() over these compose files, in Compose's merge order."""
    return engine_services_in([load_compose(path) for path in paths])


def built_services(paths: list[Path]) -> set[str]:
    """Services with a `build:` in any of these compose files: the ones whose image the override
    can label. A `build:` entry for an image-only service would create a build that is not there."""
    out: set[str] = set()
    for doc in (load_compose(path) for path in paths):
        services = doc.get("services") if isinstance(doc, dict) else None
        out |= {name for name, spec in (services or {}).items()
                if isinstance(spec, dict) and "build" in spec}
    return out


def engine_services_in(docs: list[object]) -> dict[str, str]:
    """{service -> engine repository} for every service these compose documents build from
    ENGINE_DIR, or that names an engine repository directly. A later `build:` wins, as Compose
    merges it."""
    builds: dict[str, object] = {}
    images: dict[str, str] = {}
    for doc in docs:
        services = doc.get("services") if isinstance(doc, dict) else None
        for name, spec in (services or {}).items():
            if not isinstance(spec, dict):
                continue
            if "build" in spec:
                builds[name] = spec["build"]
            if spec.get("image"):
                images[name] = str(spec["image"])
    found = {}
    for name in sorted(set(builds) | set(images)):
        repo = _dockerfile_repository(builds[name]) if name in builds else None
        if repo is None and repository(images.get(name, "")) in ENGINE_REPOSITORIES:
            repo = repository(images[name])
        if repo:
            found[name] = repo
    return found


def render_override(services: Mapping[str, str], built: frozenset[str] | set[str] = frozenset()) -> str:
    """The `services:` entries (two-space indent) that pin each engine service to the scoped tag.
    `:?` so a bare compose run without the pin fails instead of building `:latest`. A built service
    also gets the provenance label (okengine#1042 [14]); Compose leaves `build` out of its config
    hash, so the label never recreates a container by itself."""
    lines = []
    for name, repo in sorted(services.items()):
        lines += [f"  {name}:",
                  f"    image: {repo}:${{{TAG_VAR}:?deploy.sh must pin an immutable engine image tag}}"]
        if name in built:
            lines += ["    build:", "      labels:",
                      f"        {LABEL}: ${{{BUILD_VAR}:-{UNATTRIBUTED}}}"]
    return "".join(line + "\n" for line in lines)


# --- drift: running container vs what its compose image reference resolves to now ---------------

Runner = Callable[..., "str | None"]   # (argv, cwd=None) -> stdout, or None on any failure


def _run(args: list[str], cwd: str | None = None) -> str | None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60, cwd=cwd)
    except (OSError, SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


_SCOPED_TAG = re.compile(
    r"okengine-.+-(?P<sha>[0-9a-f]{4,40})(-dirty)?\.(?P<project>[a-z0-9][a-z0-9_-]*)")
#: The revision label CI stamps on every release image it builds (ci/release_images.py).
REVISION_LABEL = "org.opencontainers.image.revision"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def tag_of(reference: str) -> str:
    """`name[:tag][@digest]` -> `tag`, or "" for none (`:latest` implied)."""
    return reference.partition("@")[0][len(repository(reference)):].removeprefix(":")


def is_scoped(reference: str, project: str | None = None) -> bool:
    """True for `<repo>:okengine-<release>-<sha>.<project>`, of THIS project when one is given."""
    match = _SCOPED_TAG.fullmatch(tag_of(reference))
    return bool(match) and (project is None or match["project"] == project)


def attributed(reference: str, target: tuple) -> bool:
    """Whether the image a reference resolves to has provenance for it: deploy.sh's build label
    naming this tag, or (okengine#1048) CI's revision label for the commit the tag or digest pins,
    which is what `deploy.sh --release-digests` loads."""
    label = target[1]
    revision = target[2] if len(target) > 2 else ""
    tag = tag_of(reference)
    if label and label == tag:
        return True
    if not _FULL_SHA.fullmatch(revision):
        return False
    if "@sha256:" in reference:
        return True
    match = _SCOPED_TAG.fullmatch(tag)
    return bool(match) and not tag.endswith(f"-dirty.{match['project']}") \
        and revision.startswith(match["sha"])


def compare(configured: Mapping[str, str], running: Mapping[str, str],
            resolve: Callable[[str], "tuple[str, ...] | None"],
            created: Mapping[str, str] | None = None,
            project: str | None = None) -> dict[str, list[str]]:
    """Classify each running service. `drifted`: its tag now names another image. `unresolved`:
    the tag names no image at all, so the next recreate rebuilds or fails. `unpinned`: no image
    reference at all, in the config or on the container. `unconfigured`: absent from the compose
    config (a hand-started profile), judged by its container's own reference instead. `shared`: an
    engine image on a tag that is not this deployment's scoped tag. `unattributed`: an engine image
    whose provenance label does not name the tag it runs under, so deploy.sh did not build it."""
    result: dict[str, list[str]] = {"in_sync": [], "drifted": [], "unresolved": [], "unpinned": [],
                                    "unconfigured": [], "shared": [], "unattributed": []}
    created = created or {}
    resolved: dict[str, tuple[str, str] | None] = {}
    for service, image_id in sorted(running.items()):
        ref = configured.get(service)
        if not ref:
            ref = created.get(service)
            if not ref:
                result["unpinned"].append(service)
                continue
            result["unconfigured"].append(f"{service} ({ref})")
        if ref not in resolved:
            resolved[ref] = resolve(ref)
        target = resolved[ref]
        if target is None:
            result["unresolved"].append(f"{service} ({ref})")
            continue
        if target[0] != image_id:
            result["drifted"].append(f"{service} ({ref} -> {target[0][:19]}, running {image_id[:19]})")
        else:
            result["in_sync"].append(service)
        if repository(ref) not in ENGINE_REPOSITORIES:
            continue
        # okengine#1048: a digest reference is immutable, so no other deployment can move it.
        if "@sha256:" not in ref and not is_scoped(ref, project):
            result["shared"].append(f"{service} ({ref})")
        elif not attributed(ref, target):
            result["unattributed"].append(f"{service} ({ref}, {LABEL}={target[1] or 'absent'})")
    return result


def collect(run: Runner = _run) -> tuple[dict[str, str], dict[str, str], dict[str, str]] | None:
    """(configured service -> image ref, running service -> image id, running service -> the
    reference its container was created from) for the compose project in the current directory,
    in three docker calls; None when either side cannot be read."""
    config = run(["docker", "compose", "config", "--format", "json"])
    ids = run(["docker", "compose", "ps", "-q"])
    if config is None or ids is None:
        return None
    try:
        services = json.loads(config).get("services") or {}
    except (ValueError, AttributeError):
        return None
    configured = {name: str(spec["image"]) for name, spec in services.items()
                  if isinstance(spec, dict) and spec.get("image")}
    running: dict[str, str] = {}
    created: dict[str, str] = {}
    containers = ids.split()
    if containers:
        out = run(["docker", "inspect", "--format",
                   '{{index .Config.Labels "com.docker.compose.service"}} {{.Image}} {{.Config.Image}}',
                   *containers])
        if out is None:
            return None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) in (2, 3):
                service, image_id, *ref = parts
                running[service] = image_id
                created.update((service, item) for item in ref)
    return configured, running, created


def resolver(run: Runner = _run) -> Callable[[str], "tuple[str, str, str] | None"]:
    def resolve(ref: str) -> tuple[str, str, str] | None:
        # `with`: an image with no labels at all (postgres) has a nil map, and `index` of nil
        # fails the whole inspect, which would read as an unresolved reference.
        out = run(["docker", "image", "inspect", "--format",
                   f'{{{{.Id}}}}|{{{{with .Config.Labels}}}}{{{{index . "{LABEL}"}}}}|'
                   f'{{{{index . "{REVISION_LABEL}"}}}}{{{{end}}}}', ref])
        parts = [part.strip() for part in (out or "").strip().split("|")]
        if not parts[0]:
            return None
        # `index` of a missing label prints `<no value>`: that is an absent label, not a value.
        labels = [value if value != "<no value>" else "" for value in parts[1:3]]
        labels += [""] * (2 - len(labels))
        return parts[0], labels[0], labels[1]
    return resolve


def logical_cwd() -> Path:
    """The working directory as the shell spells it: $PWD when it names this directory (a
    symlinked deployment keeps its link name, as Compose sees it), else the physical one."""
    physical = Path.cwd()
    spelled = os.environ.get("PWD")
    try:
        if spelled and os.path.isabs(spelled) and os.path.samefile(spelled, physical):
            return Path(spelled)
    except OSError:
        pass
    return physical


def _current_project(environ: Mapping[str, str]) -> str | None:
    """This deployment's compose project (drift runs from the deployment dir), else None."""
    cwd = logical_cwd()
    try:
        return project_name(cwd, _pack_env(cwd, environ))
    except (ValueError, OSError):
        return None


#: Every finding kind `drift` prints, one `  <KIND> <item>` line each, and the ones that make it exit 1.
#: post_deploy_verify.sh quotes every such line in its WARN rather than a hand-typed list of kinds,
#: so a kind added here is named there too (okengine#1044 [17]).
REPORTED_KINDS = ("drifted", "unresolved", "unpinned", "shared", "unattributed", "unconfigured")
FAILING_KINDS = ("drifted", "unresolved", "unpinned", "shared", "unattributed")


def drift_main(run: Runner = _run, environ: Mapping[str, str] | None = None) -> int:
    """0 in sync, 1 drift found, 2 undetectable (nothing could be read: not a pass)."""
    collected = collect(run)
    if collected is None:
        print("image tag drift: UNDETECTABLE — docker compose config/ps/inspect could not be read",
              file=sys.stderr)
        return 2
    configured, running, created = collected
    if not running:
        print("image tag drift: UNDETECTABLE — no running containers in this compose project",
              file=sys.stderr)
        return 2
    project = _current_project(os.environ if environ is None else environ)
    result = compare(configured, running, resolver(run), created, project)
    print(f"image tag drift: {len(result['in_sync'])} of {len(running)} running service(s) match "
          f"their image reference")
    for kind in REPORTED_KINDS:
        for item in result[kind]:
            print(f"  {kind.upper()} {item}")
    return 1 if any(result[kind] for kind in FAILING_KINDS) else 0


# --- claim: one compose project, one deployment directory (okengine#1042 [8]) -----------------

def project_owners(project: str, run: Runner = _run) -> list[str] | None:
    """The working directories of every container (running or not) in this compose project, or
    None when docker cannot be asked."""
    out = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
               "--format", '{{.Label "com.docker.compose.project.working_dir"}}'])
    if out is None:
        return None
    return sorted({line.strip() for line in out.splitlines()})


def foreign_owners(pack: Path, owners: list[str]) -> list[str]:
    """Owners that are not this directory. Both sides are resolved: one deployment reached through
    a symlinked parent records either spelling. An empty label cannot be attributed: foreign."""
    mine = pack.resolve()
    return [owner for owner in owners if not owner or Path(owner).resolve() != mine]


def _cmd_claim(args, environ, run: Runner = _run) -> int:
    pack = Path(args.pack)
    project = project_name(pack, _pack_env(pack, environ))
    owners = project_owners(project, run)
    if owners is None:
        print(f"engine_images: UNDETECTABLE — cannot list compose project {project!r}'s containers; "
              "a second deployment sharing it would go unnoticed", file=sys.stderr)
        return 2
    foreign = foreign_owners(pack, owners)
    if foreign:
        print(f"engine_images: compose project {project!r} already belongs to "
              f"{', '.join(repr(o or '<unlabeled>') for o in foreign)}, not {str(pack.resolve())!r}. "
              "Deploying here would recreate that deployment's containers onto this directory and "
              "share its engine image tags. Set a unique COMPOSE_PROJECT_NAME in this pack's .env.",
              file=sys.stderr)
        return 1
    return 0


# --- ports: host ports another deployment already publishes (okengine#1044 [25]) ----------------

_WILDCARD = {"", "0.0.0.0", "::"}  # nosec B104 - compared with published addresses, never bound
#: One published binding in `docker ps`'s Ports column: `[ip:]host[-host_end]->target[...]/proto`.
_PUBLISHED = re.compile(r"(?:(?P<ip>\[[0-9a-fA-F:]*\]|[0-9.]+):)?(?P<lo>\d+)(?:-(?P<hi>\d+))?"
                        r"->[^/]+/(?P<proto>\w+)")


def planned_ports(config: object) -> list[tuple[str, str, int, str]] | None:
    """(service, host_ip, port, protocol) this deployment publishes, from `docker compose config
    --format json` (interpolated and profile-filtered by Compose itself), or None when that is not
    a compose model. A port Compose leaves empty is ephemeral and cannot collide."""
    if not isinstance(config, dict) or not isinstance(config.get("services"), dict):
        return None
    out = []
    for service, spec in sorted(config["services"].items()):
        for port in (spec or {}).get("ports") or []:
            published = str(port.get("published") or "")
            lo, _, hi = published.partition("-")
            if not lo.isdigit():
                continue
            for number in range(int(lo), int(hi or lo) + 1):
                out.append((service, str(port.get("host_ip") or ""), number,
                            str(port.get("protocol") or "tcp")))
    return out


def bound_ports(ps: str) -> list[tuple[str, str, str, int, str]]:
    """(project, container, host_ip, port, protocol) every running container publishes, from
    `docker ps --format '{{.Label "com.docker.compose.project"}}\t{{.Names}}\t{{.Ports}}'`."""
    out = []
    for line in ps.splitlines():
        project, _, rest = line.partition("\t")
        name, _, ports = rest.partition("\t")
        for match in _PUBLISHED.finditer(ports):
            ip = (match["ip"] or "").strip("[]")
            lo, hi = int(match["lo"]), int(match["hi"] or match["lo"])
            out += [(project, name, ip, number, match["proto"]) for number in range(lo, hi + 1)]
    return out


def _overlap(a: str, b: str) -> bool:
    """Two host addresses contend for a port when either is a wildcard or they are the same."""
    return a in _WILDCARD or b in _WILDCARD or a == b


def port_conflicts(planned, bound, project: str) -> list[str]:
    """Each planned binding a container of ANOTHER project publishes now. This deployment's own
    containers hold its ports until Compose recreates them, so they are not a conflict."""
    found = {f"{service} wants {ip or '*'}:{port}/{proto}, held by {name or '<unnamed>'} "
             f"(compose project {owner or '<none>'!r})": None
             for service, ip, port, proto in planned
             for owner, name, bip, bport, bproto in bound
             if owner != project and bport == port and bproto == proto and _overlap(ip, bip)}
    return list(found)   # one line per holder: a container bound on IPv4 and IPv6 is one holder


def _cmd_ports(args, environ, run: Runner = _run) -> int:
    pack = Path(args.pack)
    project = project_name(pack, _pack_env(pack, environ))
    # Run IN the pack, as deploy.sh's own compose calls do: --project-directory does not move
    # where Compose looks for compose files, so from the deployer's cwd it found none and the
    # check exited UNDETECTABLE for `deploy.sh /path/to/pack` (v0.15.14 audit).
    config = run(["docker", "compose", "--project-directory", str(pack), "config", "--format",
                  "json"], cwd=str(pack))
    ps = run(["docker", "ps", "--format",
              '{{.Label "com.docker.compose.project"}}\t{{.Names}}\t{{.Ports}}'])
    try:
        planned = planned_ports(json.loads(config)) if config is not None else None
    except ValueError:
        planned = None
    if planned is None or ps is None:
        print("engine_images: UNDETECTABLE — cannot read this deployment's published ports or the "
              "host's bound ones; a port another deployment holds would surface only as a failed "
              "`docker compose up`", file=sys.stderr)
        return 2
    conflicts = port_conflicts(planned, bound_ports(ps), project)
    for conflict in conflicts:
        print(f"engine_images: host port conflict: {conflict}", file=sys.stderr)
    if conflicts:
        print("engine_images: `docker compose up` would fail part-way with `port is already "
              "allocated`. Give this pack a free port_offset (framework pull --port-offset, or edit "
              "its docker-compose.yml ports), or stop the other service.", file=sys.stderr)
        return 1
    return 0


def _pack_env(pack: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """The pack `.env` as Compose reads it, with the process environment winning for the keys
    that choose files and the project, as it does for Compose."""
    return {**read_compose_env(pack / ".env", environ), **{
        key: environ[key] for key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME",
                                      "COMPOSE_PATH_SEPARATOR") if environ.get(key)}}


def _cmd_tag(args, environ) -> str:
    pack = Path(args.pack)
    return image_tag(args.release, args.sha, project_name(pack, _pack_env(pack, environ))) + "\n"


def _cmd_project(args, environ) -> str:
    pack = Path(args.pack)
    return project_name(pack, _pack_env(pack, environ)) + "\n"


def _cmd_override(args, environ) -> str:
    pack = Path(args.pack)
    files = compose_files(pack, _pack_env(pack, environ))
    return render_override(engine_services(files), built_services(files))


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="immutable engine image tags (okengine#1033)")
    sub = parser.add_subparsers(dest="command", required=True)
    tag = sub.add_parser("tag", help="print this deployment's immutable engine image tag")
    tag.add_argument("pack")
    tag.add_argument("--release", required=True)
    tag.add_argument("--sha", required=True)
    tag.set_defaults(render=_cmd_tag)
    project = sub.add_parser("project", help="print the compose project this deployment runs as")
    project.add_argument("pack")
    project.set_defaults(render=_cmd_project)
    override = sub.add_parser("override", help="print the engine-image override entries")
    override.add_argument("pack")
    override.set_defaults(render=_cmd_override)
    drift = sub.add_parser("drift", help="running image vs compose reference, from the deployment dir")
    drift.set_defaults(render=None, claim=False)
    claim = sub.add_parser("claim", help="refuse a compose project another directory already runs")
    claim.add_argument("pack")
    claim.set_defaults(render=None, claim=_cmd_claim)
    ports = sub.add_parser("ports", help="refuse host ports another deployment already publishes")
    ports.add_argument("pack")
    ports.set_defaults(render=None, claim=_cmd_ports)
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    if args.render is None and not args.claim:
        return drift_main(environ=environ)
    if args.render is None:
        try:
            # `_run` looked up now, not bound as a default at import: the docker boundary must be
            # replaceable (a CI runner has no docker; a test there exits 2, undetectable).
            return args.claim(args, environ, _run)
        except (ValueError, OSError) as exc:
            print(f"engine_images: {exc}", file=sys.stderr)
            return 1
    try:
        sys.stdout.write(args.render(args, environ))
    except (ValueError, OSError, YAMLError) as exc:
        print(f"engine_images: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
