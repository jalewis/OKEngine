"""`deploy.sh --release-digests`: deploy the images CI tested, loaded by digest (okengine#1048).

The acceptance set, written before the loader:

* hostile: a loaded image whose config ID is not the tested one FAILs, and nothing is tagged;
* hostile: a list for another commit, or a dirty checkout, FAILs before anything is fetched;
* hostile: a list without an image the deployment runs FAILs;
* hostile: registry credentials that are missing, incomplete, shared (group/world-readable) or kept
  inside the repo FAIL; the token never appears on any argv;
* hostile: a loader image that is not digest-pinned fails this test file;
* legitimate: every image is loaded, verified, then tagged with exactly the references the
  deployment pins; an image already present by config ID is not fetched again.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
REAL_POPEN = subprocess.Popen
SCRIPT = REPO / "scripts" / "release_load.py"
DEPLOY = REPO / "scripts" / "deploy.sh"


def _load_module():
    spec = importlib.util.spec_from_file_location("release_load", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()
REGISTRY = "registry.invalid:5050/jlew/okengine/release"
TOKEN = "glpat-NEVER-ON-ARGV"
#: What the deploy host's own resolver (its /etc/hosts) says the registry name is. The loader
#: container cannot resolve it (okengine#1058); FakeDocker refuses a loader run without the mapping.
REGISTRY_HOST = "registry.invalid"
REGISTRY_ADDRESS = "198.51.100.7"


def host_resolver(table):
    """getaddrinfo at the resolver boundary: names in `table` resolve, anything else does not."""
    def getaddrinfo(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (address, 0)) for address in table[host]]
    return getaddrinfo


@pytest.fixture(autouse=True)
def deploy_host_resolver(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", host_resolver({REGISTRY_HOST: [REGISTRY_ADDRESS]}))


def sha(char: str) -> str:
    return "sha256:" + char * 64


IDS = {"reader": sha("a"), "gateway": sha("b"), "okengine-mcp": sha("c")}


def make_list(commit: str, **overrides) -> dict:
    images = {name: {"image": f"{REGISTRY}/{name}@{sha(str(i + 1))}", "id": image_id}
              for i, (name, image_id) in enumerate(IDS.items())}
    images.update(overrides)
    return {"schema": 1, "project_id": "24", "pipeline_id": "777", "commit_sha": commit,
            "images": images, "missing": []}


# --- the loader image stays digest-pinned -------------------------------------------------------

_PINNED = re.compile(r"[a-z0-9.:/_-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}")


def digest_pinned(reference: str) -> bool:
    return bool(_PINNED.fullmatch(reference))


def test_the_loader_image_is_pinned_by_digest():
    assert digest_pinned(mod.LOADER_IMAGE), (
        f"LOADER_IMAGE must name an immutable digest, got {mod.LOADER_IMAGE!r}")


@pytest.mark.parametrize("reference", ["gcr.io/go-containerregistry/crane:debug",
                                       "gcr.io/go-containerregistry/crane",
                                       "gcr.io/go-containerregistry/crane@sha256:abc",
                                       "gcr.io/go-containerregistry/crane:debug@sha256:" + "A" * 64])
def test_the_pin_check_rejects_a_mutable_or_malformed_reference(reference):
    """Negative fixture: the pin test above catches every unpinned shape."""
    assert not digest_pinned(reference)


def test_the_loader_image_cannot_be_overridden():
    """No environment variable or argument may replace the pinned loader."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count("LOADER_IMAGE = ") == 1
    assert not re.search(r"environ[^\n]*LOADER|LOADER[^\n]*environ|add_argument\([^)]*loader", source)


# --- a scratch engine checkout, deployment and credential file ----------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


COMPOSE = """services:
  okengine-reader:
    build: {context: "${ENGINE_DIR}", dockerfile: okengine-reader/Dockerfile}
  okengine-mcp:
    build: {context: "${ENGINE_DIR}", dockerfile: okengine-mcp/Dockerfile}
  postgres:
    image: postgres:17-alpine
"""


@pytest.fixture
def world(tmp_path):
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "README").write_text("engine\n")
    _git(engine, "init", "-q")
    _git(engine, "add", "README")
    _git(engine, "commit", "-q", "-m", "release")
    head = _git(engine, "rev-parse", "HEAD")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text(COMPOSE)
    creds = tmp_path / "home" / ".config" / "okengine" / "registry.env"
    creds.parent.mkdir(parents=True)
    creds.write_text(f"# okengine release loader\nOKENGINE_REGISTRY_USER=deploy-token\n"
                     f"OKENGINE_REGISTRY_TOKEN={TOKEN}\n")
    creds.chmod(0o600)
    listing = tmp_path / "tested-digests.json"
    listing.write_text(json.dumps(make_list(head)))
    return {"engine": engine, "pack": pack, "creds": creds, "list": listing, "head": head,
            "tmp": tmp_path}


class FakeDocker:
    """The docker CLI at the process boundary: `run` (the loader), `load`, `image inspect`,
    `tag`, `rmi`. `present` is the local image store by config ID; `refs` maps each tag to the ID
    it names. Like the real daemon, `rmi` of an image's LAST tag deletes the image itself, and
    `tag` of an ID the store no longer holds fails ("No such image")."""

    def __init__(self, loaded_ids=None, present=(), loader_rc=0, load_rc=0, load_output=None,
                 preflight_rc=0, preflight_out=None, preflight_err="UNAUTHORIZED"):
        self.loaded_ids = loaded_ids or {}
        self.preflight_err = preflight_err
        self.preflight_rc = preflight_rc
        self.preflight_out = preflight_out
        self.present = set(present)
        self.refs: dict[str, str] = {}
        self.loader_rc = loader_rc
        self.load_rc = load_rc
        self.load_output = load_output
        self.calls: list[list[str]] = []
        self.last_ref = None

    @staticmethod
    def unresolved(args):
        """Like the loader container (okengine#1058): a registry NAME resolves only through a
        `--add-host <name>:<address>` mapping; an IP literal needs none."""
        host = mod.registry_host(args[-2])
        if re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]*:[0-9a-fA-F:]*", host):
            return None
        mapped = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--add-host"]
        if not any(m.startswith(host + ":") for m in mapped):
            return f'Get "https://{args[-2]}/v2/": dial tcp: lookup {host}: no such host'
        return None

    def popen(self, args, **kwargs):
        if args[0] != "docker":          # subprocess.run itself (git) builds on Popen
            return REAL_POPEN(args, **kwargs)
        self.calls.append(args)
        self.last_ref = args[-1]
        fake = self
        failure = self.unresolved(args)

        class Stream:
            def __init__(self, data):
                self.data = data

            def read(self):
                return self.data

            def close(self):
                pass

        class Proc:
            stdout = Stream(b"TARBALL")
            stderr = Stream(failure.encode() if failure else
                            (b"fetch failed" if fake.loader_rc else b""))

            def wait(self, timeout):
                return 1 if failure else fake.loader_rc
        return Proc()

    def run(self, args, **kwargs):
        self.calls.append(args)
        verb = args[1]

        def done(rc=0, out=""):
            return subprocess.CompletedProcess(args, rc, out, "load error" if rc else "")
        if verb == "run":                # the preflight: crane digest through the loader
            failure = self.unresolved(args)
            if failure:
                return subprocess.CompletedProcess(args, 1, "", failure)
            if self.preflight_rc:
                return subprocess.CompletedProcess(args, self.preflight_rc, "", self.preflight_err)
            out = self.preflight_out if self.preflight_out is not None else args[-1].split("@")[1]
            return done(0, out + "\n")
        if verb == "load":
            image_id = self.loaded_ids.get(self.last_ref, sha("0"))
            placeholder = f"{self.last_ref.split('@')[0]}:i-was-a-digest"
            self.present.add(image_id)
            self.refs[placeholder] = image_id
            if self.load_output is not None:
                return done(self.load_rc, self.load_output)
            return done(self.load_rc, f"Loaded image: {placeholder}\n")
        if args[1:3] == ["image", "inspect"]:
            ref = args[-1]
            if ref in self.refs:
                return done(0, self.refs[ref])
            return done(0, ref) if ref in self.present else done(1)
        if verb == "tag":
            source, target = args[2], args[3]
            image_id = self.refs.get(source, source)
            if image_id not in self.present:
                return done(1)           # Error response from daemon: No such image
            self.refs[target] = image_id
            return done(0)
        if verb == "rmi":
            image_id = self.refs.pop(args[-1], None)
            if image_id is None:
                return done(1)
            if image_id not in self.refs.values():
                self.present.discard(image_id)   # Untagged ... Deleted: sha256:...
            return done(0)
        raise AssertionError(args)

    def resolve(self, ref):
        """The ID `ref` names in the local store now, or None."""
        image_id = self.refs.get(ref)
        return image_id if image_id in self.present else None

    def tags(self):
        return [c[2:] for c in self.calls if c[1] == "tag"]


