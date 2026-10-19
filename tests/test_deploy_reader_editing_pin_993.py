"""okengine#993 R2: the reader's read-only indicator follows the same OKENGINE_EDITING value the
gateway and ensure-runtime follow.

The reader gets OKENGINE_EDITING through compose interpolation, where the invoking shell beats
.env; the gateway gets it through env_file, where .env wins. deploy.sh pins the compose
environment to the .env value before `compose up`. These tests run deploy.sh's own pin block.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "scripts" / "deploy.sh"
SKELETON = REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml"


def _pin_block() -> str:
    text = DEPLOY.read_text()
    start = text.index("# --- reader editing pin: begin")
    end = text.index("# --- reader editing pin: end")
    assert start < end < text.index('echo "==> [4/6] docker compose up'), \
        "the pin must run before deploy.sh's first compose up"
    return text[start:end]


def _run_pin(pack: Path, shell_value: str | None, engine_dir: Path = REPO):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"),
           "PACK": str(pack), "ENGINE_DIR": str(engine_dir)}
    if shell_value is not None:
        env["OKENGINE_EDITING"] = shell_value
    script = _pin_block() + '\nprintf "RESULT=%s|%s\\n" "${OKENGINE_EDITING+set}" ' \
                            '"${OKENGINE_EDITING:-}"\n'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    line = next(x for x in result.stdout.splitlines() if x.startswith("RESULT="))
    is_set, value = line[len("RESULT="):].split("|", 1)
    return (value if is_set else None), result


def test_the_env_file_value_beats_a_disagreeing_shell_value_and_says_so(tmp_path):
    (tmp_path / ".env").write_text("OKENGINE_EDITING=0  # read-only\n")
    value, result = _run_pin(tmp_path, "1")
    assert value == "0"
    assert "overridden by .env" in result.stdout


def test_an_agreeing_shell_value_is_kept_silently(tmp_path):
    (tmp_path / ".env").write_text("OKENGINE_EDITING=0\n")
    value, result = _run_pin(tmp_path, "0")
    assert value == "0" and "overridden" not in result.stdout


def test_the_env_file_value_is_exported_when_the_shell_has_none(tmp_path):
    (tmp_path / ".env").write_text("OKENGINE_EDITING=${EDIT}\nEDIT=0\n")
    assert _run_pin(tmp_path, None)[0] == "0"


@pytest.mark.parametrize("dotenv", [None, "OTHER=1\n", "# OKENGINE_EDITING=0\n"])
def test_a_shell_value_stands_when_the_env_file_does_not_set_it(tmp_path, dotenv):
    """ensure-runtime then follows the process value, so the reader should too."""
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    assert _run_pin(tmp_path, "0")[0] == "0"
    assert _run_pin(tmp_path, None)[0] is None


def test_an_unresolvable_env_file_warns_and_leaves_the_shell_value(tmp_path):
    (tmp_path / ".env").write_text("OKENGINE_EDITING=0\n")
    value, result = _run_pin(tmp_path, "1", engine_dir=tmp_path / "no-engine")
    assert value == "1"
    assert "could not resolve OKENGINE_EDITING" in result.stderr


def test_the_pinned_reader_agrees_with_the_gateway_under_compose(tmp_path):
    """Live: run the pin, then ask compose what the skeleton's reader and gateway get."""
    docker = shutil.which("docker")
    if docker is None or subprocess.run([docker, "compose", "version"], capture_output=True,
                                        timeout=60).returncode != 0:
        pytest.skip("docker compose is not available here; reader/gateway agreement unverified")
    (tmp_path / ".env").write_text("OKENGINE_EDITING=0\n")
    reader_line = next(x for x in SKELETON.read_text().splitlines()
                       if x.strip().startswith("- OKENGINE_EDITING="))
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  gateway:\n    image: alpine\n    env_file:\n      - path: .env\n"
        "        required: false\n  reader:\n    image: alpine\n    environment:\n"
        f"      {reader_line.strip()}\n")
    value, _result = _run_pin(tmp_path, "1")
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST",
                                                          "DOCKER_CONFIG", "XDG_RUNTIME_DIR")}

    def services(shell_value):
        out = subprocess.run([docker, "compose", "config", "--format", "json"], cwd=tmp_path,
                             capture_output=True, text=True, timeout=120,
                             env={**base, "OKENGINE_EDITING": shell_value})
        assert out.returncode == 0, out.stderr
        return {name: svc["environment"].get("OKENGINE_EDITING")
                for name, svc in json.loads(out.stdout)["services"].items()}

    assert services("1") == {"gateway": "0", "reader": "1"}       # the split, unpinned
    assert services(value) == {"gateway": "0", "reader": "0"}     # pinned by deploy.sh
