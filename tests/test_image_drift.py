"""image_drift: prove each engine-built image was built from THIS source, by inspecting the image.

The fourth deploy surface. `okcti-review-write` -- write_server.py mounting the vault read-write --
ran for three weeks on an image 20 commits old, missing write-path ENFORCEMENT the gateway had
already gained. Nothing reported it, because nothing compared an image to its source. On its first
live run this detector also found `okengine-operation-runner` a commit behind, which nobody had
noticed either.

Most of the contract is about what must NOT read as clean: an unreadable file, a manifest that
loaded nothing, and a project where zero services matched are each a measurement that did not
happen, and each is the shape that turns a missing check into a green tick.
"""
import importlib.util
import subprocess
from unittest.mock import create_autospec
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "image_drift.py"
MANIFEST = REPO / "config" / "image-provenance.yaml"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="image_drift absent")


def _load():
    spec = importlib.util.spec_from_file_location("image_drift", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["image_drift"] = mod
    spec.loader.exec_module(mod)
    return mod


def _engine(tmp_path, **files):
    root = tmp_path / "engine"
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return root


MAN = {"svc-a": {"source": "a/app.py", "container": "/app/app.py"},
       "svc-b": {"source": "b/app.py", "container": "/app/app.py"}}


def test_matching_image_and_source_is_in_sync(tmp_path, monkeypatch):
    mod = _load()
    root = _engine(tmp_path, **{"a/app.py": "one", "b/app.py": "two"})
    digests = {"ca": mod.file_hash(root / "a/app.py"), "cb": mod.file_hash(root / "b/app.py")}
    monkeypatch.setattr(mod, "container_hash", lambda c, p: digests[c])
    result = mod.compare(MAN, {"svc-a": "ca", "svc-b": "cb"}, root)
    assert result == {"in_sync": ["svc-a", "svc-b"], "drifted": [], "unreadable": [],
                      "not_running": []}


def test_an_image_built_from_other_source_is_drifted(tmp_path, monkeypatch):
    """The review-write case: the file is readable on both sides and simply differs."""
    mod = _load()
    root = _engine(tmp_path, **{"a/app.py": "current", "b/app.py": "current"})
    monkeypatch.setattr(mod, "container_hash",
                        lambda c, p: mod.file_hash(root / "a/app.py") if c == "ca" else "f" * 64)
    result = mod.compare(MAN, {"svc-a": "ca", "svc-b": "cb"}, root)
    assert result["drifted"] == ["svc-b"] and result["in_sync"] == ["svc-a"]


def test_unreadable_is_reported_apart_from_drifted(tmp_path, monkeypatch):
    """Different faults, different fixes. Drift means the image was built from other source;
    unreadable means the check could not be performed at all. Folding the second into the first
    sends the operator to rebuild an image that may be perfectly current."""
    mod = _load()
    root = _engine(tmp_path, **{"a/app.py": "one"})          # b/app.py deliberately absent
    monkeypatch.setattr(mod, "container_hash", lambda c, p: mod.file_hash(root / "a/app.py"))
    result = mod.compare(MAN, {"svc-a": "ca", "svc-b": "cb"}, root)
    assert result["unreadable"] == ["svc-b"], "a missing SOURCE file must not read as drift"

    # ...and the same when the CONTAINER side cannot be read.
    monkeypatch.setattr(mod, "container_hash", lambda c, p: "")
    assert mod.compare(MAN, {"svc-a": "ca"}, root)["unreadable"] == ["svc-a"]


def test_a_service_that_is_not_running_is_skipped_not_passed(tmp_path, monkeypatch):
    mod = _load()
    root = _engine(tmp_path, **{"a/app.py": "one", "b/app.py": "two"})
    monkeypatch.setattr(mod, "container_hash", lambda c, p: mod.file_hash(root / "a/app.py"))
    result = mod.compare(MAN, {"svc-a": "ca"}, root)
    assert result["not_running"] == ["svc-b"] and result["in_sync"] == ["svc-a"]


def test_a_container_hash_that_is_not_a_digest_is_not_trusted(tmp_path, monkeypatch):
    """`docker exec` merges stderr into stdout on some paths, so "sha256sum: /x: No such file"
    must not be parsed as a digest -- it would compare unequal and report DRIFT on a healthy
    image, which is a false alarm that gets the check switched off."""
    mod = _load()
    monkeypatch.setattr(mod, "_run", lambda args: "sha256sum: /app/app.py: No such file")
    assert mod.container_hash("c", "/app/app.py") == ""
    monkeypatch.setattr(mod, "_run", lambda args: "z" * 64 + "  /app/app.py")
    assert mod.container_hash("c", "/app/app.py") == "", "64 chars is not enough; it must be hex"
    monkeypatch.setattr(mod, "_run", lambda args: "a" * 64 + "  /app/app.py")
    assert mod.container_hash("c", "/app/app.py") == "a" * 64


def test_containers_are_matched_by_compose_label_not_by_name(tmp_path, monkeypatch):
    """Container names are per-deployment (`okcti-cockpit` vs `market-intel-cockpit`). Matching on
    names would compare nothing on a differently-named pack and report a clean run."""
    mod = _load()
    seen = {}

    def fake(args):
        seen["args"] = args
        return "abc123 okengine-cockpit\ndef456 okengine-reader"

    monkeypatch.setattr(mod, "_run", fake)
    found = mod.running_containers("somepack")
    assert found == {"okengine-cockpit": "abc123", "okengine-reader": "def456"}
    assert "label=com.docker.compose.project=somepack" in seen["args"]


def test_an_empty_or_unreadable_manifest_is_undetectable(tmp_path, capsys):
    mod = _load()
    assert mod.load_manifest(tmp_path / "nope.yaml") == {}
    (tmp_path / "empty.yaml").write_text("services: {}\n", encoding="utf-8")
    assert mod.load_manifest(tmp_path / "empty.yaml") == {}
    rc = mod.main(["--project", "p", "--engine-dir", str(tmp_path),
                   "--manifest", str(tmp_path / "nope.yaml")])
    assert rc == 1
    assert "UNDETECTABLE" in capsys.readouterr().err


def test_a_manifest_entry_missing_a_path_is_dropped(tmp_path):
    mod = _load()
    (tmp_path / "m.yaml").write_text(
        "services:\n  ok: {source: a.py, container: /a.py}\n"
        "  half: {source: b.py}\n  bad: 'not a mapping'\n", encoding="utf-8")
    assert list(mod.load_manifest(tmp_path / "m.yaml")) == ["ok"]


def test_zero_comparable_services_fails_rather_than_reporting_a_clean_run(tmp_path, monkeypatch,
                                                                         capsys):
    """Pointed at the wrong compose project, nothing matches. "0 drifted" over an empty set is the
    exact shape this whole detector exists to stop."""
    mod = _load()
    root = _engine(tmp_path, **{"a/app.py": "one"})
    (tmp_path / "m.yaml").write_text(
        "services:\n  svc-a: {source: a/app.py, container: /app/app.py}\n", encoding="utf-8")
    monkeypatch.setattr(mod, "running_containers", lambda project: {})
    rc = mod.main(["--project", "wrong", "--engine-dir", str(root),
                   "--manifest", str(tmp_path / "m.yaml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "UNDETECTABLE" in err and "measured nothing" in err


def test_cli_reports_drift_and_names_the_service(tmp_path, monkeypatch, capsys):
    mod = _load()
    root = _engine(tmp_path, **{"a/app.py": "one"})
    (tmp_path / "m.yaml").write_text(
        "services:\n  svc-a: {source: a/app.py, container: /app/app.py}\n", encoding="utf-8")
    monkeypatch.setattr(mod, "running_containers", lambda project: {"svc-a": "cid"})
    monkeypatch.setattr(mod, "container_hash", lambda c, p: "b" * 64)
    rc = mod.main(["--project", "p", "--engine-dir", str(root),
                   "--manifest", str(tmp_path / "m.yaml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "DRIFTED    svc-a" in err
    assert "The deploy reported success" in err


def test_cli_exits_zero_when_everything_matches(tmp_path, monkeypatch, capsys):
    mod = _load()
    root = _engine(tmp_path, **{"a/app.py": "one"})
    (tmp_path / "m.yaml").write_text(
        "services:\n  svc-a: {source: a/app.py, container: /app/app.py}\n", encoding="utf-8")
    monkeypatch.setattr(mod, "running_containers", lambda project: {"svc-a": "cid"})
    monkeypatch.setattr(mod, "container_hash", lambda c, p: mod.file_hash(root / "a/app.py"))
    assert mod.main(["--project", "p", "--engine-dir", str(root),
                     "--manifest", str(tmp_path / "m.yaml")]) == 0
    assert "1 service(s) compared" in capsys.readouterr().out


def test_run_returns_none_when_docker_is_unavailable(monkeypatch):
    """No docker, or a hung docker, is UNKNOWN. Returning "" or {} silently would make every
    service look not-running and the run would report a clean skip."""
    mod = _load()
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no docker")))
    assert mod._run(["docker", "ps"]) is None
    assert mod.running_containers("p") == {}
    assert mod.container_hash("c", "/x") == ""


def test_run_returns_none_on_a_nonzero_exit(monkeypatch):
    mod = _load()

    class P:
        returncode = 1
        stdout = "nope"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: P())
    assert mod._run(["docker", "ps"]) is None


def test_the_shipped_manifest_points_at_files_that_exist():
    """A manifest naming a moved file degrades every service to `unreadable` -- the check keeps
    running and stops measuring. This is the guard against that going unnoticed."""
    mod = _load()
    manifest = mod.load_manifest(MANIFEST)
    assert manifest, "the shipped manifest must load"
    for service, spec in manifest.items():
        assert (REPO / spec["source"]).is_file(), f"{service}: {spec['source']} does not exist"
        assert spec["container"].startswith("/"), f"{service}: container path must be absolute"


def test_clean_wheel_package_tree_matches_source_with_declared_runtime_member_exclusion(tmp_path):
    """The provenance tree probe must pass for an image built from this exact checkout."""
    mod = _load()
    out = tmp_path / "wheel"
    subprocess.run(
        [sys.executable, str(REPO / "scripts/build_engine_wheel.py"), "--out", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    installed = tmp_path / "installed"
    with zipfile.ZipFile(next(out.glob("*.whl"))) as archive:
        archive.extractall(installed)
    probe = next(
        probe
        for probe in mod.load_manifest(MANIFEST)["okengine-review-write"]["probes"]
        if probe.get("source_dir") == "src/okengine"
    )
    assert mod.tree_hash(REPO / probe["source_dir"], probe["exclude"]) == mod.tree_hash(
        installed / "okengine", probe["exclude"]
    )


def test_post_deploy_verify_runs_the_detector():
    """A detector nobody calls is not a detector. post_deploy_verify.sh is deploy.sh step 6 — the
    live end-to-end pass — which is the only place that can see the running containers."""
    verify = (REPO / "scripts" / "post_deploy_verify.sh").read_text(encoding="utf-8")
    assert "image_drift.py" in verify, "post_deploy_verify.sh must run the image drift check"
    assert "--project" in verify and "--engine-dir" in verify
    # It must FAIL the verification, not warn: a stale write path that only warns is a stale write
    # path, and the whole finding was that nothing surfaced it.
    idx = verify.index("image_drift.py")
    assert "bad " in verify[idx:idx + 900], "drift must count as a FAIL, not a WARN"


def test_a_manifest_whose_services_key_is_not_a_mapping_loads_nothing(tmp_path):
    """`services: some-string` (a hand-edit slip) must load NOTHING, which main() then reports as
    UNDETECTABLE. Coercing it would invent services and compare paths nobody declared."""
    mod = _load()
    (tmp_path / "m.yaml").write_text("services: just-a-string\n", encoding="utf-8")
    assert mod.load_manifest(tmp_path / "m.yaml") == {}
    (tmp_path / "n.yaml").write_text("not-services: {}\n", encoding="utf-8")
    assert mod.load_manifest(tmp_path / "n.yaml") == {}


def test_a_malformed_docker_ps_line_is_skipped_not_guessed(tmp_path, monkeypatch):
    """A container with no compose-service label prints one field. Guessing a service name from it
    would map a real container to the wrong service and compare the wrong file."""
    mod = _load()
    monkeypatch.setattr(mod, "_run",
                        lambda args: "onlyid\n\nabc123 okengine-cockpit\nx y z extra")
    assert mod.running_containers("p") == {"okengine-cockpit": "abc123"}


def test_cli_reports_an_unreadable_service_distinctly_from_drift(tmp_path, monkeypatch, capsys):
    """Both fail the run, but they send the operator to different places, so the OUTPUT must
    separate them even though the exit code cannot."""
    mod = _load()
    root = _engine(tmp_path, **{"a/app.py": "one"})
    (tmp_path / "m.yaml").write_text(
        "services:\n  svc-a: {source: a/app.py, container: /app/app.py}\n", encoding="utf-8")
    monkeypatch.setattr(mod, "running_containers", lambda project: {"svc-a": "cid"})
    monkeypatch.setattr(mod, "container_hash", lambda c, p: "")      # unreadable inside the image
    rc = mod.main(["--project", "p", "--engine-dir", str(root),
                   "--manifest", str(tmp_path / "m.yaml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "UNDETECTABLE svc-a" in err
    assert "DRIFTED" not in err, "an unreadable file must not be reported as drift"


# --- okengine#596: the manifest must not fall behind the compose it describes --------------------
COMPOSE_SKELETON = REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml"


def _engine_built_services() -> set[str]:
    """Services a pack's compose builds from ENGINE_DIR — the set this detector must cover."""
    import re

    import yaml
    # The skeleton is a TEMPLATE: `{{PACK}}` placeholders are not valid YAML until `framework
    # init` substitutes them. Neutralise them rather than skipping the check on the one file that
    # defines what every new deployment builds.
    raw = re.sub(r"\{\{[^}]*\}\}", "placeholder", COMPOSE_SKELETON.read_text(encoding="utf-8"))
    compose = yaml.safe_load(raw) or {}

    def context_of(build) -> str:
        # Compose accepts BOTH forms and this skeleton uses both: a mapping with `context:` for
        # the four that build from the repo root, and the short string form for the reader and
        # cockpit, which build from their own directory. Handling only the mapping silently
        # skipped those two — the same shape of omission as the missing manifest entry itself.
        if isinstance(build, dict):
            return str(build.get("context", ""))
        return str(build or "")

    return {
        name for name, spec in (compose.get("services") or {}).items()
        if isinstance(spec, dict) and "ENGINE_DIR" in context_of(spec.get("build"))
    }


def test_every_engine_built_service_is_in_the_provenance_manifest():
    """The detector already existed, worked, and was wired into post_deploy_verify. It missed the
    read-MCP going six weeks stale for one reason: `okengine-mcp` was not in the manifest.

    A per-service list maintained by hand beside a compose file that grows is a list that falls
    behind, and the failure is silent — an unlisted service is not reported as unchecked, it is
    simply never mentioned. So pin the list to the compose that defines the work.
    """
    if not COMPOSE_SKELETON.is_file():
        pytest.skip("compose skeleton absent")
    mod = _load()
    manifest = mod.load_manifest(MANIFEST)
    missing = _engine_built_services() - set(manifest)
    assert not missing, (
        f"built from ENGINE_DIR but absent from config/image-provenance.yaml, so nothing ever "
        f"checks whether their images match this source: {sorted(missing)}"
    )


def test_the_manifest_describes_no_service_the_compose_does_not_build():
    """The other direction: an entry for a service nobody builds is a check that can never run,
    and it reports `not_running` forever rather than admitting it is dead."""
    if not COMPOSE_SKELETON.is_file():
        pytest.skip("compose skeleton absent")
    mod = _load()
    stray = set(mod.load_manifest(MANIFEST)) - _engine_built_services()
    assert not stray, f"manifest names services the skeleton does not build from ENGINE_DIR: {sorted(stray)}"


def test_every_manifest_source_file_exists_in_this_repo():
    """A source path that has moved makes the service `unreadable` forever — which image_drift
    correctly refuses to call clean, but only after a deploy has already failed verification."""
    mod = _load()
    for service, spec in sorted(mod.load_manifest(MANIFEST).items()):
        assert (REPO / spec["source"]).is_file(), (
            f"{service}: manifest points at {spec['source']}, which does not exist in this repo"
        )


def test_the_read_mcp_probe_is_a_file_its_dockerfile_actually_copies():
    """The probe must be a file the image COPIES from ENGINE_DIR; anything else is unreadable in
    the container and the service can never be compared."""
    mod = _load()
    spec = mod.load_manifest(MANIFEST).get("okengine-mcp")
    assert spec, "okengine-mcp must be covered — it is the service okengine#596 was filed for"
    dockerfile = (REPO / "okengine-mcp" / "Dockerfile").read_text(encoding="utf-8")
    copied = " ".join(l for l in dockerfile.splitlines() if l.startswith("COPY"))
    assert spec["source"] in copied, (
        f"okengine-mcp/Dockerfile does not COPY {spec['source']} — the probe would never be "
        f"present in the image"
    )


# --- okengine#596: one probe per image is a weak proxy for "built from this source" --------------

def test_a_service_may_name_extra_probes_and_all_are_compared(tmp_path):
    mod = _load()
    (tmp_path / "manifest.yaml").write_text(
        "services:\n"
        "  svc:\n"
        "    source: a.py\n"
        "    container: /app/a.py\n"
        "    also:\n"
        "      - {source: b.yaml, container: /app/b.yaml}\n"
        "      - {source_dir: extensions, container_dir: /engine/extensions}\n", encoding="utf-8")
    spec = mod.load_manifest(tmp_path / "manifest.yaml")["svc"]
    assert spec["source"] == "a.py" and spec["container"] == "/app/a.py", \
        "the primary pair must stay a plain source/container for existing readers"
    assert spec["probes"] == [
        {"source": "a.py", "container": "/app/a.py"},
        {"source": "b.yaml", "container": "/app/b.yaml"},
        {"source_dir": "extensions", "container_dir": "/engine/extensions"},
    ]


def test_a_malformed_extra_probe_is_dropped_not_half_used(tmp_path):
    mod = _load()
    (tmp_path / "m.yaml").write_text(
        "services:\n  svc:\n    source: a.py\n    container: /app/a.py\n"
        "    also:\n      - {source: b.yaml}\n"
        "      - {source_dir: extensions}\n      - not-a-mapping\n", encoding="utf-8")
    assert len(mod.load_manifest(tmp_path / "m.yaml")["svc"]["probes"]) == 1


def test_one_drifted_probe_drifts_the_service(monkeypatch):
    """The incident this closes: the stale read-MCP's server.py was byte-identical across six
    weeks while the base-schema baked beside it was not. Agreeing on one file is not evidence."""
    mod = _load()
    manifest = {"svc": {"source": "a", "container": "/a",
                        "probes": [{"source": "a", "container": "/a"},
                                   {"source": "b", "container": "/b"}]}}
    monkeypatch.setattr(mod, "file_hash", lambda p: "same" if p.name == "a" else "src")
    monkeypatch.setattr(mod, "container_hash", lambda c, p: "same" if p == "/a" else "img")
    result = mod.compare(manifest, {"svc": "cid"}, Path("/engine"))
    assert result["drifted"] == ["svc"] and result["in_sync"] == []


def test_an_unreadable_probe_outranks_an_agreeing_one(monkeypatch):
    """One probe agreeing does not make up for another that could not be read."""
    mod = _load()
    manifest = {"svc": {"source": "a", "container": "/a",
                        "probes": [{"source": "a", "container": "/a"},
                                   {"source": "b", "container": "/b"}]}}
    monkeypatch.setattr(mod, "file_hash", lambda p: "same" if p.name == "a" else "")
    monkeypatch.setattr(mod, "container_hash", lambda c, p: "same" if p == "/a" else "x")
    result = mod.compare(manifest, {"svc": "cid"}, Path("/engine"))
    assert result["unreadable"] == ["svc"] and result["in_sync"] == []


def test_every_probe_of_every_service_exists_in_this_repo():
    mod = _load()
    for service, spec in sorted(mod.load_manifest(MANIFEST).items()):
        for probe in spec["probes"]:
            if "source_dir" in probe:
                assert (REPO / probe["source_dir"]).is_dir(), (
                    f"{service}: tree probe {probe['source_dir']} does not exist in this repo"
                )
                assert probe["container_dir"].startswith("/"), \
                    f"{service}: container tree path must be absolute"
            else:
                assert (REPO / probe["source"]).is_file(), (
                    f"{service}: probe {probe['source']} does not exist in this repo"
                )
                assert probe["container"].startswith("/"), \
                    f"{service}: container path {probe['container']} must be absolute"


def test_the_read_mcp_probes_cover_more_than_its_entrypoint():
    """A single probe on `server.py` reported the six-week-stale image as clean, because that one
    file had not changed. The probes must include something that actually moves."""
    mod = _load()
    probes = {
        p["source"]
        for p in mod.load_manifest(MANIFEST)["okengine-mcp"]["probes"]
        if "source" in p
    }
    assert len(probes) > 1
    assert "config/base-schema.yaml" in probes, (
        "the schema the served search tier-filters on is the file that was stale"
    )


def test_every_shared_wheel_image_probes_the_installed_okengine_tree():
    """An unchanged service entrypoint cannot prove that its shared OKEngine wheel is current."""
    mod = _load()
    manifest = mod.load_manifest(MANIFEST)
    expected = {
        "source_dir": "src/okengine",
        "container_dir": "/usr/local/lib/python3.13/site-packages/okengine",
        "exclude": ["data/base-schema.yaml"],
    }
    for service in (
        "okengine-mcp",
        "okengine-cockpit",
        "okengine-reader",
        "okengine-review-write",
        "okengine-operation-runner",
        "okengine-projection",
    ):
        assert expected in manifest[service]["probes"], (
            f"{service}: the installed shared wheel must be checked as a tree"
        )


def test_stale_shared_wheel_drifts_even_when_service_entrypoint_agrees(tmp_path, monkeypatch):
    mod = _load()
    root = _engine(tmp_path, **{"app.py": "unchanged", "src/okengine/shared.py": "new"})
    stale_tree = tmp_path / "old-okengine"
    stale_tree.mkdir()
    (stale_tree / "shared.py").write_text("old")
    source_digest = mod.tree_hash(root / "src/okengine")
    stale_digest = mod.tree_hash(stale_tree)
    app_digest = mod.file_hash(root / "app.py")
    monkeypatch.setattr(mod, "file_hash", create_autospec(mod.file_hash, return_value=app_digest))
    monkeypatch.setattr(
        mod, "container_hash", create_autospec(mod.container_hash, return_value=app_digest)
    )
    monkeypatch.setattr(mod, "tree_hash", create_autospec(mod.tree_hash, return_value=source_digest))
    monkeypatch.setattr(
        mod,
        "container_tree_hash",
        create_autospec(mod.container_tree_hash, return_value=stale_digest),
    )
    manifest = {"svc": {
        "source": "app.py", "container": "/app/app.py",
        "probes": [
            {"source": "app.py", "container": "/app/app.py"},
            {"source_dir": "src/okengine", "container_dir": "/site/okengine"},
        ],
    }}
    result = mod.compare(manifest, {"svc": "cid"}, root)
    assert result["drifted"] == ["svc"] and not result["in_sync"], (
        "an unchanged entrypoint must not mask a stale shared wheel"
    )


def test_release_audit_probes_cover_baked_operation_engine_and_review_policy():
    mod = _load()
    manifest = mod.load_manifest(MANIFEST)
    operations = manifest["okengine-operation-runner"]["probes"]
    review = manifest["okengine-review-write"]["probes"]
    operation_files = {p.get("source") for p in operations}
    review_files = {p.get("source") for p in review}
    assert {"src/okengine/operations/run.py", "src/okengine/operations/framework.py",
            "tools/policy_plane.py", "engine-manifest.yaml"} <= operation_files, (
        "the operation runner entrypoint is not the governed executor baked into its wheel"
    )
    wheel_builder = (REPO / "scripts/build_engine_wheel.py").read_text()
    runtime_sources = {
        "okengine-mcp/output_contract_enforce.py", "okengine-mcp/converge.py",
        "scripts/cron/id_lib.py", "scripts/cron/schema_lib.py", "scripts/cron/id_index.py",
        "scripts/cron/okf_migrate.py", "config/base-schema.yaml",
    }
    assert {"config/policy/catalog.yaml", "tools/policy_plane.py", "tools/schema_validator.py",
            "src/okengine/write_services/convergence.py",
            "src/okengine/write_services/review.py", *runtime_sources} <= review_files, (
        "the review writer must detect stale policy, schema, and write-service code"
    )
    assert any(
        probe.get("source_dir") == "src/okengine"
        and probe.get("container_dir") == "/usr/local/lib/python3.13/site-packages/okengine"
        and probe.get("exclude") == ["data/base-schema.yaml"]
        for probe in review
    ), (
        "the whole package copied into the review wheel must be governed as a tree"
    )
    for source in runtime_sources:
        assert f'"{source}"' in wheel_builder, f"review probe {source} is not a wheel member"
    assert {"source_dir": "extensions", "container_dir": "/engine/extensions"} in operations, (
        "operation definitions are copied as a tree, not represented by app.py"
    )
    assert "COPY config/policy/catalog.yaml" in (
        REPO / "okengine-mcp/Dockerfile.review").read_text(), (
        "review policy probe must correspond to a baked file"
    )
    dockerfile = (REPO / "okengine-operations/Dockerfile").read_text()
    assert "COPY src/" in dockerfile and "COPY extensions/" in dockerfile, (
        "operation and extension probes must correspond to baked sources"
    )


def test_tree_hash_detects_changed_and_removed_definitions_but_ignores_runtime_caches(tmp_path):
    mod = _load()
    tree = tmp_path / "extensions"
    operation = tree / "sample/operation.yaml"
    operation.parent.mkdir(parents=True)
    operation.write_text("name: before\n")
    baseline = mod.tree_hash(tree)
    assert len(baseline) == 64
    cache = tree / "sample/__pycache__/operation.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"runtime cache")
    assert mod.tree_hash(tree) == baseline, "Python caches created after COPY are not source drift"
    operation.write_text("name: after\n")
    assert mod.tree_hash(tree) != baseline, "changed operation definitions must be visible"
    operation.unlink()
    assert mod.tree_hash(tree) != baseline, "removed operation definitions must be visible"
    assert mod.tree_hash(tmp_path / "missing") == "", "missing tree is undetectable, not clean"


def test_tree_hash_includes_symlink_targets_without_following_them(tmp_path):
    mod = _load()
    tree = tmp_path / "extensions"
    tree.mkdir()
    (tree / "alias").symlink_to("first.yaml")
    first = mod.tree_hash(tree)
    (tree / "alias").unlink()
    (tree / "alias").symlink_to("second.yaml")
    assert mod.tree_hash(tree) != first, "a redirected definition symlink changes the baked tree"


def test_stale_extension_tree_drifts_even_when_operation_entrypoint_agrees(tmp_path, monkeypatch):
    mod = _load()
    root = _engine(tmp_path, **{"app.py": "unchanged", "extensions/sample/operation.yaml": "new"})
    old_tree = tmp_path / "old-extensions"
    old_tree.mkdir()
    (old_tree / "sample").mkdir()
    (old_tree / "sample/operation.yaml").write_text("old")
    stale_tree_digest = mod.tree_hash(old_tree)
    app_digest = mod.file_hash(root / "app.py")
    original_run = mod._run

    def docker_boundary(args):
        if args[:2] == ["docker", "exec"]:
            return (stale_tree_digest if "-c" in args else f"{app_digest}  /app/app.py")
        return original_run(args)

    monkeypatch.setattr(mod, "_run", create_autospec(mod._run, side_effect=docker_boundary))
    manifest = {"okengine-operation-runner": {
        "source": "app.py", "container": "/app/app.py",
        "probes": [
            {"source": "app.py", "container": "/app/app.py"},
            {"source_dir": "extensions", "container_dir": "/engine/extensions"},
        ],
    }}
    result = mod.compare(manifest, {"okengine-operation-runner": "cid"}, root)
    assert result["drifted"] == ["okengine-operation-runner"] and not result["in_sync"], (
        "unchanged app.py must not mask a stale operation definition"
    )


def test_stale_review_policy_drifts_even_when_write_server_agrees(tmp_path, monkeypatch):
    mod = _load()
    root = _engine(tmp_path, **{"write_server.py": "unchanged", "catalog.yaml": "new-rule"})
    manifest = {"okengine-review-write": {
        "source": "write_server.py", "container": "/engine/write_server.py",
        "probes": [
            {"source": "write_server.py", "container": "/engine/write_server.py"},
            {"source": "catalog.yaml", "container": "/engine/catalog.yaml"},
        ],
    }}
    server_digest = mod.file_hash(root / "write_server.py")

    def docker_boundary(args):
        return f"{server_digest if args[-1] == '/engine/write_server.py' else 'f' * 64}  {args[-1]}"

    monkeypatch.setattr(mod, "_run", create_autospec(mod._run, side_effect=docker_boundary))
    result = mod.compare(manifest, {"okengine-review-write": "cid"}, root)
    assert result["drifted"] == ["okengine-review-write"] and not result["in_sync"], (
        "unchanged write_server.py must not mask stale baked policy rules"
    )
