"""OKENGINE_HARDENED posture contract (okengine#78).

Pins the fail-closed hardened profile from both sides: the pure helper
(hardening_lib.hardened_posture_violations) case by case, and its wiring into the
runtime gate (deployment_validate.check_auth) so the deployment-validation lane
actually FAILs on an unsafe hardened deployment.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
LIB = REPO / "scripts" / "cron" / "hardening_lib.py"
DV = REPO / "scripts" / "cron" / "deployment_validate.py"


def _lib():
    spec = importlib.util.spec_from_file_location("hardening_lib", LIB)
    m = importlib.util.module_from_spec(spec)
    sys.modules["hardening_lib"] = m
    spec.loader.exec_module(m)
    return m


# A fully-safe hardened env: real token, reader password, rate unset (=default on), private,
# UI editing off (okengine#257 — editing defaults ON, so a hardened env must set it off).
SAFE = {
    "OKENGINE_HARDENED": "1",
    "OKENGINE_MCP_TOKEN": "s3cret-generated-token",
    "OKENGINE_READER_PASSWORD": "hunter2",
    "OKENGINE_TRUST": "private",
    "OKENGINE_EDITING": "0",
}


def test_mode_off_never_flags_even_a_wildly_unsafe_env():
    m = _lib()
    unsafe = {"OKENGINE_MCP_TOKEN": "", "OKENGINE_TRUST": "private",
              "OKENGINE_READER_RATE": "0"}  # no OKENGINE_HARDENED
    assert m.hardened_posture_violations(unsafe) == []


def test_fully_safe_hardened_env_passes():
    m = _lib()
    assert m.hardened_posture_violations(dict(SAFE)) == []


def test_missing_mcp_token_flagged():
    m = _lib()
    env = dict(SAFE); env["OKENGINE_MCP_TOKEN"] = ""
    v = m.hardened_posture_violations(env)
    assert any("OKENGINE_MCP_TOKEN" in x for x in v), v


def test_default_local_token_flagged():
    m = _lib()
    env = dict(SAFE); env["OKENGINE_MCP_TOKEN"] = m.DEFAULT_LOCAL_TOKEN
    v = m.hardened_posture_violations(env)
    assert any(m.DEFAULT_LOCAL_TOKEN in x for x in v), v


def test_private_no_password_flagged():
    m = _lib()
    env = dict(SAFE); env["OKENGINE_READER_PASSWORD"] = ""
    v = m.hardened_posture_violations(env)
    assert any("OKENGINE_READER_PASSWORD" in x for x in v), v


def test_explicit_public_trust_needs_no_reader_password():
    m = _lib()
    env = dict(SAFE); env["OKENGINE_READER_PASSWORD"] = ""; env["OKENGINE_TRUST"] = "public"
    v = m.hardened_posture_violations(env)
    assert not any("OKENGINE_READER_PASSWORD" in x for x in v), v


def test_reader_public_flag_also_satisfies_the_auth_requirement():
    m = _lib()
    env = dict(SAFE); env["OKENGINE_READER_PASSWORD"] = ""; env["OKENGINE_READER_PUBLIC"] = "1"
    v = m.hardened_posture_violations(env)
    assert not any("OKENGINE_READER_PASSWORD" in x for x in v), v


def test_rate_limit_disabled_flagged():
    m = _lib()
    env = dict(SAFE); env["OKENGINE_READER_RATE"] = "0"
    v = m.hardened_posture_violations(env)
    assert any("OKENGINE_READER_RATE" in x for x in v), v


def test_exports_on_public_reader_flagged():
    m = _lib()
    env = dict(SAFE); env["OKENGINE_TRUST"] = "public"; env["OKENGINE_READER_PASSWORD"] = ""
    env["OKENGINE_READER_EXPORTS"] = "1"
    v = m.hardened_posture_violations(env)
    assert any("OKENGINE_READER_EXPORTS" in x for x in v), v


def test_exports_on_PRIVATE_reader_is_fine():
    # exports are only unsafe when the reader is public; a private+authed reader may enable them.
    m = _lib()
    env = dict(SAFE); env["OKENGINE_READER_EXPORTS"] = "1"
    v = m.hardened_posture_violations(env)
    assert not any("OKENGINE_READER_EXPORTS" in x for x in v), v


def test_everything_unsafe_reports_every_violation():
    m = _lib()
    env = {"OKENGINE_HARDENED": "1", "OKENGINE_MCP_TOKEN": "", "OKENGINE_TRUST": "private",
           "OKENGINE_READER_PASSWORD": "", "OKENGINE_READER_RATE": "0",
           "OKENGINE_READER_PUBLIC": "", "OKENGINE_READER_EXPORTS": ""}
    v = m.hardened_posture_violations(env)
    # token + reader-auth + rate = 3 (exports N/A because not public)
    assert len(v) >= 3, v


def test_is_hardened_and_is_public_helpers():
    m = _lib()
    assert m.is_hardened({"OKENGINE_HARDENED": "yes"})
    assert not m.is_hardened({})
    assert m.is_public({"OKENGINE_TRUST": "public"})
    assert m.is_public({"OKENGINE_READER_PUBLIC": "on"})
    assert not m.is_public({"OKENGINE_TRUST": "private"})


# ---- okengine#257: UI-editing switch ----

def test_is_editing_defaults_on_and_honors_falsey():
    m = _lib()
    assert m.is_editing({}) is True                      # unset -> on (back-compat)
    assert m.is_editing({"OKENGINE_EDITING": "1"}) is True
    for off in ("0", "false", "no", "off", "OFF", " 0 "):
        assert m.is_editing({"OKENGINE_EDITING": off}) is False, off


def test_editing_on_is_a_hardened_violation():
    m = _lib()
    env = dict(SAFE); env["OKENGINE_EDITING"] = "1"      # editing ON under hardened
    v = m.hardened_posture_violations(env)
    assert any("OKENGINE_EDITING" in x for x in v), v


def test_editing_default_on_is_a_hardened_violation():
    m = _lib()
    env = dict(SAFE); env.pop("OKENGINE_EDITING")         # unset -> defaults on -> still flagged
    v = m.hardened_posture_violations(env)
    assert any("OKENGINE_EDITING" in x for x in v), v


def test_editing_off_clears_the_violation():
    m = _lib()
    env = dict(SAFE)                                      # SAFE has OKENGINE_EDITING=0
    assert not any("OKENGINE_EDITING" in x for x in m.hardened_posture_violations(env))


# ---- wiring: deployment_validate.check_auth must honor the flag ----

def _dv():
    spec = importlib.util.spec_from_file_location("deployment_validate", DV)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    return m


def _hardening_rows(m):
    return [(lvl, msg) for lvl, area, msg in m.F if area == "hardening"]


def test_gate_fails_on_unsafe_hardened_deployment(tmp_path, monkeypatch):
    m = _dv()
    m.F.clear()
    m.DATA = tmp_path  # no config.yaml -> api_server block skipped
    for k in ("OKENGINE_HARDENED", "OKENGINE_MCP_TOKEN", "OKENGINE_READER_PASSWORD",
              "OKENGINE_TRUST", "OKENGINE_BIND", "OKENGINE_READER_RATE",
              "API_SERVER_ENABLED", "API_SERVER_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OKENGINE_HARDENED", "1")
    monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")  # loopback: isolate the hardening FAILs
    m.check_auth()
    rows = _hardening_rows(m)
    assert any(lvl == "FAIL" for lvl, _ in rows), rows


def test_gate_passes_and_confirms_on_safe_hardened_deployment(tmp_path, monkeypatch):
    m = _dv()
    m.F.clear()
    m.DATA = tmp_path
    for k in ("API_SERVER_ENABLED", "API_SERVER_KEY", "OKENGINE_READER_RATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OKENGINE_HARDENED", "1")
    monkeypatch.setenv("OKENGINE_MCP_TOKEN", "real-token")
    monkeypatch.setenv("OKENGINE_READER_PASSWORD", "hunter2")
    monkeypatch.setenv("OKENGINE_TRUST", "private")
    monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")
    monkeypatch.setenv("OKENGINE_EDITING", "0")   # okengine#257: hardened requires UI editing off
    m.check_auth()
    rows = _hardening_rows(m)
    assert rows and all(lvl == "INFO" for lvl, _ in rows), rows


def test_gate_silent_when_mode_off(tmp_path, monkeypatch):
    m = _dv()
    m.F.clear()
    m.DATA = tmp_path
    for k in ("OKENGINE_HARDENED", "API_SERVER_ENABLED", "API_SERVER_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OKENGINE_TRUST", "private")
    monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")
    monkeypatch.setenv("OKENGINE_MCP_TOKEN", "")  # unsafe, but mode is OFF
    m.check_auth()
    assert _hardening_rows(m) == []


def _write_ts_config(tmp_path, *tools):
    body = "platform_toolsets:\n  api_server:\n" + "".join(f"    - {t}\n" for t in tools)
    (tmp_path / "config.yaml").write_text(body)


def _auth_fails(m):
    return [msg for lvl, area, msg in m.F if lvl == "FAIL" and area == "auth"]


def test_gate_flags_editing_off_but_write_still_in_toolset(tmp_path, monkeypatch):
    """okengine#257: editing off but okengine-write STILL in the api_server toolset = the switch
    didn't take (config not reconciled / gateway not recreated) -> FAIL."""
    m = _dv()
    m.F.clear(); m.DATA = tmp_path
    _write_ts_config(tmp_path, "okengine", "okengine-write")
    monkeypatch.delenv("OKENGINE_HARDENED", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "k")       # chat enabled
    monkeypatch.setenv("OKENGINE_EDITING", "0")     # editing OFF
    m.check_auth()
    assert any("OKENGINE_EDITING is off but okengine-write" in x for x in _auth_fails(m)), m.F


def test_gate_editing_on_with_write_present_is_fine(tmp_path, monkeypatch):
    m = _dv()
    m.F.clear(); m.DATA = tmp_path
    _write_ts_config(tmp_path, "okengine", "okengine-write")
    monkeypatch.delenv("OKENGINE_HARDENED", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("OKENGINE_EDITING", "1")     # editing ON -> write present is correct
    m.check_auth()
    assert not any("OKENGINE_EDITING" in x for x in _auth_fails(m)), m.F


def test_gate_editing_off_and_write_dropped_is_clean(tmp_path, monkeypatch):
    """The post-ensure-runtime steady state: editing off, okengine-write already dropped -> no FAIL."""
    m = _dv()
    m.F.clear(); m.DATA = tmp_path
    _write_ts_config(tmp_path, "okengine")          # read-only toolset
    monkeypatch.delenv("OKENGINE_HARDENED", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("OKENGINE_EDITING", "0")
    m.check_auth()
    assert not any("OKENGINE_EDITING" in x for x in _auth_fails(m)), m.F