def _install(monkeypatch, fake):
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[0] == "git":
            return real_run(args, **kwargs)
        return fake.run(args, **kwargs)
    monkeypatch.setattr(mod.subprocess, "run", run)
    monkeypatch.setattr(mod.subprocess, "Popen", fake.popen)


def _ids_by_ref(world):
    data = json.loads(world["list"].read_text())
    return {entry["image"]: entry["id"] for entry in data["images"].values()}


def call(world, *, env=None, preflight=False, **overrides):
    args = [str(world["list"]), "--pack", str(world["pack"]), "--engine-dir", str(world["engine"]),
            "--gateway-image", "hermes-agent:okengine-v1-abc.tested",
            "--engine-tag", "okengine-v1-abc.pack"] + (["--preflight"] if preflight else [])
    environ = {"OKENGINE_REGISTRY_ENV_FILE": str(world["creds"]), "ENGINE_DIR": str(world["engine"])}
    environ.update(env or {})
    return mod.main(args, environ=environ)


# --- legitimate ---------------------------------------------------------------------------------

def test_every_image_is_loaded_verified_then_tagged_as_the_deployment_pins_it(world, monkeypatch, capsys):
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    assert call(world) == 0, capsys.readouterr().err
    assert sorted(fake.tags()) == sorted([
        [IDS["gateway"], "hermes-agent:okengine-v1-abc.tested"],
        [IDS["okengine-mcp"], "okengine-mcp:okengine-v1-abc.pack"],
        [IDS["reader"], "okengine-reader:okengine-v1-abc.pack"]])
    first_tag = next(i for i, c in enumerate(fake.calls) if c[1] == "tag")
    assert all(c[1] != "load" for c in fake.calls[first_tag:]), "every image verified before any tag"
    loaders = [c for c in fake.calls if c[1] == "run"]
    assert len(loaders) == 3
    for argv in loaders:
        assert argv[argv.index("--env-file") + 1] == str(world["creds"])
        assert mod.LOADER_IMAGE in argv and "/var/run/docker.sock" not in " ".join(argv)
        assert not any("-v" == a or a.startswith("--volume") for a in argv), "no socket, no mounts"
    assert all(TOKEN not in " ".join(c) for c in fake.calls), "the token is never on argv"
    assert [c[1:] for c in fake.calls if c[1] == "rmi"], "crane's placeholder name is untagged"
    for argv in loaders:
        assert argv[-3:-1] == ["loader", "registry.invalid:5050"], "crane logs in to the image's registry"
    out = capsys.readouterr().out
    assert f"  loaded hermes-agent:okengine-v1-abc.tested = {IDS['gateway'][:19]}" in out
    assert "release-digests: 3 image reference(s) pinned to CI-tested digests" in out


def test_every_deployed_tag_still_names_a_present_image_after_the_placeholder_is_removed(
        world, monkeypatch, capsys):
    """Fix A: crane's placeholder is a loaded image's ONLY tag, and `docker rmi` of an image's last
    tag deletes the image. Removing it before the real tags exist deleted every loaded image, so
    the tag step failed and nothing was deployed."""
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    assert call(world) == 0, capsys.readouterr().err
    for target, ci_service in [("hermes-agent:okengine-v1-abc.tested", "gateway"),
                               ("okengine-mcp:okengine-v1-abc.pack", "okengine-mcp"),
                               ("okengine-reader:okengine-v1-abc.pack", "reader")]:
        assert fake.resolve(target) == IDS[ci_service], f"{target} must name the tested image"
    assert set(IDS.values()) <= fake.present, "no loaded image was deleted"
    assert not [ref for ref in fake.refs if ref.endswith(":i-was-a-digest")], (
        "crane's placeholder names are removed once the real tags hold the images")
    first_rmi = next(i for i, c in enumerate(fake.calls) if c[1] == "rmi")
    assert all(c[1] != "tag" for c in fake.calls[first_rmi:]), "placeholders go only after every tag"


def test_a_failed_tag_leaves_the_loaded_images_in_place(world, monkeypatch, capsys):
    """No placeholder is removed when tagging fails: the loaded bytes stay for a retry."""
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    original = fake.run
    fake.run = lambda args, **kw: (subprocess.CompletedProcess(args, 1, "", "")
                                   if args[1] == "tag" else original(args, **kw))
    assert call(world) == 1
    assert "cannot tag" in capsys.readouterr().err
    assert not [c for c in fake.calls if c[1] == "rmi"]
    assert set(IDS.values()) <= fake.present


def test_an_image_already_present_by_config_id_is_not_fetched_again(world, monkeypatch):
    fake = FakeDocker(loaded_ids=_ids_by_ref(world), present=set(IDS.values()))
    _install(monkeypatch, fake)
    assert call(world) == 0
    assert not [c for c in fake.calls if c[1] in ("run", "load")]
    assert len(fake.tags()) == 3


def test_a_load_that_reports_an_image_id_directly_is_accepted(world, monkeypatch):
    ids = _ids_by_ref(world)
    fake = FakeDocker(loaded_ids=ids)
    _install(monkeypatch, fake)
    original = fake.run

    def run(args, **kwargs):
        if args[1] == "load":
            original(args, **kwargs)
            return subprocess.CompletedProcess(args, 0, f"Loaded image ID: {ids[fake.last_ref]}\n", "")
        return original(args, **kwargs)
    fake.run = run
    assert call(world) == 0


# --- hostile ------------------------------------------------------------------------------------

def test_a_loaded_image_whose_config_id_is_not_the_tested_one_fails_and_nothing_is_tagged(
        world, monkeypatch, capsys):
    ids = _ids_by_ref(world)
    gateway_ref = next(ref for ref in ids if "/gateway@" in ref)
    ids[gateway_ref] = sha("e")
    fake = FakeDocker(loaded_ids=ids)
    _install(monkeypatch, fake)
    assert call(world) == 1
    err = capsys.readouterr().err
    assert "release-digests: FAIL" in err and f"not the tested {IDS['gateway']}" in err
    assert fake.tags() == []


@pytest.mark.parametrize("commit", ["f" * 40, "0" * 40])
def test_a_list_for_another_commit_fails_before_anything_is_fetched(world, monkeypatch, capsys, commit):
    world["list"].write_text(json.dumps(make_list(commit)))
    fake = FakeDocker()
    _install(monkeypatch, fake)
    assert call(world) == 1
    assert "deploy from a checkout of the tested release tag" in capsys.readouterr().err
    assert fake.calls == []


def test_a_dirty_checkout_fails(world, monkeypatch, capsys):
    (world["engine"] / "README").write_text("edited\n")
    fake = FakeDocker()
    _install(monkeypatch, fake)
    assert call(world) == 1
    assert "uncommitted changes" in capsys.readouterr().err and fake.calls == []


def test_an_engine_dir_that_is_not_a_checkout_fails(world, monkeypatch, capsys):
    fake = FakeDocker()
    _install(monkeypatch, fake)
    shutil.rmtree(world["engine"] / ".git")
    assert call(world) == 1
    assert "cannot read the engine revision" in capsys.readouterr().err


def test_a_list_without_an_image_the_deployment_runs_fails(world, monkeypatch, capsys):
    data = make_list(world["head"])
    del data["images"]["okengine-mcp"]
    world["list"].write_text(json.dumps(data))
    fake = FakeDocker()
    _install(monkeypatch, fake)
    assert call(world) == 1
    assert "no CI-tested image for: okengine-mcp" in capsys.readouterr().err and fake.calls == []


