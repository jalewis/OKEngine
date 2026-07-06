"""Regression tests for the extension trust gate (okengine#124).

No OS sandboxing/signing yet, so an OPERATOR-tier (third-party/paid drop-in) extension
that runs in-gateway (full access, no isolation) is refused unless --allow-untrusted.
pack/engine-tier in-gateway (author already trusted) and any-tier sidecar (isolated)
are allowed.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "scripts" / "framework_extensions.py"
DISC = REPO / "scripts" / "extension_discovery.py"

pytestmark = pytest.mark.skipif(not CLI.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _manifest(ext_id, trust):
    if trust == "sidecar":
        ep = {"image": {"registry": f"r/{ext_id}", "digest": "sha256:abc"}}
    else:
        ep = {"script": "run.py"}
    return {"id": ext_id, "kind": "operation", "version": "0.1.0", "trust": trust,
            "requires": {"engine": ">=0.3.0"},
            "capabilities": {"read": ["wiki/**"], "write": [ext_id.split(".")[-1] + "/**"]},
            "operation": {"schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": ep}}


def _operator_ext(pack, ext_id, trust="in-gateway"):
    d = pack / ".okengine" / "extensions" / ext_id          # tier-3: operator drop-in
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_manifest(ext_id, trust)), encoding="utf-8")
    if trust != "sidecar":
        (d / "run.py").write_text("print('{}')\n", encoding="utf-8")
    return d


def _pack_ext(pack, ext_id, trust="in-gateway"):
    d = pack / "extensions" / ext_id                        # tier-2: pack-bundled
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_manifest(ext_id, trust)), encoding="utf-8")
    if trust != "sidecar":
        (d / "run.py").write_text("print('{}')\n", encoding="utf-8")
    return d


def test_operator_in_gateway_refused_by_default(tmp_path):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    assert cli.main(["enable", str(pack), "third.party"]) == 1     # refused
    enabled, _ = disc.load_enabled_state(pack)
    assert "third.party" not in enabled                            # no state change


def test_operator_in_gateway_allowed_with_override(tmp_path):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    assert cli.main(["enable", str(pack), "third.party", "--allow-untrusted"]) == 0
    enabled, _ = disc.load_enabled_state(pack)
    assert "third.party" in enabled


def test_operator_sidecar_allowed(tmp_path):
    """A sidecar is isolated (own container + scoped MCP), so an operator-tier sidecar
    needs no override."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.side", trust="sidecar")
    assert cli.main(["enable", str(pack), "third.side"]) == 0
    enabled, _ = disc.load_enabled_state(pack)
    assert "third.side" in enabled


def test_pack_tier_in_gateway_allowed(tmp_path):
    """A pack-bundled in-gateway extension is as trusted as the pack's own crons — allowed."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _pack_ext(pack, "demo.bundled")
    assert cli.main(["enable", str(pack), "demo.bundled"]) == 0
    enabled, _ = disc.load_enabled_state(pack)
    assert "demo.bundled" in enabled
