"""tested_digests.py: a running engine image CI never tested FAILs post-deploy verify (okengine#1048).

The acceptance set, written before the verifier:

* hostile: a running engine container whose image is not in the release's tested-digest list (the
  v0.15.13 gateway, sha256:b9dae488, built on the host and never run in CI) FAILs;
* hostile: a supplied list that cannot be read or is malformed FAILs, rather than vouching for
  nothing;
* undetectable, never a pass: no list supplied, docker that cannot be asked, or a project with no
  engine container at all;
* legitimate: an image pulled by digest from the release registry passes by its registry digest,
  and by its config digest (image ID) under the classic store or the manifest digest under the
  containerd store;
* legitimate: third-party services (postgres) are skipped by name, never silently.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "tested_digests.py"
VERIFY = REPO / "scripts" / "post_deploy_verify.sh"


def _load():
    spec = importlib.util.spec_from_file_location("tested_digests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()

REGISTRY = "registry.invalid:5050/jlew/okengine/release"


def sha(char: str) -> str:
    return "sha256:" + char * 64


# The v0.15.13 gateway image ID as it ran on the fleet, built on the host and never run in CI.
HOST_BUILT_GATEWAY = "sha256:b9dae4884eacd782eb1a818d12d19b53b8767742fba9d00fa4ab92d04b194a32"


def release_list(**images) -> dict:
    """A tested-digest list in the shape ci/release_images.py writes."""
    return {"schema": 1, "project_id": "24", "pipeline_id": "13049", "commit_sha": "f" * 40,
            "images": {service: {"image": f"{REGISTRY}/{service}@{manifest}", "id": config}
                       for service, (manifest, config) in images.items()}}


DEFAULT_LIST = release_list(reader=(sha("1"), sha("a")), cockpit=(sha("2"), sha("b")))


def write_list(tmp_path: Path, data) -> Path:
    path = tmp_path / "tested-digests.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def fake_docker(rows=(("okengine-reader", sha("a"), "okengine-reader:okengine-v1-abc.p"),),
                repo_digests=None, ps=None, inspect=None):
    """A docker boundary answering exactly the three calls the verifier makes. `rows` are
    (service, image id, the reference the container was created from)."""
    repo_digests = repo_digests or {}
    calls = []

    def run(args):
        calls.append(args)
        if args[:3] == ["docker", "compose", "ps"]:
            return ps if ps is not None or rows is None else "".join(f"c{i}\n" for i in range(len(rows)))
        if args[:2] == ["docker", "inspect"]:
            if inspect is not None or rows is None:
                return inspect
            return "".join(f"{s} {i} {r}\n" for s, i, r in rows)
        if args[:3] == ["docker", "image", "inspect"]:
            value = repo_digests.get(args[-1], [])
            return None if value is None else json.dumps(value)
        raise AssertionError(f"unexpected docker call {args}")

    run.calls = calls
    return run


def verify(tmp_path, run, data=DEFAULT_LIST, environ=None, argv=None):
    path = write_list(tmp_path, data)
    return mod.main(argv if argv is not None else ["--list", str(path)], run=run,
                    environ=environ if environ is not None else {})


# --- hostile: an image CI never tested -----------------------------------------------------------

def test_a_running_engine_image_ci_never_tested_fails(tmp_path, capsys):
    run = fake_docker(rows=[("gateway", HOST_BUILT_GATEWAY, "hermes-agent:okengine-v0.15.13-faaf5f08"),
                            ("okengine-reader", sha("a"), "okengine-reader:okengine-v1-abc.p")])
    assert verify(tmp_path, run) == 1
    out = capsys.readouterr().out
    assert "UNTESTED gateway (hermes-agent:okengine-v0.15.13-faaf5f08" in out
    assert "TESTED okengine-reader" in out


def test_an_untested_image_fails_even_when_another_cannot_be_read(tmp_path, capsys):
    run = fake_docker(rows=[("okengine-mcp", sha("c"), "okengine-mcp:t"),
                            ("okengine-reader", sha("d"), "okengine-reader:t")],
                      repo_digests={sha("c"): None, sha("d"): []})
    assert verify(tmp_path, run) == 1
    out = capsys.readouterr().out
    assert "UNREADABLE okengine-mcp" in out and "UNTESTED okengine-reader" in out
    assert f"UNTESTED okengine-reader (okengine-reader:t = {sha('d')[:19]})" in out


def test_a_digest_from_another_repository_name_still_has_to_be_in_the_list(tmp_path, capsys):
    """A pulled image whose registry digest is not one CI pushed is untested, whatever its name."""
    run = fake_docker(rows=[("okengine-reader", sha("e"), f"{REGISTRY}/reader@{sha('9')}")],
                      repo_digests={sha("e"): [f"{REGISTRY}/reader@{sha('9')}"]})
    assert verify(tmp_path, run) == 1


@pytest.mark.parametrize("data", [
    "{", "[]", {"schema": 2, "images": DEFAULT_LIST["images"]},
    {"schema": 0, "images": DEFAULT_LIST["images"]}, {"schema": 1},
    {"schema": 1, "images": {}}, {"schema": 1, "images": []},
    {"schema": 1, "images": {"reader": "x"}},
    {"schema": 1, "images": {"reader": {"image": f"{REGISTRY}/reader@{sha('1')}"}}},
    {"schema": 1, "images": {"reader": {"image": f"{REGISTRY}/reader:tag", "id": sha("a")}}},
    {"schema": 1, "images": {"reader": {"image": f"@{sha('1')}", "id": sha("a")}}},
    {"schema": 1, "images": {"reader": {"image": f"{REGISTRY}/reader@{sha('1')}", "id": "sha256:A"}}},
    {"schema": 1, "images": {"reader": {"image": f"{REGISTRY}/reader@{sha('1')}x", "id": sha("a")}}},
    {"schema": 1, "images": {"reader": {"image": 7, "id": sha("a")}}},
], ids=["truncated", "not-an-object", "schema-2", "schema-0", "no-images", "empty-images", "images-list",
        "entry-not-object", "no-id", "tag-not-digest", "no-repository", "bad-id", "long-digest",
        "image-not-string"])
def test_a_supplied_list_that_is_malformed_fails_and_asks_docker_nothing(tmp_path, capsys, data):
    run = fake_docker()
    assert verify(tmp_path, run, data=data) == 1
    assert "FAIL" in capsys.readouterr().err
    assert run.calls == []


def test_a_supplied_list_that_does_not_exist_fails(tmp_path, capsys):
    run = fake_docker()
    assert mod.main(["--list", str(tmp_path / "absent.json")], run=run, environ={}) == 1
    assert "cannot read" in capsys.readouterr().err


# --- undetectable is not a pass -------------------------------------------------------------------

@pytest.mark.parametrize("environ", [{}, {"OKENGINE_TESTED_DIGESTS": ""}])
def test_no_list_supplied_is_undetectable_not_a_pass(tmp_path, capsys, environ):
    run = fake_docker()
    assert mod.main([], run=run, environ=environ) == 2
    assert "UNDETECTABLE" in capsys.readouterr().err
    assert run.calls == []


@pytest.mark.parametrize("kwargs", [
    {"rows": None, "ps": None},
    {"rows": None, "ps": "c1\n", "inspect": None},
    {"rows": None, "ps": "c1\n", "inspect": "okengine-reader sha256:aa\n"},
    {"rows": None, "ps": "c1\n", "inspect": "okengine-reader sha256:aa ref extra\n"},
], ids=["ps-fails", "inspect-fails", "unparseable-inspect", "four-field-inspect"])
def test_docker_that_cannot_be_asked_is_undetectable(tmp_path, capsys, kwargs):
    assert verify(tmp_path, fake_docker(**kwargs)) == 2
    assert "UNDETECTABLE" in capsys.readouterr().err


@pytest.mark.parametrize("rows", [[], [("postgres", sha("p"), "postgres:16-alpine")]],
                         ids=["nothing-running", "only-third-party"])
def test_a_project_with_no_engine_container_is_undetectable(tmp_path, capsys, rows):
    assert verify(tmp_path, fake_docker(rows=rows)) == 2
    assert "no running engine container" in capsys.readouterr().err


def test_an_image_whose_digests_cannot_be_read_is_undetectable_not_tested(tmp_path, capsys):
    run = fake_docker(rows=[("okengine-mcp", sha("c"), "okengine-mcp:t")],
                      repo_digests={sha("c"): None})
    assert verify(tmp_path, run) == 2
    assert "UNREADABLE okengine-mcp" in capsys.readouterr().out


@pytest.mark.parametrize("answer, rc", [("not json", 2), ('{"a": 1}', 2), ("null", 1), ("[]", 1)],
                         ids=["not-json", "not-a-list", "nil-list-is-untested", "empty-is-untested"])
def test_repo_digests_answers(tmp_path, answer, rc):
    original = fake_docker(rows=[("okengine-mcp", sha("c"), "okengine-mcp:t")])

    def answering(args):
        return answer if args[:3] == ["docker", "image", "inspect"] else original(args)

    assert verify(tmp_path, answering) == rc


# --- legitimate: the images CI tested -------------------------------------------------------------

def test_an_image_matched_by_its_config_digest_passes(tmp_path, capsys):
    """The classic image store: a pulled image's ID is its config digest, which CI recorded."""
    run = fake_docker(rows=[("okengine-reader", sha("a"), "okengine-reader:t"),
                            ("okengine-cockpit", sha("b"), "okengine-cockpit:t")])
    assert verify(tmp_path, run) == 0
    out = capsys.readouterr().out
    assert "2 of 2 running engine container(s) run an image CI tested" in out
    assert f"(pipeline 13049, commit {'f' * 12})" in out
    # Matched by ID: no RepoDigests lookup needed.
    assert not [c for c in run.calls if c[:3] == ["docker", "image", "inspect"]]