@pytest.mark.parametrize("content", ["{", json.dumps({"schema": 1, "images": {}})])
def test_an_unusable_list_fails(world, monkeypatch, capsys, content):
    world["list"].write_text(content)
    _install(monkeypatch, FakeDocker())
    assert call(world) == 1
    assert "tested-digest list unusable" in capsys.readouterr().err


@pytest.mark.parametrize("fault, message", [
    ("missing", "registry credentials missing"),
    ("directory", "is not a regular file"),
    ("shared-640", "readable by others"),
    ("shared-604", "readable by others"),
    ("shared-601", "readable by others"),
    ("no-token", "no value for OKENGINE_REGISTRY_TOKEN"),
    ("empty-user", "no value for OKENGINE_REGISTRY_USER"),
    ("in-pack", "keep it host-local"),
    ("in-engine", "keep it host-local"),
])
def test_registry_credentials_that_are_missing_or_unsafe_fail(world, monkeypatch, capsys, fault, message):
    creds = world["creds"]
    if fault == "missing":
        creds.unlink()
    elif fault == "directory":
        creds.unlink()
        creds.mkdir()
    elif fault.startswith("shared"):
        creds.chmod(int(fault.split("-")[1], 8))
    elif fault == "no-token":
        creds.write_text("OKENGINE_REGISTRY_USER=deploy-token\n# OKENGINE_REGISTRY_TOKEN=x\n")
    elif fault == "empty-user":
        creds.write_text(f"OKENGINE_REGISTRY_USER=\nOKENGINE_REGISTRY_TOKEN={TOKEN}\n")
    else:
        inside = world["pack" if fault == "in-pack" else "engine"] / "registry.env"
        if fault == "in-engine":     # gitignored, as a stray secret in a checkout usually is
            (world["engine"] / ".git" / "info" / "exclude").write_text("registry.env\n")
        shutil.copy(creds, inside)
        inside.chmod(0o600)
        creds = inside
    fake = FakeDocker()
    _install(monkeypatch, fake)
    assert call(world, env={"OKENGINE_REGISTRY_ENV_FILE": str(creds)}) == 1
    err = capsys.readouterr().err
    assert message in err and TOKEN not in err
    assert fake.calls == []


def test_the_default_credential_file_is_under_the_home_config_dir(world, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(world["tmp"] / "home"))
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    environ = {"ENGINE_DIR": str(world["engine"])}
    args = [str(world["list"]), "--pack", str(world["pack"]), "--engine-dir", str(world["engine"]),
            "--gateway-image", "g:t", "--engine-tag", "t"]
    assert mod.main(args, environ=environ) == 0, capsys.readouterr().err


@pytest.mark.parametrize("fault, message", [
    ("loader", "loader could not fetch"), ("load", "docker load failed"),
    ("no-output", "reported no image"), ("unreadable", "cannot read the image"),
])
def test_a_fetch_or_load_failure_fails(world, monkeypatch, capsys, fault, message):
    fake = FakeDocker(loaded_ids=_ids_by_ref(world), loader_rc=1 if fault == "loader" else 0,
                      load_rc=1 if fault == "load" else 0,
                      load_output="nothing useful\n" if fault == "no-output" else
                      ("Loaded image: other:i-was-a-digest\n" if fault == "unreadable" else None))
    _install(monkeypatch, fake)
    if fault == "unreadable":
        original = fake.run

        def run(args, **kwargs):
            if args[1:3] == ["image", "inspect"] and args[-1] == "other:i-was-a-digest":
                return subprocess.CompletedProcess(args, 1, "", "")
            return original(args, **kwargs)
        fake.run = run
    assert call(world) == 1
    assert message in capsys.readouterr().err
    assert fake.tags() == []


def test_a_tag_that_cannot_be_written_fails(world, monkeypatch, capsys):
    fake = FakeDocker(loaded_ids=_ids_by_ref(world), present=set(IDS.values()))
    _install(monkeypatch, fake)
    original = fake.run
    fake.run = lambda args, **kw: (subprocess.CompletedProcess(args, 1, "", "")
                                   if args[1] == "tag" else original(args, **kw))
    assert call(world) == 1
    assert "cannot tag" in capsys.readouterr().err


# --- real invocation: as deploy.sh runs it ------------------------------------------------------

STUB = r"""#!/bin/sh
# docker stub for release_load.py: state lives in $STUB_STATE.
log() { printf '%s\n' "$*" >> "$STUB_STATE/calls"; }
log "$*"
case "$1" in
  run)  case "$*" in *"--add-host localhost:"*) ;; *) echo "dial tcp: lookup localhost: no such host" >&2; exit 1 ;; esac
        for last; do :; done; case "$*" in *"crane digest"*) echo "${last#*@}"; exit 0 ;; esac
        printf '%s' "$last" > "$STUB_STATE/last"; cat /dev/null; echo TARBALL; exit 0 ;;
  load) cat > /dev/null; ref=$(cat "$STUB_STATE/last")
        id=$(grep -F "$ref " "$STUB_STATE/ids" | cut -d' ' -f2); echo "$id" >> "$STUB_STATE/present"
        echo "Loaded image ID: $id"; exit 0 ;;
  image) ref=""; for last; do ref="$last"; done
         grep -qxF "$ref" "$STUB_STATE/present" 2>/dev/null && { echo "$ref"; exit 0; }; exit 1 ;;
  tag|rmi) exit 0 ;;
esac
exit 1
"""


def _stub_world(world, mismatch=False):
    """A real python3 process resolves names with the real resolver: the subprocess worlds use a
    registry named `localhost`, which every host resolves, and the stubs refuse a loader run that
    does not map it with --add-host (the loader container sees none of the host's /etc/hosts)."""
    state = world["tmp"] / "state"
    state.mkdir()
    world["list"].write_text(world["list"].read_text().replace("registry.invalid:5050", "localhost:5050"))
    ids = _ids_by_ref(world)
    if mismatch:
        ids = {ref: (sha("e") if "/reader@" in ref else image_id) for ref, image_id in ids.items()}
    (state / "ids").write_text("".join(f"{ref} {image_id}\n" for ref, image_id in ids.items()))
    bindir = world["tmp"] / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(STUB)
    (bindir / "docker").chmod(0o755)
    python_dir = os.path.dirname(shutil.which("python3"))
    env = {"PATH": f"{bindir}:{python_dir}:/usr/bin:/bin", "HOME": str(world["tmp"] / "home"),
           "STUB_STATE": str(state), "ENGINE_DIR": str(world["engine"])}
    return state, env


