import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_review_write_image_copies_required_write_server_modules():
    dockerfile = (REPO / "okengine-mcp" / "Dockerfile.review").read_text()
    assert "COPY okengine-mcp/write_server.py /engine/okengine-mcp/write_server.py" in dockerfile
    assert "pip install --no-cache-dir -r /engine/requirements.txt /wheel/*.whl" in dockerfile
    package = (REPO / "pyproject.toml").read_text()
    assert '"okengine-mcp/output_contract_enforce.py" = "output_contract_enforce.py"' in package
    assert (REPO / "src/okengine/mcp/scope.py").is_file()


def test_runtime_images_copy_extracted_service_modules():
    cockpit = (REPO / "okengine-cockpit" / "Dockerfile").read_text()
    assert "pip install --no-cache-dir -r requirements.txt /wheel/*.whl" in cockpit


def _partial_gateway_overlays(root: Path) -> list[str]:
    """Dockerfiles that start FROM an existing gateway image and COPY engine source onto it."""
    found = []
    for path in sorted(root.rglob("*Dockerfile*")):
        if ".git" in path.parts or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        base = re.search(r"^ARG BASE=(\S+)", text, re.M)
        froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
        gateway = any("hermes-agent" in ref for ref in froms) or (
            base and "hermes-agent" in base.group(1) and any("${BASE}" in r for r in froms))
        if gateway and re.search(r"^COPY\s+(okengine-mcp|src/okengine|scripts/cron)/", text, re.M):
            found.append(path.relative_to(root).as_posix())
    return found


def test_no_partial_gateway_overlay_rebakes_a_subset_of_the_write_path():
    """v0.15.11 audit [18]. The write-server and id-index overlays copied a hand-picked subset of
    the write path onto an existing gateway image. The gateway installs okengine, id_index and
    output_contract_enforce from the wheel in site-packages, so the copies landed on paths nothing
    imports; the copy set also lacked every module write_server gained since (pack_principal,
    identity_guard, actor_types, converge); and the default BASE was hermes-agent:latest, which no
    deployment runs. A write-path change ships as a patch release through build-engine-image.sh."""
    assert _partial_gateway_overlays(REPO) == []


def test_partial_overlay_detector_flags_the_removed_overlay_shape(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "gateway-write-server-overlay.Dockerfile").write_text(
        "ARG BASE=hermes-agent:latest\nFROM ${BASE}\n"
        "COPY okengine-mcp/write_server.py /opt/hermes/okengine-mcp/write_server.py\n")
    (tmp_path / "scripts" / "gateway-idindex-overlay.Dockerfile").write_text(
        "FROM hermes-agent:okengine-v1-abc\nCOPY scripts/cron/id_index.py /opt/hermes/x.py\n")
    (tmp_path / "okengine-reader").mkdir()
    (tmp_path / "okengine-reader" / "Dockerfile").write_text(
        "FROM python:3.13-slim\nCOPY src/ ./src/\n")
    assert _partial_gateway_overlays(tmp_path) == [
        "scripts/gateway-idindex-overlay.Dockerfile", "scripts/gateway-write-server-overlay.Dockerfile"]
