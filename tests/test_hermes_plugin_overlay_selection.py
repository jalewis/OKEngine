"""Hermes image assembly must use a complete plugin generation for its exact pin."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "scripts/select_hermes_plugin_overlays.sh"
BUILD = ROOT / "scripts/build-engine-image.sh"
NATIVE_VERIFY = ROOT / "scripts/verify_native_openrouter.sh"
REQUIRED = ("model-providers/custom", "model-providers/openrouter", "web/serper")
TARGET_REQUIRED = ("model-providers/custom", "web/serper")


def _select(engine: Path, tag: str):
    return subprocess.run(["bash", str(SELECTOR), str(engine), tag],
                          text=True, capture_output=True, check=False)


def _fixture_set(engine: Path, rel_root: str, required=REQUIRED):
    for rel in required:
        plugin = engine / rel_root / rel
        plugin.mkdir(parents=True)
        (plugin / "__init__.py").write_text("", encoding="utf-8")
        (plugin / "plugin.yaml").write_text("name: fixture\n", encoding="utf-8")


def test_production_pin_uses_only_current_plugin_set():
    result = _select(ROOT, "v2026.7.7.2")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(ROOT / "plugins")


def test_target_pin_refuses_partial_target_set_until_all_ports_exist(tmp_path):
    rel_root = "overlays/hermes-v2026.9.14/plugins"
    _fixture_set(tmp_path, rel_root, TARGET_REQUIRED)
    (tmp_path / rel_root / "model-providers/custom/plugin.yaml").unlink()
    result = _select(tmp_path, "v2026.9.14")
    assert result.returncode != 0
    assert "incomplete v2026.9.14 plugin overlay set" in result.stderr


def test_target_pin_uses_target_set_without_replacing_native_openrouter(tmp_path):
    rel_root = "overlays/hermes-v2026.9.14/plugins"
    _fixture_set(tmp_path, rel_root, TARGET_REQUIRED)
    _fixture_set(tmp_path, "plugins")
    result = _select(tmp_path, "v2026.9.14")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / rel_root)


def test_selector_rejects_missing_manifest_file_and_unknown_pin(tmp_path):
    _fixture_set(tmp_path, "plugins")
    (tmp_path / "plugins/web/serper/plugin.yaml").unlink()
    missing = _select(tmp_path, "v2026.7.7.2")
    assert missing.returncode != 0
    assert "web/serper" in missing.stderr
    unknown = _select(tmp_path, "v2026.9.15")
    assert unknown.returncode != 0
    assert "no verified plugin overlay set" in unknown.stderr


def test_image_builder_calls_selector_before_copying_profiles():
    source = BUILD.read_text(encoding="utf-8")
    selector = source.index("select_hermes_plugin_overlays.sh")
    custom_copy = source.index('cp -r "$PLUGIN_OVERLAY_ROOT/model-providers/custom"')
    assert selector < custom_copy
    assert 'cp -r "$PLUGIN_OVERLAY_ROOT/model-providers/openrouter"' in source
    assert 'verify_native_openrouter.sh" "$WORK"' in source
    assert 'cp -r "$PLUGIN_OVERLAY_ROOT/web/serper"' in source


def test_native_openrouter_verifier_rejects_missing_and_tampered_source(tmp_path):
    missing = subprocess.run(["bash", str(NATIVE_VERIFY), str(tmp_path)],
                             text=True, capture_output=True, check=False)
    assert missing.returncode != 0
    assert "provider is missing" in missing.stderr
    provider = tmp_path / "plugins/model-providers/openrouter"
    provider.mkdir(parents=True)
    (provider / "__init__.py").write_text("# replaced upstream profile\n", encoding="utf-8")
    (provider / "plugin.yaml").write_text("name: openrouter\n", encoding="utf-8")
    tampered = subprocess.run(["bash", str(NATIVE_VERIFY), str(tmp_path)],
                              text=True, capture_output=True, check=False)
    assert tampered.returncode != 0
    assert "provider drifted or was replaced" in tampered.stderr
