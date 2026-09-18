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
    overlay = (REPO / "scripts" / "gateway-write-server-overlay.Dockerfile").read_text()
    assert "pip install --no-cache-dir -r requirements.txt /wheel/*.whl" in cockpit
    assert "COPY src/okengine/write_services/ /opt/hermes/src/okengine/write_services/" in overlay
