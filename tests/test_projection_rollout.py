from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ensure_projection_config", ROOT / "scripts/ensure_projection_config.py")
CFG = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(CFG)


def test_existing_deployment_gets_secure_idempotent_projection_overlay(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services:\n  okengine-mcp:\n    image: okengine-mcp\n")
    (pack / ".env").write_text(
        "\n# COMMENTED=value\nKEEP=unchanged=with-equals\n"
        "OKENGINE_PROJECTION_WRITER_PASSWORD=REPLACE_BEFORE_ENABLING\n"
        "OKENGINE_PROJECTION_READER_PASSWORD=REPLACE_WITH_A_DIFFERENT_VALUE\n")

    changes = CFG.ensure(pack, ROOT)
    assert "materialized projection compose overlay" in changes
    env = dict(line.split("=", 1) for line in (pack / ".env").read_text().splitlines()
               if line and not line.startswith("#"))
    assert env["KEEP"] == "unchanged=with-equals"
    assert "COMMENTED" not in env
    assert env["OKENGINE_PROJECTION_WRITER_PASSWORD"] not in CFG.PLACEHOLDERS
    assert env["OKENGINE_PROJECTION_READER_PASSWORD"] not in CFG.PLACEHOLDERS
    assert env["OKENGINE_PROJECTION_WRITER_PASSWORD"] != env["OKENGINE_PROJECTION_READER_PASSWORD"]
    assert env["OKENGINE_PROJECTION_READER_PASSWORD"] in env["OKENGINE_PROJECTION_READER_DSN"]
    assert env["COMPOSE_FILE"] == "docker-compose.yml:.hermes-data/projection-compose.yml"
    assert (pack / ".hermes-data/projection-compose.yml").read_text() == (
        ROOT / "templates/pack/projection-compose-overlay.yml").read_text()
    assert os.stat(pack / ".env").st_mode & 0o777 == 0o600
    before = (pack / ".env").read_text()
    assert CFG.ensure(pack, ROOT) == []
    assert (pack / ".env").read_text() == before


def test_credentials_are_exactly_36_bytes_and_equal_values_rotate(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services:\n  okengine-projection: {}\n")
    (pack / ".env").write_text(
        "OKENGINE_PROJECTION_WRITER_PASSWORD=same\n"
        "OKENGINE_PROJECTION_READER_PASSWORD=same\n")
    calls = []

    def token(size):
        calls.append(size)
        return f"generated-{size}"

    monkeypatch.setattr(CFG.secrets, "token_urlsafe", token)
    CFG.ensure(pack, ROOT)
    assert calls == [36]
    text = (pack / ".env").read_text()
    assert text.count("OKENGINE_PROJECTION_READER_PASSWORD=") == 1
    assert "OKENGINE_PROJECTION_READER_PASSWORD=generated-36" in text


def test_distinct_credentials_are_preserved_regardless_of_sort_order(tmp_path, monkeypatch):
    for writer, reader in (("aaa", "zzz"), ("zzz", "aaa")):
        pack = tmp_path / writer
        pack.mkdir()
        (pack / "docker-compose.yml").write_text("services:\n  okengine-projection: {}\n")
        (pack / ".env").write_text(
            f"OKENGINE_PROJECTION_WRITER_PASSWORD={writer}\n"
            f"OKENGINE_PROJECTION_READER_PASSWORD={reader}\n")
        monkeypatch.setattr(
            CFG.secrets, "token_urlsafe",
            lambda *_: (_ for _ in ()).throw(AssertionError("must preserve credentials")))
        CFG.ensure(pack, ROOT)
        text = (pack / ".env").read_text()
        assert f"OKENGINE_PROJECTION_WRITER_PASSWORD={writer}" in text
        assert f"OKENGINE_PROJECTION_READER_PASSWORD={reader}" in text


def test_stale_overlay_is_replaced_for_any_lexicographic_content(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services:\n  okengine-mcp: {}\n")
    CFG.ensure(pack, ROOT)
    generated = pack / ".hermes-data/projection-compose.yml"
    expected = (ROOT / "templates/pack/projection-compose-overlay.yml").read_text()
    for stale in ("aaa\n", "zzz\n"):
        generated.write_text(stale)
        assert "materialized projection compose overlay" in CFG.ensure(pack, ROOT)
        assert generated.read_text() == expected


def test_native_projection_needs_no_overlay(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text(
        "services:\n  okengine-projection:\n    image: okengine-projection\n")
    changes = CFG.ensure(pack, ROOT)
    assert "projection compose overlay" not in " ".join(changes)
    assert not (pack / ".hermes-data/projection-compose.yml").exists()
    assert "COMPOSE_FILE=" not in (pack / ".env").read_text()


def test_standard_deploy_and_verifier_require_projection():
    deploy = (ROOT / "scripts/deploy.sh").read_text()
    verify = (ROOT / "scripts/post_deploy_verify.sh").read_text()
    compose = (ROOT / "templates/pack/skeleton/docker-compose.yml").read_text()
    assert "ensure_projection_config.py" in deploy
    assert 'profiles: ["projection"]' not in compose
    assert "python /app/service.py --health" in verify
    assert "from okengine.mcp import projection" in verify
    assert "projection.count_pages()" in verify


def test_bare_compose_refuses_public_projection_placeholders_before_postgres(tmp_path):
    skeleton = (ROOT / "templates/pack/skeleton/docker-compose.yml").read_text()
    compose = yaml.safe_load(re.sub(r"\{\{[^}]*\}\}", "fixture", skeleton))
    postgres = compose["services"]["postgres"]
    projection = compose["services"]["okengine-projection"]
    assert ":?" in postgres["environment"]["POSTGRES_PASSWORD"], (
        "an unset writer password must fail Compose interpolation, not select a public default"
    )
    assert ":?" in projection["environment"]["OKENGINE_PROJECTION_READER_PASSWORD"]
    env_example = (ROOT / "templates/pack/skeleton/.env.example").read_text()
    for key in ("OKENGINE_PROJECTION_WRITER_PASSWORD", "OKENGINE_PROJECTION_READER_PASSWORD",
                "OKENGINE_PROJECTION_READER_DSN"):
        assert f"{key}=\n" in env_example, "public sample credentials must not be non-empty"

    # Compose turns $$ into $ before container start; execute that exact shell
    # contract with a disposable official-entrypoint stand-in in PATH.
    entrypoint = tmp_path / "docker-entrypoint.sh"
    entrypoint.write_text("#!/bin/sh\nexit 0\n")
    entrypoint.chmod(0o755)
    script = postgres["entrypoint"][2].replace("$$", "$")
    for writer in ("", "okengine-projection-local", "REPLACE_BEFORE_ENABLING"):
        env = {**os.environ, "POSTGRES_PASSWORD": writer,
               "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}
        result = subprocess.run(["/bin/sh", "-ec", script], env=env,
                                capture_output=True, text=True, check=False)
        assert result.returncode == 1 and "refuses" in result.stderr, (
            f"copied sample writer value {writer!r} must stop before PostgreSQL starts"
        )
    env = {**os.environ, "POSTGRES_PASSWORD": "distinct-secret",
           "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}
    result = subprocess.run(["/bin/sh", "-ec", script], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_cli_requires_engine_and_reports_changes(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services:\n  okengine-projection: {}\n")
    script = ROOT / "scripts/ensure_projection_config.py"
    missing = subprocess.run(
        [sys.executable, str(script), str(pack)], capture_output=True, text=True)
    assert missing.returncode == 2 and "--engine" in missing.stderr
    result = subprocess.run(
        [sys.executable, str(script), str(pack), "--engine", str(ROOT)],
        capture_output=True, text=True)
    assert result.returncode == 0
    assert "generated projection writer credential" in result.stdout


def test_a_directory_that_is_not_a_deployment_is_refused(tmp_path):
    """`ensure` rewrites a pack's `.env` in place. Run against a path that is not a deployment it
    would create one — a stray `.env` holding generated credentials in whatever directory the
    operator happened to be standing in. The compose file is what makes the path a pack."""
    import pytest
    not_a_pack = tmp_path / "somewhere"
    not_a_pack.mkdir()
    with pytest.raises(ValueError, match="no docker-compose.yml"):
        CFG.ensure(not_a_pack, ROOT)
    assert not (not_a_pack / ".env").exists(), "nothing may be written to a non-deployment"


def test_the_superseded_generated_overlay_is_removed_not_left_to_rot(tmp_path):
    """An earlier engine wrote the overlay under `.okengine/`. Leaving it there leaves a second,
    stale copy of the projection config on disk that COMPOSE_FILE no longer names — the next person
    to read it learns something untrue about the running stack. Only the engine's OWN generated
    file is removed; the marker comment is what proves authorship."""
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services:\n  gateway: {}\n")
    legacy_dir = pack / ".okengine"
    legacy_dir.mkdir()
    (legacy_dir / "projection-compose.yml").write_text(
        "# Engine-managed compatibility overlay\nservices: {}\n")

    changes = CFG.ensure(pack, ROOT)
    assert "removed legacy generated projection overlay" in changes
    assert not (legacy_dir / "projection-compose.yml").exists()


def test_an_operator_authored_overlay_in_the_legacy_path_is_left_alone(tmp_path):
    """Same path, no engine marker: this file is somebody's own work. Deleting it because it sits
    where a generated file used to would destroy hand-written config."""
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services:\n  gateway: {}\n")
    legacy_dir = pack / ".okengine"
    legacy_dir.mkdir()
    mine = legacy_dir / "projection-compose.yml"
    mine.write_text("# hand written by the operator\nservices: {}\n")

    changes = CFG.ensure(pack, ROOT)
    assert "removed legacy generated projection overlay" not in changes
    assert mine.is_file()


def test_the_legacy_directory_survives_if_it_still_holds_anything_else(tmp_path):
    """`.okengine/` is a shared directory — extensions config lives there too. Removing the engine's
    own overlay is a cleanup; removing the directory around somebody else's files would be data
    loss, so a non-empty directory is left exactly where it is."""
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services:\n  gateway: {}\n")
    legacy_dir = pack / ".okengine"
    legacy_dir.mkdir()
    (legacy_dir / "projection-compose.yml").write_text(
        "# Engine-managed compatibility overlay\nservices: {}\n")
    neighbour = legacy_dir / "extensions.yaml"
    neighbour.write_text("enabled: []\n")

    changes = CFG.ensure(pack, ROOT)
    assert "removed legacy generated projection overlay" in changes
    assert not (legacy_dir / "projection-compose.yml").exists()
    assert neighbour.read_text() == "enabled: []\n", "a neighbour's config must survive"
    assert legacy_dir.is_dir()