def test_an_image_matched_by_its_registry_digest_passes(tmp_path):
    run = fake_docker(rows=[("okengine-reader", sha("e"), f"{REGISTRY}/reader@{sha('1')}")],
                      repo_digests={sha("e"): [f"{REGISTRY}/reader@{sha('1')}", "junk", 3]})
    assert verify(tmp_path, run) == 0


def test_the_containerd_store_id_is_the_manifest_digest(tmp_path):
    run = fake_docker(rows=[("okengine-reader", sha("1"), "okengine-reader:t")])
    assert verify(tmp_path, run) == 0


def test_third_party_services_are_skipped_by_name_not_silently(tmp_path, capsys):
    """A skipped service sorted first must not end the judging of the services after it."""
    run = fake_docker(rows=[("a-postgres", sha("p"), "postgres:16-alpine"),
                            ("okengine-reader", sha("a"), "okengine-reader:t"),
                            ("okengine-zz", sha("9"), "okengine-mcp:t")])
    assert verify(tmp_path, run) == 1
    out = capsys.readouterr().out
    assert "SKIPPED a-postgres (postgres:16-alpine): not an engine image" in out
    assert "1 of 2 running engine container(s)" in out


def test_every_verdict_counts_toward_the_engine_total(tmp_path, capsys):
    run = fake_docker(rows=[("okengine-cockpit", sha("b"), "okengine-cockpit:t"),
                            ("okengine-mcp", sha("c"), "okengine-mcp:t"),
                            ("okengine-reader", sha("d"), "okengine-reader:t")],
                      repo_digests={sha("c"): None, sha("d"): []})
    assert verify(tmp_path, run) == 1
    assert "1 of 3 running engine container(s)" in capsys.readouterr().out


