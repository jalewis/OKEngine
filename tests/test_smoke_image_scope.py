"""okengine#1033: the smoke/e2e stack must never build under a name a deployment runs.

The smoke compose used to build `okengine-reader`, `okengine-mcp`, `okengine-cockpit` and
`okengine-review-write`, all implicitly `:latest`, which are exactly the names the pack skeleton's
services reference. On 2026-10-10 a local `smoke-e2e.sh --e2e` build on a deploy host silently
moved those live `:latest` tags to unreleased branch code: any `docker compose up -d` without
`--build` on a co-installed pack would have recreated its containers onto it.

The rule is checked by REPOSITORY, not by full reference: a smoke image that shares a deployment's
repository with a different tag is one edit away from the same accident, and `docker compose
build` writes whatever the name resolves to.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

#: Compose files that stand up throwaway test stacks.
TEST_COMPOSE_GLOBS = ("tests/e2e/**/docker-compose*.yml", "tests/e2e/**/docker-compose*.yaml")
#: Compose files a deployment is built from (the pack skeleton and its overlays, engine configs).
DEPLOY_COMPOSE_GLOBS = ("templates/**/*.yml", "templates/**/*.yaml", "config/**/docker-compose*.yml")

class _ComposeLoader(yaml.SafeLoader):
    """Compose's merge tags (`!override`, `!reset`) carry no image data; load them as plain nodes."""


def _plain(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _plain)

_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^}]*)\}")


def _resolve(reference: str) -> str:
    """Substitute `${VAR:-default}` with its default, as an unset environment would."""
    return _DEFAULT.sub(lambda m: m.group(1), reference)


def _repository(reference: str) -> str:
    """`name[:tag][@digest]` -> `name`, keeping a registry host's port (`host:5000/name`)."""
    name = _resolve(reference).split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    return name


def _compose_images(globs: tuple[str, ...]) -> dict[str, list[str]]:
    """repository -> ['<file>:<service>', ...] for every service `image:` in matching composes."""
    found: dict[str, list[str]] = {}
    for pattern in globs:
        for path in sorted(REPO.glob(pattern)):
            # Templates carry `{{PACK}}`-style placeholders that are not YAML until rendered.
            text = re.sub(r"\{\{[A-Z_]+\}\}", "placeholder", path.read_text(encoding="utf-8"))
            doc = yaml.load(text, Loader=_ComposeLoader) or {}
            services = doc.get("services") if isinstance(doc, dict) else None
            for name, service in (services or {}).items():
                image = (service or {}).get("image")
                if image:
                    where = f"{path.relative_to(REPO).as_posix()}:{name}"
                    found.setdefault(_repository(str(image)), []).append(where)
    return found


def _collisions(test_images: dict[str, list[str]], deploy_images: dict[str, list[str]]):
    return {repo: (where, deploy_images[repo]) for repo, where in test_images.items()
            if repo in deploy_images}


def test_no_smoke_or_e2e_image_shares_a_deployment_image_name():
    test_images = _compose_images(TEST_COMPOSE_GLOBS)
    deploy_images = _compose_images(DEPLOY_COMPOSE_GLOBS)
    # Premises: both sides were actually found, or the comparison is vacuous.
    assert any(w.startswith("tests/e2e/smoke/docker-compose.smoke.yml:")
               for where in test_images.values() for w in where), "smoke compose images not found"
    assert "okengine-reader" in deploy_images, "pack skeleton images not found; globs are stale"
    clash = _collisions(test_images, deploy_images)
    assert not clash, (
        "a smoke/e2e image uses a deployment's image name, so a local smoke build retags the live "
        f"deployment's image (okengine#1033); scope it as okengine-smoke-<svc>: {clash}")


def test_smoke_images_are_scoped_to_the_smoke_stack_and_a_run_id():
    doc = yaml.safe_load((REPO / "tests/e2e/smoke/docker-compose.smoke.yml").read_text())
    for name, service in doc["services"].items():
        image = service["image"]
        assert image.startswith("okengine-smoke-"), f"{name}: unscoped smoke image {image!r}"
        assert image.endswith(":${SMOKE_RUN_ID:-local}"), (
            f"{name}: smoke image must carry the run-id tag, not an implicit :latest: {image!r}")


def test_detector_flags_a_smoke_image_named_like_a_deployment():
    """Negative fixture: the pre-#1033 smoke name must be caught, whatever its tag."""
    deploy = {"okengine-reader": ["templates/pack/skeleton/docker-compose.yml:okengine-reader"]}
    for legacy in ("okengine-reader", "okengine-reader:latest", "okengine-reader:${SMOKE_RUN_ID:-local}"):
        smoke = {_repository(legacy): ["tests/e2e/smoke/docker-compose.smoke.yml:reader"]}
        assert _collisions(smoke, deploy), legacy
    scoped = {_repository("okengine-smoke-reader:${SMOKE_RUN_ID:-local}"): ["smoke:reader"]}
    assert not _collisions(scoped, deploy)


def test_repository_parsing():
    assert _repository("okengine-mcp") == "okengine-mcp"
    assert _repository("okengine-mcp:latest") == "okengine-mcp"
    assert _repository("reg.example:5000/ns/okengine-mcp:1@sha256:" + "0" * 64) == \
        "reg.example:5000/ns/okengine-mcp"
    assert _repository("${OKENGINE_GATEWAY_IMAGE:-hermes-agent:okengine-unpinned}") == "hermes-agent"
    assert _repository("okengine-smoke-reader:${SMOKE_RUN_ID:-local}") == "okengine-smoke-reader"
