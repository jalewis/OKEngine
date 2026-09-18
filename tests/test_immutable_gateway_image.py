"""Immutable gateway image contract (#627)."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_skeleton_uses_nonmoving_gateway_image_variable():
    text = (ROOT / "templates/pack/skeleton/docker-compose.yml").read_text(encoding="utf-8")
    image = next(line.split("image:", 1)[1].strip() for line in text.splitlines()
                 if line.strip().startswith("image: ${OKENGINE_GATEWAY_IMAGE"))
    assert "OKENGINE_GATEWAY_IMAGE" in image
    assert "latest" not in image
    assert image.endswith("okengine-unpinned}")


def test_generated_image_override_is_ignored_by_pack_source_control():
    ignored = (ROOT / "templates/pack/skeleton/.gitignore").read_text(encoding="utf-8")
    assert "docker-compose.okengine-image.yml" in ignored.splitlines()


def test_deploy_never_probes_or_builds_latest():
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    executable = "\n".join(line for line in deploy.splitlines()
                           if not line.lstrip().startswith("#"))
    assert "hermes-agent:latest" not in executable
    assert 'docker image inspect "$gateway_image"' in executable
    assert 'OKENGINE_TAG="$gateway_tag" TAG_LATEST=0' in executable