@pytest.mark.parametrize("ref", ["aaa/other:t", "zzz/other:t"])
def test_a_gateway_setting_does_not_make_other_repositories_engine_images(tmp_path, capsys, ref):
    run = fake_docker(rows=[("svc", sha("x"), ref)])
    assert verify(tmp_path, run, environ={"OKENGINE_GATEWAY_IMAGE": "myrepo/okengine:custom"}) == 2
    assert f"SKIPPED svc ({ref})" in capsys.readouterr().out


@pytest.mark.parametrize("ref, environ", [
    ("hermes-agent:okengine-v1-abc", {}),
    ("localhost:5000/team/hermes-agent:x", {}),
    ("myrepo/okengine:custom", {"OKENGINE_GATEWAY_IMAGE": "myrepo/okengine:custom"}),
    (f"{REGISTRY}/gateway@{sha('7')}", {}),
], ids=["hermes-agent", "registry-hermes-agent", "custom-gateway-env", "listed-repository"])
def test_engine_images_are_recognized_by_repository(tmp_path, ref, environ):
    data = release_list(reader=(sha("1"), sha("a")), gateway=(sha("7"), sha("0")))
    run = fake_docker(rows=[("gateway", sha("x"), ref)])
    assert verify(tmp_path, run, data=data, environ=environ) == 1


