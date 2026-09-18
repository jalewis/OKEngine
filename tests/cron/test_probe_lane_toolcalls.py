"""Pre-flight probe for silently-dropped tool calls (okengine#518 follow-up).

A serving stack can generate a correct tool call, fail to parse it, and return it as prose with
`finish_reason: stop`. The run looks successful and writes nothing. `detect_narrated_tool_calls`
(#477) catches that after a lane has burned a run; this catches it before.

The tests here are mostly about the VERDICTS, because the whole value of the probe is that it
does not report a false pass:

  * a narrated call is a failure, not a success — that is the exact bug;
  * an unreachable endpoint is UNKNOWN (exit 2), never a pass. An empty parse read as
    "nothing happened" is its own documented incident class (#477);
  * a lane with no bound tools is skipped rather than counted as healthy.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
PROBE = REPO / "scripts" / "cron" / "probe_lane_toolcalls.py"


def _load():
    spec = importlib.util.spec_from_file_location("probe_lane", PROBE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["probe_lane"] = m
    spec.loader.exec_module(m)
    return m


class _Tool:
    def __init__(self, name, desc="d", schema=None):
        self.name, self.description = name, desc
        self.inputSchema = schema or {"type": "object", "properties": {}}


# --------------------------------------------------------------- verdicts


def _resp(*, calls=0, text=""):
    out = []
    for _ in range(calls):
        out.append({"type": "function_call", "name": "x", "arguments": "{}"})
    if text:
        out.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
    return {"output": out}


def _probe_with(monkeypatch, m, response):
    class _R:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return json.dumps(response).encode()

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _R())
    return m.probe("http://x/v1", "m", [{"name": "t"}], 5)


def test_a_structured_call_passes(monkeypatch):
    m = _load()
    assert _probe_with(monkeypatch, m, _resp(calls=1)) == "structured"


def test_a_narrated_call_is_a_failure(monkeypatch):
    """The bug itself: a valid call delivered as text. Counting it as success would make the
    probe agree with the broken stack."""
    m = _load()
    body = "<function=score_source>\n<parameter=path>\nx\n</parameter>\n</function>\n</tool_call>"
    assert _probe_with(monkeypatch, m, _resp(text=body)) == "narrated"


def test_no_call_and_no_narration_is_silent(monkeypatch):
    m = _load()
    assert _probe_with(monkeypatch, m, _resp(text="I cannot do that.")) == "silent"


def test_an_unreachable_endpoint_is_not_a_pass(monkeypatch):
    m = _load()

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(m.urllib.request, "urlopen", _boom)
    assert m.probe("http://x/v1", "m", [{"name": "t"}], 1) == "unreachable"


# ------------------------------------------------------------ exit statuses


def _cfg(tmp_path, servers):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: test-model\n  base_url: http://endpoint/v1\n"
        "mcp_servers:\n" + servers, encoding="utf-8")
    return tmp_path


def _no_coroutine(monkeypatch, m, tools):
    """Patch the coroutine itself, not just asyncio.run — otherwise the coroutine object is
    created and never awaited, and pytest rightly warns about it."""
    monkeypatch.setattr(m, "_tools_for", lambda actor: [_Tool(t) for t in tools])
    monkeypatch.setattr(m, "asyncio", type("A", (), {"run": staticmethod(lambda v: v)})())


def _wire(monkeypatch, m, verdict, tools=("score_source",)):
    _no_coroutine(monkeypatch, m, tools)
    monkeypatch.setattr(m, "probe", lambda *a, **k: verdict)


ONE = ("  okengine-write-sq:\n    env:\n"
       "      OKENGINE_WRITE_ACTOR: cron:source-quality-backfill\n")


def test_all_structured_exits_zero(tmp_path, monkeypatch, capsys):
    m = _load()
    _wire(monkeypatch, m, "structured")
    assert m.main(["--data", str(_cfg(tmp_path, ONE)), "--reps", "2"]) == 0


def test_a_narrated_lane_exits_one(tmp_path, monkeypatch, capsys):
    m = _load()
    _wire(monkeypatch, m, "narrated")
    assert m.main(["--data", str(_cfg(tmp_path, ONE)), "--reps", "2"]) == 1
    assert "DEGRADED" in capsys.readouterr().err


def test_an_unreachable_endpoint_exits_two(tmp_path, monkeypatch, capsys):
    """Must not be mistaken for green: no data is not data saying yes."""
    m = _load()
    _wire(monkeypatch, m, "unreachable")
    assert m.main(["--data", str(_cfg(tmp_path, ONE)), "--reps", "2"]) == 2
    err = capsys.readouterr().err
    assert "UNKNOWN" in err and "NOT a pass" in err


def test_a_lane_with_no_bound_tools_is_skipped_not_passed(tmp_path, monkeypatch, capsys):
    m = _load()
    _wire(monkeypatch, m, "structured", tools=())
    assert m.main(["--data", str(_cfg(tmp_path, ONE)), "--reps", "2"]) == 0
    assert "no tools bound" in capsys.readouterr().out


def test_a_non_http_base_url_is_refused(tmp_path, capsys):
    """`base_url` is config input to urlopen, which opens `file:` happily (bandit B310 /
    CWE-22). A probe that read a local file and still printed a verdict would be worse than
    one that fails."""
    m = _load()
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: m\n  base_url: file:///etc/passwd\n"
        "mcp_servers:\n" + ONE, encoding="utf-8")
    assert m.main(["--data", str(tmp_path)]) == 2
    assert "must be http(s)" in capsys.readouterr().err


def test_scheme_check_accepts_only_http_schemes():
    m = _load()
    assert m.is_http_url("http://h:1/v1") and m.is_http_url("https://h/v1")
    for bad in ("file:///etc/passwd", "ftp://h/x", "gopher://h", "", "/opt/data"):
        assert not m.is_http_url(bad), bad


TWO_SHARING = (
    "  okengine-write-a:\n    env:\n      OKENGINE_WRITE_ACTOR: cron:lane-a\n"
    "  okengine-write-b:\n    env:\n      OKENGINE_WRITE_ACTOR: cron:lane-b\n")


def test_identical_arrays_are_probed_once(tmp_path, monkeypatch, capsys):
    """43 of 52 live lanes ship the same array. Probing each lane separately is redundant
    generations, and a naive sweep saturated the 2-slot endpoint and timed ITSELF out."""
    m = _load()
    _no_coroutine(monkeypatch, m, ("same_tool",))
    monkeypatch.setattr(m, "build_array", lambda key, tools: [{"type": "function",
                                                               "name": "mcp__shared__same_tool"}])
    calls = []
    monkeypatch.setattr(m, "probe", lambda *a, **k: calls.append(1) or "structured")
    assert m.main(["--data", str(_cfg(tmp_path, TWO_SHARING)), "--reps", "3"]) == 0
    assert len(calls) == 3, f"one array x 3 reps, not two lanes x 3: {len(calls)}"
    out = capsys.readouterr().out
    assert "2 lane(s) -> 1 distinct tool array(s)" in out


def test_different_arrays_are_probed_separately(tmp_path, monkeypatch, capsys):
    m = _load()
    _no_coroutine(monkeypatch, m, ("t",))
    seen = {"n": 0}

    def _arr(key, tools):
        seen["n"] += 1
        return [{"type": "function", "name": f"mcp__{key}__t"}]

    monkeypatch.setattr(m, "build_array", _arr)
    calls = []
    monkeypatch.setattr(m, "probe", lambda *a, **k: calls.append(1) or "structured")
    assert m.main(["--data", str(_cfg(tmp_path, TWO_SHARING)), "--reps", "2"]) == 0
    assert len(calls) == 4, "distinct arrays must each be probed"
    assert "2 distinct tool array(s)" in capsys.readouterr().out


def test_a_failing_shared_array_flags_every_lane_that_ships_it(tmp_path, monkeypatch, capsys):
    """Deduping must not hide blast radius: one bad array degrades all its lanes."""
    m = _load()
    _no_coroutine(monkeypatch, m, ("same_tool",))
    monkeypatch.setattr(m, "build_array", lambda key, tools: [{"name": "shared"}])
    monkeypatch.setattr(m, "probe", lambda *a, **k: "narrated")
    assert m.main(["--data", str(_cfg(tmp_path, TWO_SHARING)), "--reps", "1"]) == 1
    err = capsys.readouterr().err
    assert "cron:lane-a" in err and "cron:lane-b" in err


def test_missing_model_config_exits_two(tmp_path):
    m = _load()
    (tmp_path / "config.yaml").write_text("mcp_servers: {}\n", encoding="utf-8")
    assert m.main(["--data", str(tmp_path)]) == 2


def test_no_write_identities_exits_two(tmp_path):
    """Finding nothing to probe is not the same as everything being healthy."""
    m = _load()
    cfg = _cfg(tmp_path, "  plain-server:\n    command: x\n")
    assert m.main(["--data", str(cfg)]) == 2


# ------------------------------------------------------------------ wiring


def test_lane_identities_reads_the_write_actor(tmp_path):
    m = _load()
    import yaml
    cfg = yaml.safe_load((_cfg(tmp_path, ONE) / "config.yaml").read_text())
    assert m.lane_identities(cfg) == [("okengine-write-sq", "cron:source-quality-backfill")]


def test_tool_names_get_the_mcp_prefix_the_agent_sees():
    """#518 §10: name shape is part of the array under test, so the probe must not send a bare
    tool name where the agent would receive `mcp__server__tool`."""
    m = _load()
    arr = m.build_array("okengine-write-source-quality", [_Tool("score_source")])
    assert arr[0]["name"] == "mcp__okengine_write_source_quality__score_source"


def test_a_transient_unreachable_is_retried_before_being_believed(tmp_path, monkeypatch, capsys):
    """A full sweep is dozens of generations against a 2-slot endpoint under admission control,
    so it can queue past the timeout and time ITSELF out. That is load, not a broken endpoint —
    it happened on three packs during the first live run. One retry, then believe it."""
    m = _load()
    _no_coroutine(monkeypatch, m, ("t",))
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)
    seq = iter(["unreachable", "structured"])
    monkeypatch.setattr(m, "probe", lambda *a, **k: next(seq))
    assert m.main(["--data", str(_cfg(tmp_path, ONE)), "--reps", "1"]) == 0


def test_a_persistently_unreachable_endpoint_still_fails(tmp_path, monkeypatch, capsys):
    """The retry must not paper over a genuinely dead endpoint."""
    m = _load()
    _no_coroutine(monkeypatch, m, ("t",))
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)
    monkeypatch.setattr(m, "probe", lambda *a, **k: "unreachable")
    assert m.main(["--data", str(_cfg(tmp_path, ONE)), "--reps", "1"]) == 2
    assert "NOT a pass" in capsys.readouterr().err


# ---------------------------------------------------------------- cron lane


def _cron_env(tmp_path, monkeypatch, m, verdict):
    _wire(monkeypatch, m, verdict)
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    return _cfg(tmp_path, ONE)


def test_cron_mode_emits_the_no_agent_contract(tmp_path, monkeypatch, capsys):
    """cron-plus gates a no_agent lane on this JSON; without it the run is not silent."""
    m = _load()
    data = _cron_env(tmp_path, monkeypatch, m, "structured")
    assert m.main(["--data", str(data), "--reps", "1", "--cron",
                   "--wiki", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_cron_mode_logs_a_bad_verdict_to_the_vault(tmp_path, monkeypatch, capsys):
    """The signal has to survive the run. A non-zero exit alone scrolls past."""
    m = _load()
    data = _cron_env(tmp_path, monkeypatch, m, "narrated")
    assert m.main(["--data", str(data), "--reps", "1", "--cron",
                   "--wiki", str(tmp_path)]) == 1
    log = (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "lane-toolcall-probe" in log and "DEGRADED" in log


def test_cron_mode_does_not_log_on_success(tmp_path, monkeypatch, capsys):
    m = _load()
    data = _cron_env(tmp_path, monkeypatch, m, "structured")
    m.main(["--data", str(data), "--reps", "1", "--cron", "--wiki", str(tmp_path)])
    assert not (tmp_path / "wiki" / "log.md").exists()


def test_cron_mode_never_wakes_the_agent(tmp_path, monkeypatch, capsys):
    """A dropped tool call is a serving-stack fault; waking a model to restate it burns a turn
    and fixes nothing. The verdict goes to the log and the exit code."""
    m = _load()
    data = _cron_env(tmp_path, monkeypatch, m, "narrated")
    m.main(["--data", str(data), "--reps", "1", "--cron", "--wiki", str(tmp_path)])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["wakeAgent"] is False


def test_interactive_mode_stays_quiet(tmp_path, monkeypatch, capsys):
    """Without --cron nothing is written and no contract line is printed."""
    m = _load()
    data = _cron_env(tmp_path, monkeypatch, m, "narrated")
    assert m.main(["--data", str(data), "--reps", "1", "--wiki", str(tmp_path)]) == 1
    assert "wakeAgent" not in capsys.readouterr().out
    assert not (tmp_path / "wiki" / "log.md").exists()


def test_tools_for_reimports_server_for_each_actor(monkeypatch):
    m = _load()
    # Register the key with monkeypatch before production mutates os.environ so
    # teardown restores the suite-wide authenticated-caller state.
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "")

    class MCP:
        async def list_tools(self):
            return ["tool"]

    fake = type("Server", (), {"mcp": MCP()})()
    monkeypatch.setitem(sys.modules, "write_server", fake)
    # Removing the module is deliberate production behavior; supply it through import.
    real_import = __import__

    def import_fake(name, *args, **kwargs):
        return fake if name == "write_server" else real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", import_fake)
    assert m.asyncio.run(m._tools_for("cron:x")) == ["tool"]
    assert m.os.environ["OKENGINE_WRITE_ACTOR"] == "cron:x"


def test_probe_rejects_malformed_url_and_ignores_non_dict_content(monkeypatch):
    m = _load()
    assert not m.is_http_url("http://[")
    assert m.probe("file:///tmp/x", "m", [], 1) == "unreachable"
    assert _probe_with(monkeypatch, m, {
        "output": [{"type": "message", "content": ["plain", {"text": ""}]}]
    }) == "silent"


def test_bad_config_and_tool_enumeration_are_unknown(tmp_path, monkeypatch, capsys):
    m = _load()
    (tmp_path / "config.yaml").write_text("model: [", encoding="utf-8")
    assert m.main(["--data", str(tmp_path)]) == 2

    data = _cfg(tmp_path, ONE)
    monkeypatch.setattr(m, "_tools_for", lambda _actor: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(m, "asyncio", type("A", (), {"run": staticmethod(lambda v: v)})())
    assert m.main(["--data", str(data), "--reps", "1"]) == 2
    assert "could not list tools" in capsys.readouterr().err


def test_cron_log_write_failure_is_reported(tmp_path, monkeypatch, capsys):
    m = _load()
    args = type("Args", (), {"cron": True, "wiki": str(tmp_path)})()
    assert m._finish(args, 2, "bad") == 2
    assert "could not write" in capsys.readouterr().err


def test_cron_mode_and_reps_can_come_from_env(tmp_path, monkeypatch, capsys):
    """The deploy validates `script` with os.path.isfile, so a cron lane cannot pass arguments
    there — embedding them made the pre-write guard reject the whole job set. The lane carries
    them in its `env` block instead, so the defaults must honour it."""
    m = _load()
    data = _cron_env(tmp_path, monkeypatch, m, "structured")
    monkeypatch.setenv("OKENGINE_PROBE_CRON", "1")
    monkeypatch.setenv("OKENGINE_PROBE_REPS", "1")
    assert m.main(["--data", str(data), "--wiki", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_the_cron_lane_script_field_has_no_arguments():
    """Pins the shape the deploy guard requires: `script` must be a bare path that isfile()."""
    import json as _json
    jobs = _json.loads((REPO / "config" / "engine-crons.json").read_text(encoding="utf-8"))
    lane = next(j for j in jobs if j["name"] == "lane-toolcall-probe")
    assert " " not in lane["script"], "arguments in `script` fail the deploy's isfile check"
    assert lane["script"].endswith("probe_lane_toolcalls.py")
    assert lane["no_agent"] is True
    assert lane["env"]["OKENGINE_PROBE_CRON"] == "1"