@pytest.mark.parametrize("mismatch, rc", [(False, 0), (True, 1)], ids=["tested", "digest-mismatch"])
def test_the_script_runs_as_deploy_sh_invokes_it(world, mismatch, rc):
    """System python3 from PATH, the pack as cwd, no test PYTHONPATH, a stub docker CLI."""
    state, env = _stub_world(world, mismatch)
    out = subprocess.run(
        ["python3", str(SCRIPT), str(world["list"]), "--pack", str(world["pack"]),
         "--engine-dir", str(world["engine"]), "--gateway-image", "hermes-agent:t.tested",
         "--engine-tag", "okengine-v1-abc.pack"],
        cwd=world["pack"], capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == rc, out.stdout + out.stderr
    calls = (state / "calls").read_text()
    assert TOKEN not in calls
    assert ("tag " in calls) is (rc == 0)


# A docker stub that, like the real daemon, names a crane tarball `<repo>:i-was-a-digest` on load,
# keeps a tag table, and deletes an image when `rmi` removes its last tag. State: $STUB_STATE/
# ids (`<digest ref> <id>`), refs (`<tag> <id>`), present (one ID per line), calls.
NAMED_STUB = r"""#!/bin/sh
s="$STUB_STATE"; printf '%s\n' "$*" >> "$s/calls"; touch "$s/refs" "$s/present"
resolve() { awk -v r="$1" '$1 == r {id = $2} END {if (id) print id}' "$s/refs"; }
case "$1" in
  run)  case "$*" in *"--add-host localhost:"*) ;; *) echo "dial tcp: lookup localhost: no such host" >&2; exit 1 ;; esac
        for last; do :; done; case "$*" in *"crane digest"*) echo "${last#*@}"; exit 0 ;; esac
        printf '%s' "$last" > "$s/last"; echo TARBALL; exit 0 ;;
  load) cat > /dev/null; ref=$(cat "$s/last"); id=$(awk -v r="$ref" '$1 == r {print $2}' "$s/ids")
        name="${ref%@*}:i-was-a-digest"; echo "$id" >> "$s/present"; echo "$name $id" >> "$s/refs"
        echo "Loaded image: $name"; exit 0 ;;
  image) for last; do :; done; id=$(resolve "$last"); [ -n "$id" ] || id="$last"
         grep -qxF "$id" "$s/present" && { echo "$id"; exit 0; }
         echo "Error: No such image: $last" >&2; exit 1 ;;
  tag)  id=$(resolve "$2"); [ -n "$id" ] || id="$2"
        grep -qxF "$id" "$s/present" || { echo "Error response from daemon: No such image: $2" >&2; exit 1; }
        awk -v r="$3" '$1 != r' "$s/refs" > "$s/refs.new"; mv "$s/refs.new" "$s/refs"
        echo "$3 $id" >> "$s/refs"; exit 0 ;;
  rmi)  id=$(resolve "$2"); [ -n "$id" ] || { echo "Error: No such image: $2" >&2; exit 1; }
        awk -v r="$2" '$1 != r' "$s/refs" > "$s/refs.new"; mv "$s/refs.new" "$s/refs"; echo "Untagged: $2"
        if ! awk -v i="$id" '$2 == i {f = 1} END {exit !f}' "$s/refs"; then
            grep -vxF "$id" "$s/present" > "$s/present.new"; mv "$s/present.new" "$s/present"
            echo "Deleted: $id"
        fi
        exit 0 ;;
esac
exit 1
"""


def test_a_named_load_as_deploy_sh_invokes_it_leaves_every_target_tag_on_the_tested_image(world):
    """Fix A end to end: `docker load` reports `Loaded image: <repo>:i-was-a-digest` (crane's
    placeholder, the image's only tag). The real script must leave each deployed tag naming the
    tested image, which must still be in the store."""
    state, env = _stub_world(world)
    (world["tmp"] / "bin" / "docker").write_text(NAMED_STUB)
    out = subprocess.run(
        ["python3", str(SCRIPT), str(world["list"]), "--pack", str(world["pack"]),
         "--engine-dir", str(world["engine"]), "--gateway-image", "hermes-agent:t.tested",
         "--engine-tag", "okengine-v1-abc.pack"],
        cwd=world["pack"], capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    refs = dict(line.split() for line in (state / "refs").read_text().splitlines())
    present = set((state / "present").read_text().split())
    assert refs == {"hermes-agent:t.tested": IDS["gateway"],
                    "okengine-mcp:okengine-v1-abc.pack": IDS["okengine-mcp"],
                    "okengine-reader:okengine-v1-abc.pack": IDS["reader"]}, refs
    assert set(IDS.values()) <= present, "a loaded image was deleted"
    calls = (state / "calls").read_text()
    assert calls.count("\nrmi ") + calls.startswith("rmi ") == 3, "every placeholder is removed"


# --- deploy.sh end to end -----------------------------------------------------------------------

DEPLOY_STUBS = ("ensure-runtime.sh", "install-cron-plus.sh", "build-engine-image.sh",
                "deploy-cron-scripts.sh", "deploy-cron-plus-jobs.sh", "kickstart.sh")
GIT_STUB = """#!/bin/sh
dir=""; [ "$1" = "-C" ] && dir="$2"
for arg in "$@"; do
  case "$arg" in
    --show-toplevel) (cd "$dir" && pwd -P) ; exit 0 ;;
  esac
done
for arg in "$@"; do
  case "$arg" in
    rev-parse) echo "abc1234" ; exit 0 ;;
    status)    exit 0 ;;
  esac
done
exit 0
"""
DEPLOY_DOCKER_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_STATE/calls"
case "$1" in
  run)  [ -f "$STUB_STATE/pins-at-first-loader-run" ] || cp "$PWD/.env" "$STUB_STATE/pins-at-first-loader-run" 2>/dev/null
        [ ! -f "$STUB_STATE/unauthorized" ] || { echo "UNAUTHORIZED: authentication required" >&2; exit 1; }
        case "$*" in *"--add-host localhost:"*) ;; *) echo "dial tcp: lookup localhost: no such host" >&2; exit 1 ;; esac
        for last; do :; done; case "$*" in *"crane digest"*) echo "${last#*@}"; exit 0 ;; esac
        printf '%s' "$last" > "$STUB_STATE/last"; echo TARBALL; exit 0 ;;
  load) cat > /dev/null; ref=$(cat "$STUB_STATE/last")
        id=$(grep -F "$ref " "$STUB_STATE/ids" | cut -d' ' -f2); echo "$id" >> "$STUB_STATE/present"
        echo "Loaded image ID: $id"; exit 0 ;;
  image) ref=""; for last; do ref="$last"; done
         case "$*" in *org.okengine.git_sha*) echo abc1234; exit 0 ;; esac
         case "$ref" in sha256:*) grep -qxF "$ref" "$STUB_STATE/present" && { echo "$ref"; exit 0; }; exit 1 ;; esac
         exit 0 ;;
  compose) case "$2" in ps) echo cid ;; esac; exit 0 ;;
  inspect) echo "2026-08-17T00:00:00.000000000Z"; exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def deploy(world):
    engine = world["tmp"] / "deploy-engine"
    (engine / "scripts" / "cron").mkdir(parents=True)
    shutil.copy(DEPLOY, engine / "scripts" / "deploy.sh")
    for name in ("engine_images.py", "release_load.py", "tested_digests.py"):
        shutil.copy(REPO / "scripts" / name, engine / "scripts" / name)
    shutil.copy(REPO / "scripts" / "cron" / "dotenv_lib.py", engine / "scripts" / "cron" / "dotenv_lib.py")
    (engine / "scripts" / "lib").mkdir()
    shutil.copy(REPO / "scripts" / "lib" / "hermes_uid.sh", engine / "scripts" / "lib" / "hermes_uid.sh")
    (engine / "engine-manifest.yaml").write_text("engine_release: v9.8.7\n")
    for name in DEPLOY_STUBS:
        (engine / "scripts" / name).write_text("#!/bin/sh\nexit 0\n")
        (engine / "scripts" / name).chmod(0o755)
    verifier = engine / "scripts" / "post_deploy_verify.sh"
    verifier.write_text('#!/bin/sh\necho "verify tested=$OKENGINE_TESTED_DIGESTS require=$OKENGINE_REQUIRE_TESTED_DIGESTS"\n')
    verifier.chmod(0o755)
    data = json.loads(world["list"].read_text())
    data["commit_sha"] = "abc1234"
    world["list"].write_text(json.dumps(data))
    state, env = _stub_world(world)
    bindir = Path(env["PATH"].split(":")[0])
    (bindir / "docker").write_text(DEPLOY_DOCKER_STUB)
    (bindir / "git").write_text(GIT_STUB)
    (bindir / "git").chmod(0o755)
    python = bindir / "python-stub"
    python.write_text(f'#!/bin/sh\ncase "$1" in\n  */scripts/engine_images.py|*/scripts/release_load.py) '
                      f'exec "{sys.executable}" "$@" ;;\nesac\nexit 0\n')
    python.chmod(0o755)

    tmpdir = world["tmp"] / "deploy-tmp"
    tmpdir.mkdir()

    def run(*args, **extra):
        environment = {**env, "PYTHON": str(python), "HERMES_UID": "1003", "HERMES_GID": "1003",
                       "TMPDIR": str(tmpdir),
                       "OKENGINE_VERIFY_DELAY": "0",
                       "OKENGINE_REGISTRY_ENV_FILE": str(world["creds"]), **extra}
        environment.pop("ENGINE_DIR")
        return subprocess.run(["bash", str(engine / "scripts" / "deploy.sh"), *args],
                              cwd=world["pack"], capture_output=True, text=True, timeout=180,
                              env=environment)
    run.state = state
    run.pack = world["pack"]
    run.tmpdir = tmpdir
    return run


