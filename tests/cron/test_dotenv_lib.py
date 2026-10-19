"""okengine#991: the one host-side `.env` reader parses exactly what `docker compose` env_file gives
the containers. A security switch decided on a different reading than the containers' is the
fail-open this pins: `OKENGINE_EDITING=0 # read-only` read as "0 # read-only" left the chat
writer bound while the gateway and reader saw editing OFF."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "cron"))
import dotenv_lib as D  # noqa: E402

# Every expected value below was read back from `docker compose config` (v2.39.1).
_ROWS = [
    ("OKENGINE_EDITING=0 # read-only on the LAN", "0"),
    ("export OKENGINE_EDITING=0", "0"),
    ('OKENGINE_EDITING="0"  # off', "0"),
    ("OKENGINE_EDITING='0' # off", "0"),
    ('OKENGINE_EDITING="0"#off', "0"),
    ("OKENGINE_EDITING=0\r", "0"),
    ("OKENGINE_EDITING=0#nospace", "0#nospace"),
    ("OKENGINE_EDITING=0\t# tab is not a comment", "0\t# tab is not a comment"),
    ("OKENGINE_EDITING= # c", "# c"),
    ('OKENGINE_EDITING="a#b"', "a#b"),
    ("OKENGINE_EDITING=a b  ", "a b"),
    ("OKENGINE_EDITING = 3", "3"),
    ("OKENGINE_EDITING: 1", "1"),
    ("   OKENGINE_EDITING=5", "5"),
    ("export  OKENGINE_EDITING=1", "1"),
    ('OKENGINE_EDITING="esc\\"q"', 'esc"q'),
    ('OKENGINE_EDITING="x\\ty"', "x\ty"),
    ("OKENGINE_EDITING=x\\ty", "x\\ty"),
    ("OKENGINE_EDITING='x\\ty'", "x\\ty"),
    ('OKENGINE_EDITING="multi\nline"', "multi\nline"),
    ('OKENGINE_EDITING="a\\qb"', "a\\qb"),
    ('OKENGINE_EDITING="a\\ b"', "a\\ b"),
    ('OKENGINE_EDITING="a\\\\b"', "a\\b"),
    ('OKENGINE_EDITING="\\a"', "\x07"),
    ("OKENGINE_EDITING=a\\\\b", "a\\\\b"),
    ("OKENGINE_EDITING=", ""),
    ("OKENGINE_EDITING=1\nOKENGINE_EDITING=off", "off"),
]


@pytest.mark.parametrize(("text", "expected"), _ROWS)
def test_each_dotenv_spelling_reads_as_compose_reads_it(text, expected):
    assert D.parse_dotenv(text + "\n")["OKENGINE_EDITING"] == expected


@pytest.mark.parametrize("text", ["# OKENGINE_EDITING=0", "  # OKENGINE_EDITING=0", "",
                                  "OKENGINE_EDITING", "OKENGINE_EDITING\nX=1",
                                  "BAD KEY!=1\n", 'OKENGINE_EDITING="unterminated'])
def test_non_assignments_set_nothing(text):
    assert "OKENGINE_EDITING" not in D.parse_dotenv(text)


@pytest.mark.parametrize("text", ["OKENGINE_EDITING\n", "OKENGINE_EDITING"])
def test_a_bare_key_inherits_from_the_lookup(text):
    assert D.parse_dotenv(text, {"OKENGINE_EDITING": "0"}) == {"OKENGINE_EDITING": "0"}


@pytest.mark.parametrize("text", ['A="x" B=1\n', 'A="x"B=1\n', 'A="xy" B=1\n'])
def test_a_statement_may_follow_a_closing_quote_on_the_same_line(text):
    assert D.parse_dotenv(text) == {"A": text[3:text.index('"', 3)], "B": "1"}


def test_export_as_a_whole_line_does_not_crash():
    assert isinstance(D.parse_dotenv("A=1\nexport"), dict)


def test_an_invalid_statement_does_not_hide_the_next_one():
    assert D.parse_dotenv("BAD KEY!=1\nOKENGINE_EDITING=0\n") == {"OKENGINE_EDITING": "0"}


@pytest.mark.parametrize(("text", "expected"), [
    ("A=1\nB=${A}", "1"), ("B=$UNSET_X", ""), ("B=${UNSET_X:-d}", "d"), ("B=${UNSET_X-d}", "d"),
    ("A=\nB=${A:-d}", "d"), ("A=\nB=${A-d}", ""), ('B="${L}x"', "lx"), ("B='${L}'", "${L}"),
    ('B="\\$L"', "$L"), ("B=\\$L", "\\l"), ("A=1\nB=$A", "1"),
])
def test_interpolation(text, expected):
    assert D.parse_dotenv(text, {"L": "l"})["B"] == expected


def test_a_byte_order_mark_is_not_part_of_the_first_key():
    assert D.parse_dotenv("\ufeffOKENGINE_EDITING=0") == {"OKENGINE_EDITING": "0"}


def test_read_dotenv_of_a_missing_or_unreadable_path_is_empty(tmp_path):
    assert D.read_dotenv(tmp_path / "absent") == {}
    assert D.read_dotenv(tmp_path) == {}            # a directory: OSError
    (tmp_path / ".env").write_text("OKENGINE_EDITING=0 # c\n")
    assert D.read_dotenv(tmp_path / ".env") == {"OKENGINE_EDITING": "0"}


#: Set by the `dotenv-compose-parity` CI job (okengine#994 [18]). The parity tests skip where there is
#: no `docker compose`, which was EVERY CI lane, so a compose grammar change was never detected. Where
#: this is set, a missing compose FAILS the test instead.
REQUIRE_PARITY = "OKENGINE_REQUIRE_COMPOSE_PARITY"


def _docker_compose() -> str:
    """The docker binary when `docker compose` works; otherwise skip, or FAIL where parity is
    required. `compose config` only parses, so no daemon is needed."""
    docker = shutil.which("docker")
    if docker is not None and subprocess.run([docker, "compose", "version"], capture_output=True,
                                             timeout=60).returncode == 0:
        return docker
    if os.environ.get(REQUIRE_PARITY) == "1":
        pytest.fail(f"{REQUIRE_PARITY}=1 but `docker compose` is unavailable: the dotenv parity "
                    "check would be skipped, which is not a pass")
    pytest.skip("docker compose is not available here; dotenv parity unverified on this host")


def test_a_required_parity_run_fails_rather_than_skips_without_compose(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv(REQUIRE_PARITY, "1")
    with pytest.raises(pytest.fail.Exception, match="not a pass"):
        _docker_compose()
    monkeypatch.delenv(REQUIRE_PARITY)
    with pytest.raises(pytest.skip.Exception):
        _docker_compose()


def test_a_broken_compose_plugin_counts_as_unavailable(monkeypatch, tmp_path):
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda _name: str(fake))
    monkeypatch.setenv(REQUIRE_PARITY, "1")
    with pytest.raises(pytest.fail.Exception):
        _docker_compose()


_PARITY = "\n".join(
    [f"K{i}{t.split('OKENGINE_EDITING', 1)[1]}" if t.startswith("OKENGINE_EDITING") else
     t.replace("OKENGINE_EDITING", f"K{i}", 1)
     for i, (t, _e) in enumerate(_ROWS[:-1])]
    + ["DUP=1", "DUP=2", "IA=1", "IB=${IA}", "IC=${PARITY_UNSET:-d}", 'ID="${PARITY_SET}x"',
       'QA="x" QB=1', 'QG="xy"QH=1', "QC=$IA", 'QD="\\$IA"', "QE=\\$IA", 'QF="\\q|\\ |\\a|\\\\"',
       "IE='${PARITY_SET}'", "INHERIT_ME", "# COMMENTED=1", "Z=last"]) + "\n"


def test_parity_with_docker_compose_config(tmp_path):
    """The table above was transcribed from compose; this re-asks compose directly, so a compose
    upgrade that changes the grammar fails here instead of drifting silently."""
    docker = _docker_compose()
    (tmp_path / "parity.env").write_text(_PARITY, encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  x:\n    image: alpine\n    env_file: [parity.env]\n")
    lookup = {"PARITY_SET": "s", "INHERIT_ME": "inherited"}
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST",
                                                         "DOCKER_CONFIG", "XDG_RUNTIME_DIR")}
    result = subprocess.run([docker, "compose", "config", "--format", "json"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=120, env={**env, **lookup})
    assert result.returncode == 0, result.stderr
    # `config` prints a compose file, so it re-escapes a literal `$` as `$$`.
    compose = {k: v.replace("$$", "$")
               for k, v in json.loads(result.stdout)["services"]["x"]["environment"].items()}
    assert D.parse_dotenv(_PARITY, lookup) == compose


# --- okengine#993 R1: `${VAR}` resolves in compose's order --------------------------------------
# compose-go resolves an env_file's `${X}` from the invoking process environment first, then the
# project `.env` (the whole file, parsed on its own), and only then from earlier keys of the
# env_file itself; the project `.env` is parsed with the process environment first, then its own
# earlier keys. The pack's `.env` is both the project .env and the gateway's env_file, so a forward
# reference resolves there and a process value beats an earlier line. Every row below was read
# back from `docker compose config` (v2.39.1); the live parity test re-asks compose where docker
# exists, and this table carries the cases into CI, where it does not.

#: (pack .env text, invoking process environment, the container environment compose builds)
_PACK_ROWS = [
    ("A=1\nB=${A}\n", {}, {"A": "1", "B": "1"}),
    ("A=1\nB=${A}\n", {"A": "2"}, {"A": "1", "B": "2"}),
    ('A=1\nB="${A}q"\nC=${A:-zz}\n', {"A": "2"}, {"A": "1", "B": "2q", "C": "2"}),
    ("B=${FWD}\nFWD=later\n", {}, {"B": "later", "FWD": "later"}),
    ("B=${FWD}\nFWD=later\n", {"FWD": "proc"}, {"B": "proc", "FWD": "later"}),
    ("B=${FWD:-d}\nC=${FWD-d}\nFWD=later\n", {}, {"B": "later", "C": "later", "FWD": "later"}),
    ("B=${PROC_ONLY}\n", {"PROC_ONLY": "p"}, {"B": "p"}),
    ("B=${PROC_ONLY}\n", {}, {"B": ""}),
    ("A=${FWD}\nB=${A}\nFWD=later\n", {}, {"A": "later", "B": "", "FWD": "later"}),
    ("B=${C}\nC=${D}\nD=1\n", {}, {"B": "", "C": "1", "D": "1"}),
    ("OKENGINE_EDITING=${EDIT}\nEDIT=0\n", {}, {"OKENGINE_EDITING": "0", "EDIT": "0"}),
]


@pytest.mark.parametrize(("text", "environ", "expected"), _PACK_ROWS)
def test_the_pack_env_resolves_as_compose_resolves_it(text, environ, expected):
    assert D.compose_env(text, environ, project_text=text) == expected


def test_read_compose_env_reads_the_pack_shape(tmp_path):
    (tmp_path / ".env").write_text("B=${FWD}\nFWD=later\n")
    assert D.read_compose_env(tmp_path / ".env", {}) == {"B": "later", "FWD": "later"}
    assert D.read_compose_env(tmp_path / "absent", {"X": "1"}) == {}


@pytest.mark.parametrize(("text", "environ", "project", "expected"), [
    # an env_file that is not the project .env: no forward references, process before earlier keys
    ("A=1\nB=${A}\nC=${FWD}\nFWD=x\n", {}, None, {"A": "1", "B": "1", "C": "", "FWD": "x"}),
    ("A=1\nB=${A}\n", {"A": "2"}, None, {"A": "1", "B": "2"}),
    ("B=${A}\nA=3\n", {}, None, {"A": "3", "B": ""}),
    # a separate project .env beats the env_file's own earlier key
    ("A=1\nB=${A}\nC=${Q}\n", {}, "A=fromdotenv\n", {"A": "1", "B": "fromdotenv", "C": ""}),
    # a bare key inherits from the project .env too
    ("Q\nR=${Q}\n", {}, "Q=fromproj\n", {"Q": "fromproj", "R": "fromproj"}),
])
def test_an_env_file_resolves_process_then_project_then_earlier_keys(text, environ, project,
                                                                       expected):
    assert D.compose_env(text, environ, project_text=project) == expected


def test_the_lookup_beats_an_earlier_key_of_the_same_file():
    assert D.parse_dotenv("A=1\nB=${A}\n", {"A": "2"}) == {"A": "1", "B": "2"}


@pytest.mark.parametrize(("text", "environ"), [(t, e) for t, e, _x in _PACK_ROWS])
def test_pack_env_parity_with_docker_compose_config(tmp_path, text, environ):
    """Re-asks compose for each transcribed row: the file is both the project .env and the
    service's env_file, run under the row's process environment."""
    docker = _docker_compose()
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  x:\n    image: alpine\n    env_file: [.env]\n")
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST",
                                                          "DOCKER_CONFIG", "XDG_RUNTIME_DIR")}
    result = subprocess.run([docker, "compose", "config", "--format", "json"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=120, env={**base, **environ})
    assert result.returncode == 0, result.stderr
    compose = {k: v.replace("$$", "$")
               for k, v in json.loads(result.stdout)["services"]["x"]["environment"].items()}
    assert D.compose_env(text, environ, project_text=text) == compose



# --- okengine#994 [17]/A: the CLI shell callers use instead of a grep/cut of their own ---------------


def test_cli_get_prints_the_compose_value_without_a_newline(tmp_path, capsys):
    (tmp_path / ".env").write_text("export T='abc' # rotated\nT=${BASE}x\nBASE=b\n")
    assert D.main(["get", str(tmp_path / ".env"), "T"], environ={}) == 0
    assert capsys.readouterr().out == "bx"


def test_cli_get_takes_interpolation_from_the_given_environment(tmp_path, capsys):
    (tmp_path / ".env").write_text("T=${FROM_PROC}\n")
    assert D.main(["get", str(tmp_path / ".env"), "T"], environ={"FROM_PROC": "p"}) == 0
    assert capsys.readouterr().out == "p"


def test_cli_get_defaults_to_the_process_environment(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOTENV_LIB_CLI_PROC", "q")
    (tmp_path / ".env").write_text("T=${DOTENV_LIB_CLI_PROC}\n")
    assert D.main(["get", str(tmp_path / ".env"), "T"]) == 0
    assert capsys.readouterr().out == "q"


@pytest.mark.parametrize("text", ["OTHER=1\n", "T=\n", 'T=""\n', "T='unterminated\n"])
def test_cli_get_misses_on_an_unset_or_empty_key(tmp_path, capsys, text):
    (tmp_path / ".env").write_text(text)
    assert D.main(["get", str(tmp_path / ".env"), "T"], environ={}) == 1
    assert capsys.readouterr().out == ""


def test_cli_get_misses_on_a_missing_file(tmp_path, capsys):
    assert D.main(["get", str(tmp_path / "absent"), "T"], environ={}) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("argv", [[], ["get"], ["get", "p"], ["set", "p", "K"], ["GET", "p", "K"],
                                  ["get", "p", "K", "extra"]])
def test_cli_rejects_a_malformed_command_line(capsys, argv):
    assert D.main(argv, environ={}) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "usage: dotenv_lib.py get" in captured.err


def test_cli_entry_point_exits_with_the_get_status(tmp_path, monkeypatch, capsys):
    import runpy
    (tmp_path / ".env").write_text("T=v\n")
    script = str(REPO / "scripts" / "cron" / "dotenv_lib.py")
    monkeypatch.setattr(sys, "argv", [script, "get", str(tmp_path / ".env"), "T"])
    with pytest.raises(SystemExit) as hit:
        runpy.run_path(script, run_name="__main__")
    assert hit.value.code == 0 and capsys.readouterr().out == "v"
    monkeypatch.setattr(sys, "argv", [script, "get", str(tmp_path / ".env"), "U"])
    with pytest.raises(SystemExit) as miss:
        runpy.run_path(script, run_name="__main__")
    assert miss.value.code == 1


def test_cli_runs_as_a_script(tmp_path):
    (tmp_path / ".env").write_text("T=1 # c\nT=2 # last wins\n")
    script = REPO / "scripts" / "cron" / "dotenv_lib.py"
    hit = subprocess.run([sys.executable, str(script), "get", str(tmp_path / ".env"), "T"],
                         capture_output=True, text=True, timeout=30)
    miss = subprocess.run([sys.executable, str(script), "get", str(tmp_path / ".env"), "U"],
                          capture_output=True, text=True, timeout=30)
    assert (hit.returncode, hit.stdout) == (0, "2")
    assert (miss.returncode, miss.stdout) == (1, "")


# --- compose_default: `${KEY:-default}` as compose interpolates it (okengine#1042 [36]) ----------

@pytest.mark.parametrize("environ, dotenv, want", [
    ({}, "TZ=America/New_York\n", "America/New_York"),          # the pack .env
    ({"TZ": "Europe/Paris"}, "TZ=America/New_York\n", "Europe/Paris"),   # the shell wins
    ({"TZ": ""}, "TZ=America/New_York\n", "UTC"),               # exported empty: `:-` default
    ({}, "TZ=\n", "UTC"),
    ({}, None, "UTC"),
    ({"ZONE": "Asia/Tokyo"}, "TZ=${ZONE}\n", "Asia/Tokyo"),       # .env interpolates from the shell
], ids=["dotenv", "shell-wins", "exported-empty", "dotenv-empty", "no-dotenv", "interpolated"])
def test_compose_default_interpolates_like_compose(tmp_path, environ, dotenv, want):
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    assert D.compose_default("TZ", environ, tmp_path / ".env", "UTC") == want
