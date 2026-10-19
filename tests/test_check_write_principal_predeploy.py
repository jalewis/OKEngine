"""okengine#1027 (v0.15.11 invariant audit #2/#8): the write-principal check must stop a deploy
BEFORE the gateway is recreated. It used to run only in post-deploy verify and the daily lane, after
the write servers had already refused to start."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_write_principal.py"
DEPLOY = REPO / "scripts" / "deploy.sh"

spec = importlib.util.spec_from_file_location("check_write_principal", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def _pack(tmp_path: Path, pack_yaml: str | None, writer_env: dict | None = None) -> Path:
    pack = tmp_path / "pack"
    (pack / ".hermes-data").mkdir(parents=True)
    if pack_yaml is not None:
        (pack / "pack.yaml").write_text(pack_yaml)
    servers = {"okengine": {"url": "http://okengine-mcp:8730/mcp"},
               "okengine-write": {"command": "python",
                                  "args": ["/opt/hermes/okengine-mcp/write_server.py"],
                                  "env": writer_env or {}}}
    (pack / ".hermes-data" / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": servers}))
    return pack


def test_a_nameless_pack_stops_the_deploy(tmp_path, capsys):
    assert gate.main([str(_pack(tmp_path, "version: 1.0.0\n"))]) == 1
    err = capsys.readouterr().err
    assert "okengine-write would refuse to start" in err and "Nothing was recreated" in err


def test_a_named_pack_passes(tmp_path, capsys):
    assert gate.main([str(_pack(tmp_path, "name: okpack-x\n"))]) == 0
    assert "start with a pack principal" in capsys.readouterr().out


def test_a_stale_env_pin_that_disagrees_stops_the_deploy(tmp_path):
    pack = _pack(tmp_path, "name: okpack-x\n", {"OKENGINE_PACK": "okpack-old"})
    assert gate.main([str(pack)]) == 1


def test_no_rendered_config_has_nothing_to_judge(tmp_path):
    pack = tmp_path / "bare"
    pack.mkdir()
    assert gate.main([str(pack)]) == 0


def test_usage_is_refused():
    assert gate.main([]) == 2


def test_the_cli_exit_code_carries_the_verdict(tmp_path):
    out = subprocess.run([sys.executable, str(SCRIPT), str(_pack(tmp_path, "version: 1\n"))],
                         capture_output=True, text=True, check=False)
    assert out.returncode == 1, out.stderr


def test_deploy_runs_the_gate_after_rendering_and_before_any_recreate():
    text = DEPLOY.read_text(encoding="utf-8")
    gate_call = text.index('"$PYTHON" "$ENGINE_DIR/scripts/check_write_principal.py" "$PACK" || exit 1')
    assert text.index('bash "$ENGINE_DIR/scripts/ensure-runtime.sh" "$PACK"') < gate_call
    assert gate_call < text.index("docker compose up -d --build )")
    assert gate_call < text.index("--force-recreate --no-deps gateway")
    assert "SKIP_VALIDATE" not in text[text.rindex("\n", 0, gate_call - 300):gate_call]


def test_more_than_one_argument_is_refused():
    assert gate.main(["a", "b"]) == 2


def test_an_undetectable_principal_is_not_a_pass(tmp_path, monkeypatch):
    """A WARN (the engine cannot judge) stops the deploy as surely as a FAIL."""
    def missing():
        raise ImportError("no okengine.pack_principal")

    monkeypatch.setattr(gate.checks, "_pack_principal", missing)
    assert gate.main([str(_pack(tmp_path, "name: okpack-x\n"))]) == 1


def test_deploy_sh_invocation_passes_on_a_named_pack_despite_a_stale_okengine(tmp_path):
    """!1305 QA B1, exactly as deploy.sh runs step 2b: the host python, no PYTHONPATH from the
    engine, cwd elsewhere, and a stale `okengine` WITHOUT pack_principal ahead on the path (the
    fleet-roll venv's editable install of another worktree). It refused every healthy host."""
    stale = tmp_path / "stale" / "okengine"
    stale.mkdir(parents=True)
    (stale / "__init__.py").write_text("STALE = True\n")
    pack = _pack(tmp_path, "name: okpack-x\n")
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path / "stale"), "HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, str(SCRIPT), str(pack)], capture_output=True, text=True,
                         check=False, cwd=str(tmp_path), env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "start with a pack principal" in out.stdout