def test_the_environment_supplies_the_list(tmp_path):
    path = write_list(tmp_path, DEFAULT_LIST)
    assert mod.main([], run=fake_docker(), environ={"OKENGINE_TESTED_DIGESTS": str(path)}) == 0


def test_the_default_docker_boundary_is_looked_up_at_call_time(tmp_path, monkeypatch):
    path = write_list(tmp_path, DEFAULT_LIST)
    monkeypatch.setattr(mod, "_run", fake_docker())
    monkeypatch.setenv("OKENGINE_TESTED_DIGESTS", str(path))
    assert mod.main([]) == 0


# --- real invocation: as post_deploy_verify.sh (deploy.sh step 6) runs it -------------------------

STUB = """#!/bin/sh
# docker stub: the three calls tested_digests.py makes; everything else fails (cannot be asked).
case "$1 $2" in
  "compose ps") [ "$3" = "-q" ] && { echo c1; echo c2; exit 0; }; exit 1 ;;
  "inspect --format") echo "gateway $GATEWAY_ID hermes-agent:okengine-v0.15.13-faaf5f08"
                      echo "okengine-reader READER_ID okengine-reader:okengine-v0.15.13-faaf5f08.p"; exit 0 ;;
  "image inspect") echo '[]'; exit 0 ;;
esac
exit 1
""".replace("READER_ID", sha("a"))


def _deployment(tmp_path: Path, gateway_id: str) -> tuple[Path, dict]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "docker"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    # The host's own python3 (/usr/bin on a deploy host, /usr/local/bin in a python image), found
    # on PATH exactly as post_deploy_verify.sh finds it; no venv, no test PYTHONPATH.
    python_dir = os.path.dirname(shutil.which("python3"))
    env = {"PATH": f"{bindir}:{python_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "GATEWAY_ID": gateway_id}
    return pack, env


@pytest.mark.parametrize("gateway_id, rc", [(HOST_BUILT_GATEWAY, 1), (sha("0"), 0)],
                         ids=["host-built-untested", "ci-tested"])
def test_the_script_runs_as_post_deploy_verify_invokes_it(tmp_path, gateway_id, rc):
    """No test PYTHONPATH, the deployment dir as cwd, the system python3 as the interpreter."""
    pack, env = _deployment(tmp_path, gateway_id)
    data = release_list(reader=(sha("1"), sha("a")), gateway=(sha("7"), sha("0")))
    listing = write_list(tmp_path, data)
    out = subprocess.run(["python3", str(SCRIPT), "--list", str(listing)], cwd=pack,
                         capture_output=True, text=True, timeout=60,
                         env={**env, "PYTHONPATH": f"{REPO}/src:{REPO}"})
    assert out.returncode == rc, out.stdout + out.stderr


def _verify_output(tmp_path: Path, gateway_id: str, listing: str | None) -> str:
    pack, env = _deployment(tmp_path, gateway_id)
    if listing is not None:
        env["OKENGINE_TESTED_DIGESTS"] = listing
    result = subprocess.run(["bash", str(VERIFY)], cwd=pack, capture_output=True, text=True,
                            timeout=300, env=env)
    return result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_post_deploy_verify_fails_an_untested_running_image(tmp_path):
    listing = write_list(tmp_path, release_list(reader=(sha("1"), sha("a"))))
    output = _verify_output(tmp_path, HOST_BUILT_GATEWAY, str(listing))
    line = next(l for l in output.splitlines() if "CI never tested" in l)
    assert "\x1b[31mFAIL\x1b[0m" in line and "UNTESTED gateway" in line, output[-3000:]


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_post_deploy_verify_passes_ci_tested_images(tmp_path):
    data = release_list(reader=(sha("1"), sha("a")), gateway=(sha("7"), sha("0")))
    output = _verify_output(tmp_path, sha("0"), str(write_list(tmp_path, data)))
    line = next(l for l in output.splitlines() if "run an image CI tested" in l)
    assert "\x1b[32mPASS\x1b[0m" in line, output[-3000:]


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_post_deploy_verify_warns_when_no_list_is_supplied(tmp_path):
    output = _verify_output(tmp_path, sha("0"), None)
    line = next(l for l in output.splitlines() if "tested-digest check UNDETECTABLE" in l)
    assert "\x1b[33mWARN\x1b[0m" in line, output[-3000:]
    assert "CI never tested" not in output and "run an image CI tested" not in output


def test_engine_repositories_agree_with_the_image_pinning_module():
    """Kept stdlib-only so the verifier runs on a bare deploy-host python3; the copy of the engine
    repository set must not drift from the one deploy.sh pins images with."""
    spec = importlib.util.spec_from_file_location("engine_images", REPO / "scripts/engine_images.py")
    engine_images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine_images)
    assert mod.ENGINE_REPOSITORIES == engine_images.ENGINE_REPOSITORIES
    for ref in ("a", "a:t", "a@sha256:x", "host:5000/a/b:t", "host:5000/a/b@sha256:x"):
        assert mod.repository(ref) == engine_images.repository(ref)