def test_deploy_with_release_digests_loads_never_builds_and_pins_the_list(deploy, world):
    result = deploy("--release-digests", str(world["list"]))
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (deploy.state / "calls").read_text()
    assert "compose up -d --no-build" in calls and "--build" not in calls.replace("--no-build", "")
    assert "tag sha256:" + "b" * 64 + " hermes-agent:okengine-v9.8.7-abc1234.pack.tested" in calls
    env_text = (deploy.pack / ".env").read_text()
    durable = deploy.pack / ".okengine" / "generated" / "tested-digests.json"
    assert f"OKENGINE_TESTED_DIGESTS={durable}" in env_text.splitlines()
    assert durable.read_bytes() == world["list"].read_bytes()
    assert "OKENGINE_GATEWAY_IMAGE=hermes-agent:okengine-v9.8.7-abc1234.pack.tested" in env_text
    assert f"verify tested={durable} require=1" in result.stdout
    assert TOKEN not in calls


def test_deploy_with_release_digests_equals_form_is_accepted(deploy, world):
    assert deploy(f"--release-digests={world['list']}").returncode == 0


PRIOR_ENV = ("# operator notes\nHERMES_UID=1003\nHERMES_GID=1003\n"
             "OKENGINE_GATEWAY_IMAGE=hermes-agent:okengine-v9.8.6-0ld0000\n"
             "OKENGINE_ENGINE_IMAGE_TAG=okengine-v9.8.6-0ld0000.pack\n"
             "COMPOSE_FILE=docker-compose.yml:docker-compose.okengine-image.yml\n")
PRIOR_OVERRIDE = "# the previous release's override\nservices:\n  gateway:\n    image: old\n"


def _prior_release(deploy, override=True):
    """A deployment already pinned to the previous release: its .env and override as they were."""
    env_file = deploy.pack / ".env"
    env_file.write_text(PRIOR_ENV)
    env_file.chmod(0o600)
    override_file = deploy.pack / "docker-compose.okengine-image.yml"
    if override:
        override_file.write_text(PRIOR_OVERRIDE)
    return env_file, override_file


def _assert_pins_unchanged(deploy, env_file, override_file, override=True):
    """Fix B: a failed --release-digests load leaves the pins exactly as they were, so a later bare
    `compose up` or gateway recreate does not run images that were never loaded."""
    assert env_file.read_bytes() == PRIOR_ENV.encode(), env_file.read_text()
    assert (env_file.stat().st_mode & 0o777) == 0o600
    if override:
        assert override_file.read_bytes() == PRIOR_OVERRIDE.encode(), override_file.read_text()
    else:
        assert not override_file.exists(), "a failed first load leaves no override behind"
    assert not list(deploy.tmpdir.iterdir()), "the pin snapshot is removed"


def test_deploy_with_a_digest_mismatch_fails_before_compose(deploy, world):
    env_file, override_file = _prior_release(deploy)
    ids = (deploy.state / "ids").read_text().replace(IDS["reader"], sha("e"))
    (deploy.state / "ids").write_text(ids)
    result = deploy("--release-digests", str(world["list"]))
    assert result.returncode == 1
    assert "nothing was deployed" in result.stderr
    assert "compose up" not in (deploy.state / "calls").read_text()
    _assert_pins_unchanged(deploy, env_file, override_file)


def test_deploy_without_registry_credentials_fails_under_release_digests(deploy, world):
    env_file, override_file = _prior_release(deploy)
    world["creds"].unlink()
    result = deploy("--release-digests", str(world["list"]))
    assert result.returncode == 1 and "registry credentials missing" in result.stderr
    assert "compose up" not in (deploy.state / "calls").read_text()
    _assert_pins_unchanged(deploy, env_file, override_file)


def test_a_failed_first_release_load_leaves_no_override(deploy, world):
    env_file, override_file = _prior_release(deploy, override=False)
    world["creds"].unlink()
    assert deploy("--release-digests", str(world["list"])).returncode == 1
    _assert_pins_unchanged(deploy, env_file, override_file, override=False)


def test_a_successful_release_load_keeps_the_new_pins(deploy, world):
    env_file, override_file = _prior_release(deploy)
    result = deploy("--release-digests", str(world["list"]))
    assert result.returncode == 0, result.stdout + result.stderr
    text = env_file.read_text()
    assert "OKENGINE_GATEWAY_IMAGE=hermes-agent:okengine-v9.8.7-abc1234.pack.tested" in text
    assert "OKENGINE_ENGINE_IMAGE_TAG=okengine-v9.8.6" not in text
    assert override_file.read_text() != PRIOR_OVERRIDE
    assert not list(deploy.tmpdir.iterdir()), "the pin snapshot is removed"


@pytest.mark.parametrize("args, rc, message", [
    (["--release-digests", "/nonexistent/tested-digests.json"], 1, "does not exist"),
    (["--release-digests"], 2, "needs a file"),
    (["--release-digests="], 2, "needs a file"),
    (["--release-digests", "LIST", "--rebuild"], 2, "cannot be combined with --rebuild"),
])
def test_deploy_refuses_an_unusable_release_digests_request(deploy, world, args, rc, message):
    result = deploy(*[str(world["list"]) if a == "LIST" else a for a in args])
    assert result.returncode == rc and message in result.stderr
    assert not (deploy.state / "calls").exists() or "compose up" not in (deploy.state / "calls").read_text()


def test_deploy_refuses_release_digests_with_a_preselected_gateway(deploy, world):
    result = deploy("--release-digests", str(world["list"]), OKENGINE_GATEWAY_IMAGE="x:y")
    assert result.returncode == 1 and "unset OKENGINE_GATEWAY_IMAGE" in result.stderr


def test_a_host_build_deploy_removes_the_stale_tested_list(deploy, world):
    assert deploy("--release-digests", str(world["list"])).returncode == 0
    result = deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OKENGINE_TESTED_DIGESTS" not in (deploy.pack / ".env").read_text()
    assert "verify tested= require=" in result.stdout


def test_a_load_that_names_a_real_tag_keeps_it(world, monkeypatch):
    """Only crane's placeholder name is removed; a tarball that carries a real tag keeps it."""
    ids = _ids_by_ref(world)
    fake = FakeDocker(loaded_ids=ids)
    _install(monkeypatch, fake)
    original = fake.run

    def run(args, **kwargs):
        if args[1] == "load":
            original(args, **kwargs)
            return subprocess.CompletedProcess(args, 0, "Loaded image: named/repo:v1\n", "")
        if args[1:3] == ["image", "inspect"] and args[-1] == "named/repo:v1":
            return subprocess.CompletedProcess(args, 0, ids[fake.last_ref], "")
        return original(args, **kwargs)
    fake.run = run
    assert call(world) == 0
    assert not [c for c in fake.calls if c[1] == "rmi"]


def test_an_engine_dir_inside_another_checkout_is_not_read_as_that_checkout(world, monkeypatch, capsys):
    """git -C walks up: a plain directory inside some other repository must not borrow its HEAD."""
    plain = world["engine"] / "not-a-checkout"
    plain.mkdir()
    (world["engine"] / ".git" / "info" / "exclude").write_text("not-a-checkout/\n")
    fake = FakeDocker()
    _install(monkeypatch, fake)
    args = [str(world["list"]), "--pack", str(world["pack"]), "--engine-dir", str(plain),
            "--gateway-image", "g:t", "--engine-tag", "t"]
    assert mod.main(args, environ={"OKENGINE_REGISTRY_ENV_FILE": str(world["creds"])}) == 1
    assert "not a git checkout root" in capsys.readouterr().err and fake.calls == []


