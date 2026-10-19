"""okengine#1042 [32]: a `disabled:` id no tier discovers any more.

An operator opts out of a core extension; a later release renames or removes it. `framework
validate` (deploy step 1) FAILed on every run with no remedy, `extensions validate` called the same
state clean, and no CLI path could clear the entry from a file marked CLI-maintained. The FAIL is
kept (the opt-out silently stopped applying, so a renamed core extension is back ON), but both
validators now agree, name the remedy, and `extensions forget` performs it.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
STALE = "okengine.renamed"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(ext_id: str, **extra) -> dict:
    return {"id": ext_id, "kind": "operation", "version": "0.1.0", "name": ext_id,
            "trust": "sidecar", "requires": {"engine": ">=0.3.0"},
            "capabilities": {"read": ["wiki/**"], "write": ["dashboards/**"]},
            "operation": {"schedule": {"kind": "cron", "expr": "17 5 * * *"},
                          "entrypoint": {"image": {"registry": "reg.example.com/x",
                                                   "digest": "sha256:deadbeef"}},
                          "timeout": 1800}, **extra}


@pytest.fixture
def engine_root(monkeypatch, tmp_path):
    """A tier-1 root with one core extension, so `okengine.current` is discovered and default-on."""
    root = tmp_path / "engine"
    d = root / "extensions" / "okengine.current"
    d.mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_manifest("okengine.current", core=True)),
                                      encoding="utf-8")
    monkeypatch.setenv("OKENGINE_ENGINE_ROOT", str(root))
    return root


@pytest.fixture
def cli(engine_root):
    _load("extension_discovery", "scripts/extension_discovery.py")
    module = _load("framework_extensions", "scripts/framework_extensions.py")
    module._materialize_policy = lambda pack: []
    return module


def _pack(tmp_path, disabled=(STALE,)) -> Path:
    pack = tmp_path / "pack"
    (pack / ".okengine").mkdir(parents=True)
    (pack / ".okengine" / "extensions.yaml").write_text(
        yaml.safe_dump({"enabled": {}, "disabled": list(disabled)}), encoding="utf-8")
    return pack


def _state(pack: Path) -> dict:
    return yaml.safe_load((pack / ".okengine" / "extensions.yaml").read_text())


def _framework_validate(pack: Path) -> list[str]:
    v = _load("framework_validate_under_test_1042", "scripts/framework_validate.py")
    r = v.Report()
    v.check_enabled_extensions_resolve(pack, r)
    return [f"{status} {check} {detail}" for status, check, detail in r.rows]


def test_extensions_validate_fails_a_stale_disabled_id_as_framework_validate_does(
        tmp_path, cli, capsys):
    pack = _pack(tmp_path)
    assert cli.main(["validate", str(pack)]) == 1, "the two validators disagreed about this state"
    out = capsys.readouterr().out
    assert f"FAIL: disabled extension '{STALE}' is not discovered in any tier" in out, out
    assert f"framework extensions forget {pack} {STALE}" in out, "the FAIL must name its remedy"


def test_framework_validate_names_the_remedy(tmp_path, cli):
    pack = _pack(tmp_path)
    text = " ".join(_framework_validate(pack))
    assert f"disabled extension '{STALE}' is not discovered in any tier" in text, text
    assert f"framework extensions forget {pack} {STALE}" in text, text


@pytest.mark.parametrize("stale", [STALE, "okengine.aardvark"])   # sorts above and below
def test_forget_clears_the_stale_id_and_both_validators_pass(tmp_path, cli, capsys, stale):
    pack = _pack(tmp_path, disabled=(stale, "okengine.current"))
    assert cli.main(["forget", str(pack), stale]) == 0
    assert f"forgot the stale opt-out: {stale}" in capsys.readouterr().out
    assert _state(pack) == {"enabled": {}, "disabled": ["okengine.current"]}, \
        "every other entry (a live core opt-out) must survive"
    assert cli.main(["validate", str(pack)]) == 0, capsys.readouterr()
    assert "not discovered" not in " ".join(_framework_validate(pack))


def test_forget_drops_the_key_when_the_last_id_goes(tmp_path, cli):
    pack = _pack(tmp_path)
    assert cli.main(["forget", str(pack), STALE]) == 0
    assert _state(pack) == {"enabled": {}}


def test_forget_refuses_a_discovered_id(tmp_path, cli, capsys):
    """Removing a discovered core id from `disabled:` would silently turn it back ON."""
    pack = _pack(tmp_path, disabled=("okengine.current",))
    before = (pack / ".okengine" / "extensions.yaml").read_text()
    assert cli.main(["forget", str(pack), "okengine.current"]) == 1
    err = capsys.readouterr().err
    assert "okengine.current is discovered" in err and "extensions enable" in err, err
    assert (pack / ".okengine" / "extensions.yaml").read_text() == before


def test_forget_of_an_id_that_is_not_disabled_has_nothing_to_do(tmp_path, cli, capsys):
    pack = _pack(tmp_path)
    before = (pack / ".okengine" / "extensions.yaml").read_text()
    assert cli.main(["forget", str(pack), "okengine.never"]) == 0
    assert "not in disabled: okengine.never (nothing to do)" in capsys.readouterr().out
    assert (pack / ".okengine" / "extensions.yaml").read_text() == before


def test_forget_refuses_while_discovery_or_the_state_fails(tmp_path, cli, capsys):
    pack = _pack(tmp_path)
    squat = pack / "extensions" / "okengine.current"     # reserved id squatted by the pack tier
    squat.mkdir(parents=True)
    (squat / "extension.yaml").write_text(yaml.safe_dump(_manifest("okengine.current")),
                                          encoding="utf-8")
    before = (pack / ".okengine" / "extensions.yaml").read_text()
    assert cli.main(["forget", str(pack), STALE]) == 1
    err = capsys.readouterr().err
    assert "nothing was changed" in err and "okengine.current" in err.split("cannot tell")[0], \
        "the discovery fault itself must be reported"
    assert (pack / ".okengine" / "extensions.yaml").read_text() == before

    pack2 = tmp_path / "pack2"
    (pack2 / ".okengine").mkdir(parents=True)
    (pack2 / ".okengine" / "extensions.yaml").write_text("disabled: not-a-list\n")
    assert cli.main(["forget", str(pack2), STALE]) == 1
    assert "'disabled' must be a list" in capsys.readouterr().err


def test_forget_runs_through_the_framework_cli(tmp_path, engine_root):
    """Real invocation, as an operator types it: framework.py, no test PYTHONPATH."""
    pack = _pack(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["OKENGINE_ENGINE_ROOT"] = str(engine_root)
    run = [sys.executable, str(REPO / "scripts" / "framework.py"), "extensions"]
    r = subprocess.run(run + ["validate", str(pack)], capture_output=True, text=True,
                       timeout=120, env=env, cwd=tmp_path)
    assert r.returncode == 1 and f"forget {pack} {STALE}" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(run + ["forget", str(pack), STALE], capture_output=True, text=True,
                       timeout=120, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run(run + ["validate", str(pack)], capture_output=True, text=True,
                       timeout=120, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_forget_disabled_reports_an_unreadable_state_as_unchanged(tmp_path, engine_root):
    disc = _load("extension_discovery", "scripts/extension_discovery.py")
    pack = tmp_path / "pack"
    (pack / ".okengine").mkdir(parents=True)
    (pack / ".okengine" / "extensions.yaml").write_text("enabled: [not, a, mapping]\n")
    changed, errors = disc.forget_disabled(pack, STALE)
    assert changed is False and errors and "'enabled' must be a mapping" in errors[0]