def test_run_returns_stdout_or_none():
    assert mod._run(["true"]) == ""
    assert mod._run(["false"]) is None
    assert mod._run(["/nonexistent/docker"]) is None
    assert mod._run(["sh", "-c", "echo partial; kill -9 $$"]) is None, "a killed docker is no answer"


def test_the_script_has_a_main_guard(tmp_path):
    out = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                         timeout=60, env={"PATH": os.environ["PATH"]})
    assert out.returncode == 0 and "--list" in out.stdout


# --- okengine#1048 phase 3: a deployment rolled with --release-digests must prove it ------------

@pytest.mark.parametrize("argv, environ", [
    (["--require"], {}),
    ([], {"OKENGINE_REQUIRE_TESTED_DIGESTS": "1"}),
    ([], {"OKENGINE_REQUIRE_TESTED_DIGESTS": "1", "OKENGINE_TESTED_DIGESTS": ""}),
], ids=["flag", "env", "env-empty-list"])
def test_a_missing_list_fails_when_one_is_required(tmp_path, capsys, argv, environ):
    run = fake_docker()
    assert mod.main(argv, run=run, environ=environ) == 1
    assert "FAIL" in capsys.readouterr().err and run.calls == []


@pytest.mark.parametrize("value", ["0", "", "yes"])
def test_only_require_equals_one_turns_a_missing_list_into_a_fail(tmp_path, value):
    assert mod.main([], run=fake_docker(), environ={"OKENGINE_REQUIRE_TESTED_DIGESTS": value}) == 2


def test_a_required_list_that_is_present_is_judged_normally(tmp_path):
    path = write_list(tmp_path, DEFAULT_LIST)
    assert mod.main([], run=fake_docker(), environ={"OKENGINE_REQUIRE_TESTED_DIGESTS": "1",
                                                    "OKENGINE_TESTED_DIGESTS": str(path)}) == 0


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_post_deploy_verify_fails_a_required_but_missing_list(tmp_path):
    pack, env = _deployment(tmp_path, sha("0"))
    env["OKENGINE_REQUIRE_TESTED_DIGESTS"] = "1"
    result = subprocess.run(["bash", str(VERIFY)], cwd=pack, capture_output=True, text=True,
                            timeout=300, env=env)
    output = result.stdout + result.stderr
    line = next(l for l in output.splitlines() if "CI never tested" in l)
    assert "\x1b[31mFAIL\x1b[0m" in line and "release-digests" in line, output[-3000:]
