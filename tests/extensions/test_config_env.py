"""Fleet-wide contract for declarative extension configuration (#210)."""
import importlib.util
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
EXTENSIONS = REPO / "extensions"
COMPOSE = REPO / "scripts" / "extension_compose.py"


def _compose():
    spec = importlib.util.spec_from_file_location("extension_compose_config_test", COMPOSE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_in_gateway_config_is_synthesized_as_namespaced_job_env(tmp_path):
    c = _compose()
    manifest = {
        "id": "okengine.demo-alpha",
        "kind": "operation",
        "trust": "in-gateway",
        "config": {
            "batch_size": {"type": "integer", "default": 7},
            "dry-run": {"type": "boolean", "default": False},
            "focus": {"type": "string", "default": ""},
        },
        "operation": {
            "schedule": {"kind": "cron", "expr": "0 * * * *"},
            "entrypoint": "run.py",
        },
    }
    jobs, errors, _ = c.synthesize_ops({
        "id": manifest["id"], "manifest": manifest, "dir": str(tmp_path)
    })
    assert not errors
    assert jobs[0]["env"] == {
        "OKENGINE_DEMO_ALPHA_BATCH_SIZE": "7",
        "OKENGINE_DEMO_ALPHA_DRY_RUN": "false",
        "OKENGINE_DEMO_ALPHA_FOCUS": "",
    }


def test_config_env_names_retain_third_party_namespace():
    c = _compose()
    assert c.extension_config_env_name("acme.analytics", "batch-size") == \
        "OKENGINE_ACME_ANALYTICS_BATCH_SIZE"


def test_normalized_config_key_collision_fails_loudly(tmp_path):
    c = _compose()
    manifest = {
        "kind": "operation",
        "trust": "in-gateway",
        "config": {"batch-size": 1, "batch_size": 2},
        "operation": {
            "schedule": {"kind": "cron", "expr": "0 * * * *"},
            "entrypoint": "run.py",
        },
    }
    jobs, errors, _ = c.synthesize_ops({
        "id": "okengine.demo", "manifest": manifest, "dir": str(tmp_path)
    })
    assert jobs == []
    assert any("both map to OKENGINE_DEMO_BATCH_SIZE" in e for e in errors)


def test_every_in_gateway_config_key_has_a_canonical_runtime_read():
    """A declared knob must be consumed, not merely injected and silently ignored."""
    missing = []
    for manifest_path in sorted(EXTENSIONS.glob("*/extension.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
        config = manifest.get("config") or {}
        if not config or manifest.get("kind") != "operation" or manifest.get("trust") == "sidecar":
            continue
        ext_id = manifest.get("id") or manifest_path.parent.name
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in manifest_path.parent.rglob("*.py")
        )
        for key in config:
            env_name = _compose().extension_config_env_name(ext_id, key)
            if env_name not in source:
                missing.append(f"{ext_id}.config.{key} -> {env_name}")
    assert not missing, "config values injected but never read:\n" + "\n".join(missing)
