"""okengine#1044 [29]: post-deploy proves the running gateway carries the Hermes patch series.

The gateway was verified only by build markers (`.okengine_release`, `.hermes_pin`, the git_sha
label) that a non-engine image does not have, and a missing marker was skipped as a "pre-marker
image". A gateway started from an upstream hermes-agent tag or a hand-built image verified green
with none of the carried guards, patch 01 (the file-tool vault write guard) among them.

Acceptance replay (2026-10-12, read-only): all five live gateways report 23 of 23 patches present;
a Hermes worktree with the series only partly applied reports patches 14, 24 and 25 missing.
"""
from __future__ import annotations

import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "hermes_patch_probe.py"
VERIFY = REPO / "scripts" / "post_deploy_verify.sh"


def _load():
    spec = importlib.util.spec_from_file_location("hermes_patch_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()

PATCH_A = """\
diff --git a/tools/file_operations.py b/tools/file_operations.py
--- a/tools/file_operations.py
+++ b/tools/file_operations.py
@@ -1,3 +1,4 @@
 import os
+from tools.vault_guard import write_reject as _guard_write_reject
+    )
 def write(path):
-    return _raw_write(path)
+        guard = _guard_write_reject(path)
diff --git a/tools/vault_guard.py b/tools/vault_guard.py
new file mode 100644
--- /dev/null
+++ b/tools/vault_guard.py
@@ -0,0 +1,2 @@
+def write_reject(path):
+    return "NEW vault page must be created through the write MCP"
diff --git a/tests/tools/test_vault_guard.py b/tests/tools/test_vault_guard.py
--- /dev/null
+++ b/tests/tools/test_vault_guard.py
@@ -0,0 +1 @@
+def test_guard_is_only_in_the_upstream_test_tree(): pass
"""

PATCH_B = """\
diff --git a/tools/file_operations.py b/tools/file_operations.py
--- a/tools/file_operations.py
+++ b/tools/file_operations.py
@@ -2,2 +2,2 @@
-        guard = _guard_write_reject(path)
+        guard = _guard_write_reject(path, scope="vault")
diff --git a/cron/scheduler.py b/cron/scheduler.py
--- a/cron/scheduler.py
+++ b/cron/scheduler.py
@@ -1 +1,2 @@
+MAX_ITERATIONS_FROM_JOB = job.get("max_iterations")
"""

PATCHED_TREE = {
    "tools/file_operations.py": "import os\nfrom tools.vault_guard import write_reject as "
                                "_guard_write_reject\ndef write(path):\n"
                                "        guard = _guard_write_reject(path, scope=\"vault\")\n",
    "tools/vault_guard.py": "def write_reject(path):\n"
                            "    return \"NEW vault page must be created through the write MCP\"\n",
    "cron/scheduler.py": "MAX_ITERATIONS_FROM_JOB = job.get(\"max_iterations\")\n",
}


def _engine(tmp_path: Path, pin: str = "v2026.9.14", series: dict | None = None) -> Path:
    engine = tmp_path / "engine"
    target = engine / "patches" / f"target-{pin}"
    target.mkdir(parents=True)
    (engine / "engine-manifest.yaml").write_text(
        f"runtime:\n  hermes:\n    pinned_tag: {pin}\n  other:\n    pinned_tag: v0.0.0\n",
        encoding="utf-8")
    for name, body in (series or {"01-guard.patch": PATCH_A, "02-scope.patch": PATCH_B}).items():
        (target / name).write_text(body, encoding="utf-8")
    return engine


def _tar(tree: dict[str, str], files: list[str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in files:
            if name in tree:
                data = tree[name].encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _gateway(tree: dict[str, str] | None):
    asked = []

    def run(args):
        asked.append(args)
        if tree is None:
            return None
        return _tar(tree, args[args.index("-cf") + 2:])
    run.asked = asked
    return run


# --- the expectation model -------------------------------------------------------------------------

def test_expectations_skip_tests_short_lines_and_lines_a_later_patch_rewrites(tmp_path):
    engine = _engine(tmp_path)
    expected = mod.expectations(mod.series_dir(engine, "v2026.9.14"))
    texts = {text for _name, _path, text in expected}
    assert "guard = _guard_write_reject(path)" not in texts, "patch 02 rewrote this line"
    assert 'guard = _guard_write_reject(path, scope="vault")' in texts
    assert ")" not in texts, "a short line proves nothing"
    assert not any(path.startswith("tests/") for _name, path, _text in expected)
    assert {path for _name, path, _text in expected} == set(PATCHED_TREE)


def test_a_fully_patched_tree_misses_nothing(tmp_path):
    expected = mod.expectations(mod.series_dir(_engine(tmp_path), "v2026.9.14"))
    contents = {path: {line.strip() for line in body.splitlines()}
                for path, body in PATCHED_TREE.items()}
    assert mod.missing(expected, contents) == {}


def test_an_unpatched_tree_misses_every_patch(tmp_path):
    expected = mod.expectations(mod.series_dir(_engine(tmp_path), "v2026.9.14"))
    assert set(mod.missing(expected, {"tools/file_operations.py": {"import os"}})) == {
        "01-guard.patch", "02-scope.patch"}


def test_a_series_with_no_runtime_lines_expects_nothing(tmp_path):
    tests_only = "diff --git a/tests" + PATCH_A.split("diff --git a/tests", 1)[1]
    engine = _engine(tmp_path, series={"01-test-only.patch": tests_only})
    assert mod.expectations(mod.series_dir(engine, "v2026.9.14")) == []


# --- which series: the one apply.sh applies for the manifest pin ---------------------------------

def test_the_manifest_pin_is_the_first_pinned_tag(tmp_path):
    assert mod.manifest_pin(_engine(tmp_path, pin="v9.9.9")) == "v9.9.9"
    assert mod.manifest_pin(tmp_path / "absent") is None
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "engine-manifest.yaml").write_text("runtime: {}\n", encoding="utf-8")
    assert mod.manifest_pin(tmp_path / "bare") is None


def test_series_dir_agrees_with_every_apply_sh_case_arm():
    """apply.sh selects the series by a `case "$PIN"` table; the probe must check the same one."""
    text = (REPO / "patches" / "apply.sh").read_text(encoding="utf-8")
    arms = re.findall(r'^\s*(v[0-9.]+)\) PATCHDIR="\$PATCHROOT(/[^"]*)?"', text, re.M)
    assert arms, "apply.sh's case table moved; update this test"
    for pin, suffix in arms:
        assert mod.series_dir(REPO, pin) == REPO / "patches" / (suffix or "").lstrip("/"), pin
    assert mod.series_dir(REPO, mod.manifest_pin(REPO)) is not None, "no series for the live pin"


def test_series_dir_for_an_unknown_or_absent_pin_is_none(tmp_path):
    assert mod.series_dir(REPO, "v1999.1.1") is None
    assert mod.series_dir(REPO, None) is None
    assert mod.series_dir(tmp_path, "v1999.1.1") is None


def test_the_shipped_series_has_runtime_expectations_for_every_runtime_patch():
    series = mod.series_dir(REPO, mod.manifest_pin(REPO))
    expected = mod.expectations(series)
    covered = {name for name, _path, _text in expected}
    assert "01-file-operations-vault-guard.patch" in covered or \
        "01-file-operations-write-guard.patch" in covered, "patch 01 must be probed"
    assert len(covered) >= 10


# --- the CLI against a gateway --------------------------------------------------------------------

def test_a_gateway_carrying_the_series_passes(tmp_path, capsys):
    run = _gateway(PATCHED_TREE)
    assert mod.main(["--engine-dir", str(_engine(tmp_path))], run) == 0
    assert "2 of 2 carried patch(es) present in gateway" in capsys.readouterr().out
    assert run.asked[0][:6] == ["docker", "compose", "exec", "-T", "gateway", "tar"]
    assert run.asked[0][6:9] == ["-C", "/opt/hermes", "-cf"]


def test_a_gateway_missing_a_patch_fails_and_names_it(tmp_path, capsys):
    tree = {**PATCHED_TREE, "cron/scheduler.py": "pass\n"}
    assert mod.main(["--engine-dir", str(_engine(tmp_path)), "--service", "gw"],
                    _gateway(tree)) == 1
    out = capsys.readouterr().out
    assert "1 of 2 carried patch(es) present in gw" in out
    assert "MISSING 02-scope.patch: 1 of 2 added line(s) absent, e.g. cron/scheduler.py" in out


def test_an_upstream_gateway_with_no_patched_files_fails(tmp_path, capsys):
    assert mod.main(["--engine-dir", str(_engine(tmp_path))], _gateway({})) == 1
    assert "0 of 2 carried patch(es)" in capsys.readouterr().out


@pytest.mark.parametrize("answer", [None, b"not a tar stream"], ids=["no-exec", "garbage"])
def test_an_unreadable_gateway_is_undetectable(tmp_path, capsys, answer):
    assert mod.main(["--engine-dir", str(_engine(tmp_path))], lambda args: answer) == 2
    assert "UNDETECTABLE" in capsys.readouterr().err


def test_no_series_for_the_pin_is_undetectable(tmp_path, capsys):
    engine = _engine(tmp_path)
    shutil.rmtree(engine / "patches")
    assert mod.main(["--engine-dir", str(engine)], _gateway(PATCHED_TREE)) == 2
    assert "no patch series found for Hermes pin 'v2026.9.14'" in capsys.readouterr().err


def test_the_default_runner_returns_stdout_even_on_a_nonzero_exit():
    """GNU tar exits 2 when a requested file is absent; the archive of the rest is the answer."""
    out = mod._run([sys.executable, "-c", "import sys; sys.stdout.write('x'); sys.exit(2)"])
    assert out == b"x"
    assert mod._run([sys.executable, "-c", "pass"]) is None
    assert mod._run(["/nonexistent/binary"]) is None


# --- real invocation: the verifier's step runs the probe as it runs in a deployment -------------

needs_bash = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("tar"),
                                reason="bash/tar not available")


def _verify_with_gateway_tree(tmp_path: Path, tree: dict[str, str] | None) -> str:
    """post_deploy_verify.sh from a stub engine (this repo, with a synthetic patch series), against
    a stub docker whose gateway `tar` streams `tree` (None: the exec fails)."""
    engine = tmp_path / "engine"
    engine.mkdir()
    for entry in REPO.iterdir():
        if entry.name not in ("patches", "engine-manifest.yaml", ".git"):
            (engine / entry.name).symlink_to(entry)
    synthetic = _engine(tmp_path / "synthetic")
    (engine / "patches").symlink_to(synthetic / "patches")
    (engine / "engine-manifest.yaml").symlink_to(synthetic / "engine-manifest.yaml")
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    for path, body in (tree or {}).items():
        (hermes / path).parent.mkdir(parents=True, exist_ok=True)
        (hermes / path).write_text(body, encoding="utf-8")
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tar_arm = ("" if tree is None else
               f'  *"tar -C /opt/hermes -cf -"*) shift 9; cd "{hermes}" && exec tar -cf - "$@" '
               "2>/dev/null ;;\n")
    (bindir / "docker").write_text("#!/bin/sh\ncase \"$*\" in\n" + tar_arm + "esac\nexit 0\n",
                                   encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(["bash", str(engine / "scripts" / "post_deploy_verify.sh")],
                          cwd=deployment, capture_output=True, text=True, timeout=300, env=env)
    return proc.stdout + proc.stderr


@needs_bash
def test_the_verifier_fails_a_gateway_without_the_patch_series(tmp_path):
    out = _verify_with_gateway_tree(tmp_path, {})
    assert "\x1b[31mFAIL\x1b[0m  gateway is missing carried Hermes patches: 01-guard.patch " \
           "02-scope.patch" in out, out[-4000:]


@needs_bash
def test_the_verifier_passes_a_gateway_with_the_patch_series(tmp_path):
    out = _verify_with_gateway_tree(tmp_path, PATCHED_TREE)
    assert "\x1b[32mPASS\x1b[0m  hermes patches: 2 of 2 carried patch(es) present in gateway" \
        in out, out[-4000:]


@needs_bash
def test_the_verifier_warns_when_the_gateway_cannot_be_read(tmp_path):
    out = _verify_with_gateway_tree(tmp_path, None)
    assert "\x1b[33mWARN\x1b[0m  Hermes patch presence undetectable" in out, out[-4000:]


def test_a_hunk_that_deletes_a_file_expects_nothing_of_it(tmp_path):
    deletion = ("diff --git a/tools/old.py b/tools/old.py\ndeleted file mode 100644\n"
                "--- a/tools/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-def gone_entirely(): pass\n")
    engine = _engine(tmp_path, series={"01-delete.patch": deletion})
    assert mod.expectations(mod.series_dir(engine, "v2026.9.14")) == []


def test_directories_in_the_tar_stream_are_not_files():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("tools")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
    assert mod.read_tar(buffer.getvalue()) == {}


# --- mutation-hardening of the expectation model and the report ----------------------------------

def _series(tmp_path, patches: dict[str, str]):
    engine = _engine(tmp_path, series=patches)
    return mod.expectations(mod.series_dir(engine, "v2026.9.14"))


def test_a_line_a_hunk_moves_is_still_expected(tmp_path):
    """A step's OWN removals never discount its additions, only LATER steps' removals do."""
    moved = ("--- a/tools/a.py\n+++ b/tools/a.py\n@@ -1,2 +1,2 @@\n-moved_line_value = 1\n"
             " keep\n+moved_line_value = 1\n")
    assert _series(tmp_path, {"01-move.patch": moved}) == [
        ("01-move.patch", "tools/a.py", "moved_line_value = 1")]


def test_a_later_removal_in_another_file_does_not_discount_this_one(tmp_path):
    first = "--- a/tools/a.py\n+++ b/tools/a.py\n@@ -0,0 +1 @@\n+shared_text_line = 1\n"
    later = "--- a/tools/b.py\n+++ b/tools/b.py\n@@ -1 +0,0 @@\n-shared_text_line = 1\n"
    assert _series(tmp_path, {"01-a.patch": first, "02-b.patch": later}) == [
        ("01-a.patch", "tools/a.py", "shared_text_line = 1")]


def test_the_minimum_line_length_is_eight_characters(tmp_path):
    body = "--- a/t.py\n+++ b/t.py\n@@ -0,0 +1,2 @@\n+x = 1234\n+x = 123\n"
    assert [text for _n, _p, text in _series(tmp_path, {"01.patch": body})] == ["x = 1234"]


def test_a_header_with_a_timestamp_names_the_file(tmp_path):
    body = "--- a/t.py\t2026-10-12\n+++ b/tools/t.py\t2026-10-12 00:00:00\n@@ -0,0 +1 @@\n+value = 'ok'\n"
    assert _series(tmp_path, {"01.patch": body}) == [("01.patch", "tools/t.py", "value = 'ok'")]


def test_the_report_counts_and_quotes_exactly(tmp_path, capsys):
    long_line = "very_long_identifier_" * 6
    series = {
        "01-a.patch": f"--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+{long_line}\n+second_line_a = 1\n",
        "02-b.patch": "--- a/b.py\n+++ b/b.py\n@@ -0,0 +1 @@\n+only_line_b = 1\n",
        "03-c.patch": "--- a/c.py\n+++ b/c.py\n@@ -0,0 +1 @@\n+only_line_c = 1\n",
    }
    tree = {"a.py": "second_line_a = 1\n", "b.py": "only_line_b = 1\n", "c.py": "only_line_c = 1\n"}
    engine = _engine(tmp_path, series=series)
    assert mod.main(["--engine-dir", str(engine)], _gateway(tree)) == 1
    out = capsys.readouterr().out
    assert "2 of 3 carried patch(es) present" in out
    assert f"MISSING 01-a.patch: 1 of 2 added line(s) absent, e.g. a.py: {long_line[:80]}\n" in out


def test_the_engine_dir_is_required():
    with pytest.raises(SystemExit):
        mod.main([], _gateway({}))


def test_the_cli_runs_as_a_script(tmp_path):
    """As post_deploy_verify runs it: a subprocess, no test PYTHONPATH (here: no series, exit 2)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, str(SCRIPT), "--engine-dir", str(tmp_path)],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2 and "UNDETECTABLE" in proc.stderr


def test_only_steps_after_this_one_discount_it_at_any_position(tmp_path):
    """Step 1 moves a line and step 2, the very next one, removes a line step 1 added; step 0 is
    unrelated. Exercises the 'later steps only' slice away from index 0."""
    step0 = "--- a/tools/z.py\n+++ b/tools/z.py\n@@ -0,0 +1 @@\n+unrelated_line = 0\n"
    step1 = ("--- a/tools/a.py\n+++ b/tools/a.py\n@@ -1,2 +1,3 @@\n-moved_line_value = 1\n keep\n"
             "+moved_line_value = 1\n+rewritten_later = 1\n")
    step2 = "--- a/tools/a.py\n+++ b/tools/a.py\n@@ -3 +3 @@\n-rewritten_later = 1\n+rewritten_now = 2\n"
    assert _series(tmp_path, {"01.patch": step0, "02.patch": step1, "03.patch": step2}) == [
        ("01.patch", "tools/z.py", "unrelated_line = 0"),
        ("02.patch", "tools/a.py", "moved_line_value = 1"),
        ("03.patch", "tools/a.py", "rewritten_now = 2")]


def test_a_later_removal_in_a_file_that_sorts_first_does_not_discount_this_one(tmp_path):
    first = "--- a/tools/b.py\n+++ b/tools/b.py\n@@ -0,0 +1 @@\n+shared_text_line = 1\n"
    later = "--- a/tools/a.py\n+++ b/tools/a.py\n@@ -1 +0,0 @@\n-shared_text_line = 1\n"
    assert _series(tmp_path, {"01-b.patch": first, "02-a.patch": later}) == [
        ("01-b.patch", "tools/b.py", "shared_text_line = 1")]


def test_the_report_quotes_the_first_missing_line(tmp_path, capsys):
    series = {"01.patch": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+first_missing = 1\n"
                          "+second_missing = 2\n"}
    assert mod.main(["--engine-dir", str(_engine(tmp_path, series=series))], _gateway({})) == 1
    assert "e.g. a.py: first_missing = 1\n" in capsys.readouterr().out