@pytest.mark.parametrize("loaded", [sha("e"), sha("0")], ids=["higher", "lower"])
def test_any_other_config_id_fails_whichever_way_it_sorts(world, monkeypatch, capsys, loaded):
    ids = _ids_by_ref(world)
    ids[next(ref for ref in ids if "/reader@" in ref)] = loaded
    fake = FakeDocker(loaded_ids=ids)
    _install(monkeypatch, fake)
    assert call(world) == 1 and fake.tags() == []


@pytest.mark.parametrize("fault", ["loader", "load"])
def test_a_process_killed_by_a_signal_is_a_failure(world, monkeypatch, capsys, fault):
    """A negative return code (killed by a signal) is not success."""
    fake = FakeDocker(loaded_ids=_ids_by_ref(world), loader_rc=-9 if fault == "loader" else 0,
                      load_rc=-9 if fault == "load" else 0)
    _install(monkeypatch, fake)
    assert call(world) == 1 and fake.tags() == []


def test_a_tag_killed_by_a_signal_is_a_failure(world, monkeypatch, capsys):
    fake = FakeDocker(loaded_ids=_ids_by_ref(world), present=set(IDS.values()))
    _install(monkeypatch, fake)
    original = fake.run
    fake.run = lambda args, **kw: (subprocess.CompletedProcess(args, -9, "", "")
                                   if args[1] == "tag" else original(args, **kw))
    assert call(world) == 1


@pytest.mark.parametrize("fault", ["loader", "load"])
def test_failure_messages_keep_the_end_of_a_long_error(world, monkeypatch, capsys, fault):
    long_error = "HEAD-" + "x" * 600 + "-TAIL"
    fake = FakeDocker(loaded_ids=_ids_by_ref(world), loader_rc=1 if fault == "loader" else 0,
                      load_rc=1 if fault == "load" else 0)
    _install(monkeypatch, fake)
    if fault == "loader":
        original_popen = fake.popen

        def popen(args, **kwargs):
            proc = original_popen(args, **kwargs)
            proc.stderr.data = long_error.encode()
            return proc
        monkeypatch.setattr(mod.subprocess, "Popen", popen)
    else:
        original = fake.run

        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[1] == "load":
                return subprocess.CompletedProcess(args, 1, "", long_error)
            return result
        fake.run = run
    assert call(world) == 1
    err = capsys.readouterr().err
    assert "-TAIL" in err and "HEAD-" not in err and ("x" * 494) in err


@pytest.mark.parametrize("drop", ["--pack", "--engine-dir", "--gateway-image", "--engine-tag"])
def test_every_option_is_required(world, drop):
    args = [str(world["list"]), "--pack", "p", "--engine-dir", "e", "--gateway-image", "g",
            "--engine-tag", "t"]
    i = args.index(drop)
    del args[i:i + 2]
    with pytest.raises(SystemExit) as exc:
        mod.main(args, environ={})
    assert exc.value.code == 2


def test_the_script_accepts_images_already_present_as_deploy_sh_invokes_it(world):
    """Real docker output is text: an image found by `image inspect` must compare equal."""
    state, env = _stub_world(world)
    (state / "present").write_text("".join(f"{i}\n" for i in IDS.values()))
    out = subprocess.run(
        ["python3", str(SCRIPT), str(world["list"]), "--pack", str(world["pack"]),
         "--engine-dir", str(world["engine"]), "--gateway-image", "hermes-agent:t.tested",
         "--engine-tag", "okengine-v1-abc.pack"],
        cwd=world["pack"], capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "run " not in (state / "calls").read_text()


# --- okengine#1058: the registry name resolves on the deploy host, not in the loader -------------

def _loader_runs(fake):
    return [c for c in fake.calls if c[1] == "run"]


def test_the_loader_gets_the_registry_name_mapped_to_the_deploy_hosts_address(world, monkeypatch, capsys):
    """The v0.15.15 roll: the deploy host resolved the registry only through /etc/hosts, the loader
    container could not, and every fetch failed with `no such host`."""
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    assert call(world) == 0, capsys.readouterr().err
    for argv in _loader_runs(fake):
        i = argv.index("--add-host")
        assert argv[i + 1] == f"{REGISTRY_HOST}:{REGISTRY_ADDRESS}"
        assert argv.index(mod.LOADER_IMAGE) > i, "a docker run option, not a loader argument"
        assert not any(a == "-v" or a.startswith("--volume") or "docker.sock" in a for a in argv)


def test_a_loader_run_without_the_mapping_cannot_reach_a_named_registry(world):
    """Negative fixture: the fake loader refuses an unmapped registry name, as the real one did."""
    argv = ["docker", "run", "--rm", mod.LOADER_IMAGE, "-c", "x", "loader", "registry.invalid:5050",
            f"{REGISTRY}/reader@{sha('1')}"]
    assert "no such host" in FakeDocker.unresolved(argv)


@pytest.mark.parametrize("registry", ["203.0.113.7:5050", "[2001:db8::7]:5050", "203.0.113.7"])
def test_an_ip_literal_registry_needs_no_resolution_and_no_mapping(world, monkeypatch, capsys, registry):
    monkeypatch.setattr(socket, "getaddrinfo", host_resolver({}))
    world["list"].write_text(world["list"].read_text().replace("registry.invalid:5050", registry))
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    assert call(world) == 0, capsys.readouterr().err
    assert _loader_runs(fake) and all("--add-host" not in argv for argv in _loader_runs(fake))
    assert all(argv[-2] == registry for argv in _loader_runs(fake))


@pytest.mark.parametrize("preflight", [False, True], ids=["load", "preflight"])
def test_an_unresolvable_registry_name_is_a_clear_fail_not_a_traceback(world, monkeypatch, capsys, preflight):
    monkeypatch.setattr(socket, "getaddrinfo", host_resolver({}))
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    assert call(world, preflight=preflight) == 1
    err = capsys.readouterr().err
    assert "release-digests: FAIL — cannot resolve the registry host 'registry.invalid'" in err, err
    assert "Traceback" not in err and "/etc/hosts" in err
    assert not _loader_runs(fake) and fake.tags() == []


def test_a_resolver_error_other_than_a_miss_is_a_fail_too(world, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise UnicodeError("label too long")
    monkeypatch.setattr(socket, "getaddrinfo", broken)
    _install(monkeypatch, FakeDocker(loaded_ids=_ids_by_ref(world)))
    assert call(world) == 1
    assert "cannot resolve the registry host 'registry.invalid' on this deploy host (label too long)" \
        in capsys.readouterr().err


@pytest.mark.parametrize("addresses, mapped", [
    (["2001:db8::7", REGISTRY_ADDRESS], REGISTRY_ADDRESS),
    (["2001:db8::7"], "2001:db8::7"),
    (["198.51.100.8", REGISTRY_ADDRESS], "198.51.100.8"),
    (["::ffff:198.51.100.9", REGISTRY_ADDRESS], REGISTRY_ADDRESS),
], ids=["ipv4-preferred", "ipv6-only", "first-ipv4", "v4-mapped-v6-is-not-ipv4"])
def test_the_mapping_prefers_the_first_ipv4_address(monkeypatch, addresses, mapped):
    monkeypatch.setattr(socket, "getaddrinfo", host_resolver({REGISTRY_HOST: addresses}))
    assert mod.host_mapping("registry.invalid:5050") == ["--add-host", f"{REGISTRY_HOST}:{mapped}"]


@pytest.mark.parametrize("registry, host", [
    ("registry.invalid:5050", "registry.invalid"), ("registry.invalid", "registry.invalid"),
    ("[2001:db8::7]:5050", "2001:db8::7"), ("[2001:db8::7]", "2001:db8::7"),
    ("203.0.113.7:5050", "203.0.113.7"), ("2001:db8::7", "2001:db8::7"),
])
def test_registry_host_strips_the_port(registry, host):
    assert mod.registry_host(registry) == host


# --- the preflight: reach the registry before any pin is written ---------------------------------

def test_the_preflight_reaches_the_registry_and_loads_nothing(world, monkeypatch, capsys):
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    assert call(world, preflight=True) == 0, capsys.readouterr().err
    runs = _loader_runs(fake)
    assert len(runs) == 1, "one probe per registry"
    argv = runs[0]
    assert "crane digest --insecure" in argv[argv.index("-c") + 1]
    assert argv[argv.index("--add-host") + 1] == f"{REGISTRY_HOST}:{REGISTRY_ADDRESS}"
    assert argv[argv.index("--env-file") + 1] == str(world["creds"])
    assert argv[-2] == "registry.invalid:5050" and "@sha256:" in argv[-1]
    assert not [c for c in fake.calls if c[1] in ("load", "tag", "rmi")]
    assert TOKEN not in " ".join(argv)
    assert "preflight passed; registry reachable for 1 image(s) to fetch" in capsys.readouterr().out


def test_the_preflight_probes_each_registry_once(world, monkeypatch, capsys):
    data = json.loads(world["list"].read_text())
    entry = data["images"]["reader"]
    entry["image"] = entry["image"].replace("registry.invalid:5050", "203.0.113.7:5050")
    world["list"].write_text(json.dumps(data))
    fake = FakeDocker(loaded_ids=_ids_by_ref(world))
    _install(monkeypatch, fake)
    assert call(world, preflight=True) == 0, capsys.readouterr().err
    assert sorted(argv[-2] for argv in _loader_runs(fake)) == ["203.0.113.7:5050", "registry.invalid:5050"]


def test_the_preflight_needs_no_registry_when_every_image_is_local(world, monkeypatch, capsys):
    """A rollback onto images still in the local store must not depend on the registry."""
    monkeypatch.setattr(socket, "getaddrinfo", host_resolver({}))
    fake = FakeDocker(present=set(IDS.values()))
    _install(monkeypatch, fake)
    assert call(world, preflight=True) == 0, capsys.readouterr().err
    assert not _loader_runs(fake)
    assert "every tested image is already present locally" in capsys.readouterr().out


@pytest.mark.parametrize("fault, message", [
    ("unauthorized", "loader cannot reach the registry for registry.invalid:5050/"),
    ("killed", "loader cannot reach the registry for registry.invalid:5050/"),
    ("other-digest", "the registry reports " + sha("9")),
    ("lower-digest", "the registry reports " + sha("0")),
    ("pinned-then-other", "the registry reports " + sha("9")),
    ("no-digest", "the registry reports no digest"),
])
def test_a_preflight_that_cannot_reach_or_find_the_image_fails(world, monkeypatch, capsys, fault, message):
    pinned = json.loads(world["list"].read_text())["images"]["gateway"]["image"].split("@")[1]
    fake = FakeDocker(preflight_rc={"unauthorized": 1, "killed": -9}.get(fault, 0),
                      preflight_out={"other-digest": sha("9"), "lower-digest": sha("0"),
                                     "pinned-then-other": f"{pinned}\n{sha('9')}",
                                     "no-digest": "nothing"}.get(fault))
    _install(monkeypatch, fake)
    assert call(world, preflight=True) == 1
    err = capsys.readouterr().err
    assert message in err and "release-digests: FAIL" in err, err
    if fault == "unauthorized":
        assert "UNAUTHORIZED" in err


def test_the_preflight_judges_the_last_digest_crane_prints(world, monkeypatch, capsys):
    pinned = json.loads(world["list"].read_text())["images"]["gateway"]["image"].split("@")[1]
    _install(monkeypatch, FakeDocker(preflight_out=f"{sha('9')}\n{pinned}"))
    assert call(world, preflight=True) == 0, capsys.readouterr().err


def test_a_preflight_failure_keeps_the_end_of_a_long_error(world, monkeypatch, capsys):
    _install(monkeypatch, FakeDocker(preflight_rc=1, preflight_err="HEAD-" + "x" * 600 + "-TAIL"))
    assert call(world, preflight=True) == 1
    err = capsys.readouterr().err
    assert "-TAIL" in err and "HEAD-" not in err and ("x" * 494) in err


def test_a_preflight_loader_that_cannot_start_fails(world, monkeypatch, capsys):
    fake = FakeDocker()
    _install(monkeypatch, fake)
    installed = mod.subprocess.run

    def run(args, **kwargs):
        if args[:2] == ["docker", "run"]:
            raise OSError("docker: not found")
        return installed(args, **kwargs)
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert call(world, preflight=True) == 1
    assert "loader preflight could not run for registry.invalid:5050/" in capsys.readouterr().err


def test_the_preflight_runs_the_same_checks_as_the_load(world, monkeypatch, capsys):
    world["creds"].unlink()
    fake = FakeDocker()
    _install(monkeypatch, fake)
    assert call(world, preflight=True) == 1
    assert "registry credentials missing" in capsys.readouterr().err and fake.calls == []


# --- deploy.sh: the preflight runs before any pin is written --------------------------------------

def test_a_preflight_failure_writes_no_pin_and_composes_nothing(deploy, world):
    """okengine#1058: an unreachable registry (here: credentials refused) FAILs before .env or the
    override is written, not after, so no restore is needed and no window exists."""
    env_file, override_file = _prior_release(deploy)
    (deploy.state / "unauthorized").write_text("")
    result = deploy("--release-digests", str(world["list"]))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "preflight failed; no pin was written" in result.stderr, result.stderr
    calls = (deploy.state / "calls").read_text()
    assert "compose up" not in calls and "\nload" not in calls and "\ntag " not in calls
    assert (deploy.state / "pins-at-first-loader-run").read_bytes() == PRIOR_ENV.encode(), (
        "the first loader run happened before any pin was written")
    _assert_pins_unchanged(deploy, env_file, override_file)
    assert not (deploy.pack / ".okengine" / "generated" / "tested-digests.json").exists()


def test_an_unresolvable_registry_fails_deploy_before_any_pin(deploy, world):
    """A real python3 and the real resolver: the `.invalid` name never resolves."""
    env_file, override_file = _prior_release(deploy)
    world["list"].write_text(world["list"].read_text().replace("localhost:5050", "registry.invalid:5050"))
    result = deploy("--release-digests", str(world["list"]))
    assert result.returncode == 1
    assert "cannot resolve the registry host 'registry.invalid'" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    calls = (deploy.state / "calls").read_text()
    assert "compose up" not in calls and not [c for c in calls.splitlines() if c.startswith("run ")]
    _assert_pins_unchanged(deploy, env_file, override_file)


def test_a_failed_load_after_the_preflight_still_restores_the_pins_and_keeps_the_old_list(deploy, world):
    """The !1359 restore still covers a load that fails after a passing preflight, and the list the
    previous roll persisted is not replaced by the unverified one."""
    assert deploy("--release-digests", str(world["list"])).returncode == 0
    durable = deploy.pack / ".okengine" / "generated" / "tested-digests.json"
    before_env, before_list = (deploy.pack / ".env").read_bytes(), durable.read_bytes()
    before_override = (deploy.pack / "docker-compose.okengine-image.yml").read_bytes()
    ids = (deploy.state / "ids").read_text().replace(IDS["reader"], sha("e"))
    (deploy.state / "ids").write_text(ids)
    (deploy.state / "present").write_text("")
    data = json.loads(world["list"].read_text())
    data["pipeline_id"] = "778"
    world["list"].write_text(json.dumps(data))
    result = deploy("--release-digests", str(world["list"]))
    assert result.returncode == 1 and "nothing was deployed" in result.stderr
    assert (deploy.pack / ".env").read_bytes() == before_env
    assert (deploy.pack / "docker-compose.okengine-image.yml").read_bytes() == before_override
    assert durable.read_bytes() == before_list
    assert sorted(p.name for p in durable.parent.iterdir()) == ["tested-digests.json"], "no stage left"


# --- okengine#1057 [6]: the persisted list lives in the deployment -------------------------------

def _load_tested_digests():
    spec = importlib.util.spec_from_file_location("tested_digests_for_load", REPO / "scripts/tested_digests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env_values(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def test_post_deploy_verify_still_finds_the_list_after_the_operators_copy_is_deleted(deploy, world):
    """The operator fetched the list into a deploy clone or /tmp; pruning it must not turn every
    later verify into a FAIL on a deployment that runs exactly the tested images."""
    assert deploy("--release-digests", str(world["list"])).returncode == 0
    world["list"].unlink()
    env = _env_values(deploy.pack / ".env")
    persisted = Path(env["OKENGINE_TESTED_DIGESTS"])
    assert persisted.is_relative_to(deploy.pack), "the deployment holds its own copy"
    tested = _load_tested_digests()

    def docker(args):
        if args[1:3] == ["compose", "ps"]:
            return "c1\n"
        if args[1] == "inspect":
            return f"gateway {IDS['gateway']} {env['OKENGINE_GATEWAY_IMAGE']}\n"
        return None
    rc = tested.main([], run=docker, environ={**env, "OKENGINE_REQUIRE_TESTED_DIGESTS": "1"})
    assert rc == 0, "the persisted list must still prove the running gateway is the tested one"


# --- okengine#1057 [11]: the tested gateway tag is scoped per compose project ---------------------

def test_two_projects_rolled_from_two_lists_for_one_commit_keep_distinct_gateway_tags(deploy, world):
    """Two pipelines for the same commit record different gateway IDs. Rolling pack B from the
    second list must not move the gateway tag pack A runs."""
    assert deploy("--release-digests", str(world["list"])).returncode == 0
    gateway_a = _env_values(deploy.pack / ".env")["OKENGINE_GATEWAY_IMAGE"]
    pack_b = world["tmp"] / "pack-b"
    pack_b.mkdir()
    shutil.copy(deploy.pack / "docker-compose.yml", pack_b / "docker-compose.yml")
    data = json.loads(world["list"].read_text())
    other_gateway = {"image": data["images"]["gateway"]["image"].rsplit("@", 1)[0] + "@" + sha("7"),
                     "id": sha("d")}
    data["images"]["gateway"] = other_gateway
    data["pipeline_id"] = "778"
    list_b = world["tmp"] / "tested-digests-b.json"
    list_b.write_text(json.dumps(data))
    with (deploy.state / "ids").open("a") as ids:
        ids.write(f"{other_gateway['image']} {other_gateway['id']}\n")
    result = deploy(str(pack_b), "--release-digests", str(list_b))
    assert result.returncode == 0, result.stdout + result.stderr
    gateway_b = _env_values(pack_b / ".env")["OKENGINE_GATEWAY_IMAGE"]
    assert gateway_a == "hermes-agent:okengine-v9.8.7-abc1234.pack.tested"
    assert gateway_b == "hermes-agent:okengine-v9.8.7-abc1234.pack-b.tested"
    tags = [line.split()[1:] for line in (deploy.state / "calls").read_text().splitlines()
            if line.startswith("tag ") and "hermes-agent:" in line]
    assert [IDS["gateway"], gateway_a] in tags and [sha("d"), gateway_b] in tags
    assert all(target != gateway_a for image_id, target in tags if image_id == sha("d")), (
        "pack B's roll retagged the gateway pack A runs")


# --- okengine#1057 [5] [9] [10]: one set of engine images across every table ---------------------

# The release lane (ci/release_record.py) is publish-excluded; its two tables are checked only in the
# source tree, which always carries scripts/publish-snapshot.sh, so they can never be skipped there.
_RELEASE_LANE_TABLES = ("release_images.SERVICES", "release_record.PRODUCT")


def _public_snapshot_tree() -> bool:
    return not (REPO / "ci" / "release_record.py").is_file() and not (
        REPO / "scripts" / "publish-snapshot.sh").is_file()


def _tables():
    """Every hand-maintained enumeration of the engine product images, read from the real code."""
    sys.path.insert(0, str(REPO))
    from ci import candidate_wheel, release_images
    tables = {
        "engine_images.ENGINE_DOCKERFILES": dict(mod.engine_images.ENGINE_DOCKERFILES),
        "tested_digests.ENGINE_REPOSITORIES": set(mod.tested_digests.ENGINE_REPOSITORIES),
        "release_load.CI_SERVICE": dict(mod.CI_SERVICE),
        "candidate_wheel.DOCKERFILES": dict(candidate_wheel.DOCKERFILES),
    }
    if _public_snapshot_tree():
        return tables
    from ci import release_record
    tables["release_images.SERVICES"] = set(release_images.SERVICES) - set(release_record.FIXTURES)
    tables["release_record.PRODUCT"] = set(release_record.PRODUCT) - {mod.GATEWAY}
    return tables


def disagreements(tables) -> list[str]:
    """Each way the tables fail to name the same engine images, mapped the same way."""
    repo_of = tables["engine_images.ENGINE_DOCKERFILES"]          # Dockerfile -> repository
    service_of = tables["release_load.CI_SERVICE"]                # repository -> CI service
    dockerfile_of = tables["candidate_wheel.DOCKERFILES"]         # CI service -> Dockerfile
    repos = set(repo_of.values())
    services = set(dockerfile_of)
    found = []
    for name, value in (("tested_digests.ENGINE_REPOSITORIES", tables["tested_digests.ENGINE_REPOSITORIES"]),
                        ("release_load.CI_SERVICE", set(service_of))):
        if value != repos:
            found.append(f"{name} names {sorted(value ^ repos)} unlike engine_images.ENGINE_DOCKERFILES")
    for name in _RELEASE_LANE_TABLES:
        if name in tables and tables[name] != services:
            found.append(f"{name} names {sorted(tables[name] ^ services)} unlike candidate_wheel.DOCKERFILES")
    if set(dockerfile_of.values()) != set(repo_of):
        found.append(f"candidate_wheel.DOCKERFILES builds {sorted(set(dockerfile_of.values()) ^ set(repo_of))} "
                     "unlike engine_images.ENGINE_DOCKERFILES")
    for service, dockerfile in sorted(dockerfile_of.items()):
        repo = repo_of.get(dockerfile)
        if repo is not None and service_of.get(repo, service) != service:
            found.append(f"{repo}: CI builds it as {service!r}, release_load maps it to {service_of[repo]!r}")
    return found


def test_every_engine_image_table_names_the_same_images_the_same_way():
    """A seventh image added to some tables and not others used to pass CI and break at roll time
    (a bare KeyError in required_images, or an image no CI lane built)."""
    assert disagreements(_tables()) == []


@pytest.mark.parametrize("table", [
    "engine_images.ENGINE_DOCKERFILES", "tested_digests.ENGINE_REPOSITORIES", "release_load.CI_SERVICE",
    "candidate_wheel.DOCKERFILES", "release_images.SERVICES", "release_record.PRODUCT",
])
def test_a_seventh_image_in_one_table_only_fails_the_agreement(table):
    """Negative fixture: the agreement check catches a new image added to any single table."""
    if table in _RELEASE_LANE_TABLES and _public_snapshot_tree():
        pytest.skip("ci/release_record.py is publish-excluded and absent from this snapshot")
    tables = _tables()
    value = tables[table]
    if table == "engine_images.ENGINE_DOCKERFILES":
        value["okengine-foo/Dockerfile"] = "okengine-foo"
    elif table == "release_load.CI_SERVICE":
        value["okengine-foo"] = "foo"
    elif table == "candidate_wheel.DOCKERFILES":
        value["foo"] = "okengine-foo/Dockerfile"
    else:
        value.add("okengine-foo" if table.startswith("tested") else "foo")
    assert disagreements(tables), f"a 7th image only in {table} went unnoticed"


def test_a_renamed_ci_service_fails_the_agreement():
    tables = _tables()
    tables["release_load.CI_SERVICE"]["okengine-reader"] = "reader-renamed"
    assert any("okengine-reader" in d for d in disagreements(tables))


def test_an_unmapped_engine_repository_is_a_named_fail_not_a_keyerror(world, monkeypatch, capsys):
    monkeypatch.setitem(mod.engine_images.ENGINE_DOCKERFILES, "okengine-foo/Dockerfile", "okengine-foo")
    (world["pack"] / "docker-compose.yml").write_text(
        COMPOSE + '  okengine-foo:\n    build: {context: "${ENGINE_DIR}", dockerfile: okengine-foo/Dockerfile}\n')
    fake = FakeDocker()
    _install(monkeypatch, fake)
    assert call(world) == 1
    err = capsys.readouterr().err
    assert "no CI release service is mapped for engine image(s) okengine-foo" in err, err
    assert fake.calls == []
