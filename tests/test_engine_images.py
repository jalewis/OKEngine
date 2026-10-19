"""okengine#1033 / v0.15.11 audit [27]: compose-built engine images use immutable, deployment-scoped
tags, and a tag moved under a running container is reported.

Before: every pack named its reader/mcp/cockpit/review-write/operation-runner/projection image with
a bare repository (`:latest`). The daemon's tag store is global, so one pack's build (or a local
smoke build) moved the tag every other pack's next `compose up` would recreate onto.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "engine_images.py"
PIN = "${OKENGINE_ENGINE_IMAGE_TAG:?deploy.sh must pin an immutable engine image tag}"


def _load():
    spec = importlib.util.spec_from_file_location("engine_images", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["engine_images"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# --- the red test: no deployment template runs an unscoped, mutable engine tag -----------------

DEPLOY_TEMPLATES = ("templates/**/*.yml", "templates/**/*.yaml")
_SCOPED = re.compile(r"^(?P<repo>[a-z0-9-]+):\$\{OKENGINE_ENGINE_IMAGE_TAG(?::-(?P<default>[^}]*)"
                     r"|:\?[^}]*)\}$")


def unscoped_engine_images(paths) -> list[str]:
    """`file:service image` for every engine image whose tag is not the deploy-pinned variable.
    The only default allowed is `unpinned`, the pre-deploy placeholder the gateway also uses: a
    deployed stack always runs the deploy.sh override, which has no default at all."""
    bad = []
    for path in paths:
        doc = _template(path)
        services = doc.get("services") if isinstance(doc, dict) else None
        for name, spec in (services or {}).items():
            image = str((spec or {}).get("image") or "")
            built = mod.engine_services_in([doc]).get(name)
            if not built and mod.repository(image) not in mod.ENGINE_REPOSITORIES:
                continue
            match = _SCOPED.match(image)
            if not match or match["default"] not in (None, "unpinned") or (
                    built and match["repo"] != built):
                bad.append(f"{path.name}:{name} {image!r}")
    return bad


def _template(path: Path):
    """Templates carry `{{PACK}}` placeholders that are not YAML until `framework init` renders."""
    text = re.sub(r"\{\{[A-Z_]+\}\}", "placeholder", path.read_text(encoding="utf-8"))
    return yaml.load(text, Loader=mod._ComposeLoader) or {}


def _template_paths():
    return sorted({p for pattern in DEPLOY_TEMPLATES for p in REPO.glob(pattern)})


def test_no_deployment_template_uses_an_unscoped_mutable_engine_image_tag():
    paths = _template_paths()
    found = [p for p in paths if mod.engine_services_in([_template(p)])]
    assert any(p.name == "docker-compose.yml" for p in found), "skeleton compose not scanned"
    assert any(p.name == "projection-compose-overlay.yml" for p in found)
    assert not unscoped_engine_images(paths), (
        "an engine image in a deployment template uses a shared, mutable tag; every co-installed "
        "pack would share it (okengine#1033): " + str(unscoped_engine_images(paths)))


@pytest.mark.parametrize("image", [
    "okengine-reader",                                       # the pre-#1033 skeleton
    "okengine-reader:latest",
    "okengine-reader:okengine-v0.15.0-1807fa73",             # hand-pinned, never re-pinned
    "${OKENGINE_COCKPIT_IMAGE:-okengine-reader}",
    "okengine-reader:${OKENGINE_ENGINE_IMAGE_TAG:-latest}",
    "okengine-mcp:${OKENGINE_ENGINE_IMAGE_TAG:-unpinned}",   # builds the reader under mcp's name
])
def test_detector_flags_each_unscoped_shape(tmp_path, image):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(yaml.safe_dump({"services": {"okengine-reader": {
        "build": {"context": "${ENGINE_DIR}", "dockerfile": "okengine-reader/Dockerfile"},
        "image": image}}}), encoding="utf-8")
    assert unscoped_engine_images([compose])


def test_detector_accepts_the_scoped_forms(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(yaml.safe_dump({"services": {
        "okengine-reader": {"build": {"context": "x", "dockerfile": "okengine-reader/Dockerfile"},
                            "image": "okengine-reader:${OKENGINE_ENGINE_IMAGE_TAG:-unpinned}"},
        "okengine-mcp": {"image": f"okengine-mcp:{PIN}"},
        "postgres": {"image": "postgres:17-alpine"}}}), encoding="utf-8")
    assert unscoped_engine_images([compose]) == []


# --- tag derivation ----------------------------------------------------------------------------

def test_project_name_follows_compose_normalization(tmp_path):
    pack = tmp_path / "OKCti.Test"
    pack.mkdir()
    assert mod.project_name(pack, {}) == "okctitest"
    assert mod.project_name(pack, {"COMPOSE_PROJECT_NAME": "_My-Pack_2"}) == "my-pack_2"


def test_a_project_name_with_no_valid_character_is_refused(tmp_path):
    with pytest.raises(ValueError):
        mod.project_name(tmp_path, {"COMPOSE_PROJECT_NAME": "--__"})


def test_image_tag_is_release_sha_and_deployment_scoped():
    assert mod.image_tag("v0.15.12", "abc1234", "okcti-test") == "okengine-v0.15.12-abc1234.okcti-test"
    assert mod.image_tag("v1.0.0", "abc1234-dirty", "p") == "okengine-v1.0.0-abc1234-dirty.p"
    assert mod.image_tag("v1", "abcd", "p") == "okengine-v1-abcd.p"
    assert mod.image_tag("v1", "f" * 40, "p") == f"okengine-v1-{'f' * 40}.p"


@pytest.mark.parametrize("release,sha,project", [
    ("", "abc1234", "p"), ("v1", "", "p"), ("v1/2", "abc1234", "p"), ("v1", "a" * 40, "p" * 90),
    ("v1", "unknown", "p"),            # deploy.sh's old `git rev-parse` fallback: never unique
    ("v1", "unknown-dirty", "p"), ("v1", "abc", "p"), ("v1", "a" * 41, "p"), ("v1", "ABC1234", "p"),
    ("v1", "abc1234-dirty-x", "p"),
])
def test_an_invalid_tag_is_refused(release, sha, project):
    with pytest.raises(ValueError):
        mod.image_tag(release, sha, project)


# --- which compose files, which services -------------------------------------------------------

def _write(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_compose_files_follow_compose_file_and_skip_the_engine_and_sidecar_overrides(tmp_path):
    base = _write(tmp_path / "docker-compose.yml", "services: {}\n")
    extra = _write(tmp_path / ".hermes-data" / "projection-compose.yml", "services: {}\n")
    _write(tmp_path / mod.OVERRIDE, "services: {}\n")
    _write(tmp_path / mod.SIDECARS, "services: {}\n")
    absolute = _write(tmp_path / "abs.yml", "services: {}\n")
    env = {"COMPOSE_FILE": ":".join(["docker-compose.yml", ".hermes-data/projection-compose.yml",
                                     mod.OVERRIDE, str(tmp_path / mod.SIDECARS),
                                     str(absolute), ""])}
    assert mod.compose_files(tmp_path, env) == [base.resolve(), extra.resolve(), absolute.resolve()]


def test_a_missing_compose_file_entry_is_refused_not_skipped(tmp_path):
    """QA on !1317: a skipped COMPOSE_FILE entry produced a partial override with rc=0, leaving
    that file's engine services on whatever tag it names. Compose refuses the entry; so do we."""
    _write(tmp_path / "docker-compose.yml", "services: {}\n")
    env = {"COMPOSE_FILE": "docker-compose.yml:.hermes-data/projection-compose.yml"}
    with pytest.raises(ValueError, match=r"projection-compose\.yml.*does not exist"):
        mod.compose_files(tmp_path, env)


def test_the_engine_and_sidecar_overrides_need_not_exist(tmp_path):
    base = _write(tmp_path / "docker-compose.yml", "services: {}\n")
    env = {"COMPOSE_FILE": f"docker-compose.yml:{mod.OVERRIDE}:{mod.SIDECARS}"}
    assert mod.compose_files(tmp_path, env) == [base.resolve()]


def test_cli_names_the_missing_compose_file_and_exits_one(tmp_path, capsys):
    _write(tmp_path / "docker-compose.yml", "services: {}\n")
    (tmp_path / ".env").write_text("COMPOSE_FILE=docker-compose.yml:gone.yml\n", encoding="utf-8")
    assert mod.main(["override", str(tmp_path)], environ={}) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "engine_images: COMPOSE_FILE names 'gone.yml'" in captured.err


def test_compose_files_default_and_custom_separator(tmp_path):
    base = _write(tmp_path / "docker-compose.yml", "services: {}\n")
    other = _write(tmp_path / "b.yml", "services: {}\n")
    assert mod.compose_files(tmp_path, {}) == [base.resolve()]
    env = {"COMPOSE_FILE": "docker-compose.yml;b.yml", "COMPOSE_PATH_SEPARATOR": ";"}
    assert mod.compose_files(tmp_path, env) == [base.resolve(), other.resolve()]


def test_engine_services_maps_each_engine_dockerfile_and_ignores_pack_builds(tmp_path):
    compose = _write(tmp_path / "docker-compose.yml", {"services": {
        "gateway": {"image": "hermes-agent:x"},
        "okengine-reader": {"build": {"context": "${ENGINE_DIR}",
                                      "dockerfile": "./okengine-reader/Dockerfile"},
                            "image": "okengine-reader"},
        "review": {"build": {"context": "${ENGINE_DIR}",
                             "dockerfile": "okengine-mcp/Dockerfile.review"}},
        "short": {"build": "${ENGINE_DIR}/okengine-cockpit"},
        "mapped-context": {"build": {"context": "../engine/okengine-projection"}},
        "pack-owned": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "image": "mine"},
        "by-image": {"image": "okengine-operation-runner:latest"},
        "not-a-mapping": "x",
        "empty": None,
    }})
    assert mod.engine_services([compose]) == {
        "by-image": "okengine-operation-runner", "mapped-context": "okengine-projection",
        "okengine-reader": "okengine-reader", "review": "okengine-review-write",
        "short": "okengine-cockpit"}


def test_a_later_files_build_wins_as_compose_merges_it(tmp_path):
    first = _write(tmp_path / "a.yml", {"services": {"svc": {
        "build": {"context": ".", "dockerfile": "okengine-reader/Dockerfile"}}}})
    second = _write(tmp_path / "b.yml", {"services": {"svc": {
        "build": {"context": ".", "dockerfile": "okengine-mcp/Dockerfile"}}}})
    assert mod.engine_services([first, second]) == {"svc": "okengine-mcp"}


def test_compose_merge_tags_and_empty_files_load(tmp_path):
    tagged = _write(tmp_path / "a.yml", "services:\n  okengine-mcp:\n    image: !override okengine-mcp\n"
                    "    ports: !reset []\n    environment: !override {A: 1}\n"
                    "    volumes: !reset\n      - x\n")
    empty = _write(tmp_path / "b.yml", "")
    listed = _write(tmp_path / "c.yml", "- not a mapping\n")
    assert mod.engine_services([tagged, empty, listed]) == {"okengine-mcp": "okengine-mcp"}


def test_repository_parsing():
    assert mod.repository("okengine-mcp") == "okengine-mcp"
    assert mod.repository("okengine-mcp:tag") == "okengine-mcp"
    assert mod.repository("okengine-mcp@sha256:" + "0" * 64) == "okengine-mcp"
    assert mod.repository("reg:5000/ns/okengine-mcp:1@sha256:" + "0" * 64) == "reg:5000/ns/okengine-mcp"


def test_render_override_pins_every_service_with_a_fail_closed_variable():
    text = mod.render_override({"okengine-reader": "okengine-reader", "b": "okengine-mcp"})
    assert mod.BUILD_VAR not in text, "no build label without a build"
    doc = yaml.safe_load("services:\n" + text)
    assert doc == {"services": {"b": {"image": f"okengine-mcp:{PIN}"},
                                "okengine-reader": {"image": f"okengine-reader:{PIN}"}}}
    assert mod.render_override({}) == ""


@pytest.mark.parametrize("shape", ["okcti", "ai-research", "skeleton"])
def test_every_live_compose_shape_is_fully_pinned_by_the_override(tmp_path, shape):
    """Migration across the fleet's shapes: whatever a deployment's files say, the effective image
    of every engine-built service after the override is the scoped tag, and nothing is added."""
    skeleton = (REPO / "templates/pack/skeleton/docker-compose.yml").read_text(encoding="utf-8")
    shapes = {
        "okcti": "services:\n  gateway: {image: hermes-agent}\n"
                 "  okengine-reader: {build: {context: e, dockerfile: okengine-reader/Dockerfile},"
                 " image: okengine-reader}\n"
                 "  okengine-review-write: {build: {context: e, dockerfile: "
                 "okengine-mcp/Dockerfile.review}, image: okengine-review-write}\n"
                 "  okengine-cockpit: {build: {context: e, dockerfile: okengine-cockpit/Dockerfile},"
                 " image: '${OKENGINE_COCKPIT_IMAGE:-okengine-cockpit}'}\n",
        "ai-research": "services:\n  okengine-reader: {build: {context: e, dockerfile: "
                       "okengine-reader/Dockerfile}, image: okengine-reader}\n"
                       "  okengine-mcp: {build: {context: e, dockerfile: okengine-mcp/Dockerfile},"
                       " image: okengine-mcp}\n",
        "skeleton": re.sub(r"\{\{[A-Z_]+\}\}", "placeholder", skeleton),
    }
    base = _write(tmp_path / "docker-compose.yml", shapes[shape])
    projection = (REPO / "templates/pack/projection-compose-overlay.yml").read_text()
    overlay = _write(tmp_path / ".hermes-data/projection-compose.yml",
                     projection.replace("okengine-projection:${OKENGINE_ENGINE_IMAGE_TAG:-unpinned}",
                                        "okengine-projection"))
    files = mod.compose_files(tmp_path, {"COMPOSE_FILE":
                                         "docker-compose.yml:.hermes-data/projection-compose.yml"})
    assert files == [base.resolve(), overlay.resolve()]
    pinned = mod.engine_services(files)
    built = mod.built_services(files)
    override = yaml.safe_load("services:\n" + mod.render_override(pinned, built))["services"]
    defined = set()
    for path in files:
        defined |= set((yaml.load(path.read_text(), Loader=mod._ComposeLoader) or {})["services"])
    assert set(override) <= defined, "the override must never introduce a service"
    for name, repo in pinned.items():
        assert override[name]["image"] == f"{repo}:{PIN}"
        assert ("build" in override[name]) == (name in built)
    assert "okengine-projection" in override and "okengine-reader" in override


# --- drift: running image vs what the compose reference resolves to ----------------------------

def fake_docker(config=None, ps="c1 c2\n", inspect=None, images=None, fail=()):
    """A docker CLI boundary: answers only the calls engine_images makes."""
    images = images or {}
    calls = []

    def run(args):
        calls.append(args)
        verb = " ".join(args[1:3])
        if verb in fail:
            return None
        if verb == "compose config":
            return json.dumps(config) if not isinstance(config, str) else config
        if verb == "compose ps":
            return ps
        if verb == "inspect --format":
            return inspect
        if verb == "image inspect":
            ref = args[-1]
            if ref not in images:
                return None
            # Fixtures read "<id> [<build label>]"; the resolver's format is "<id>|<label>|<revision>".
            value = images[ref]
            if "|" not in value:
                image_id, *label = value.split()
                value = f"{image_id}|{label[0] if label else ''}|"
            return value + "\n"
        raise AssertionError(f"unexpected docker call {args}")
    run.calls = calls
    return run


TAG = "okengine-v1.2.3-abc1234.pack"
READER = f"okengine-reader:{TAG}"
CONFIG = {"services": {"okengine-reader": {"image": READER},
                       "gateway": {"image": "hermes-agent:g"}, "built": {"build": "."}}}
EMPTY = {"in_sync": [], "drifted": [], "unresolved": [], "unpinned": [], "unconfigured": [],
         "shared": [], "unattributed": []}


def test_matching_containers_are_in_sync(capsys):
    run = fake_docker(CONFIG, inspect="okengine-reader sha256:aa\ngateway sha256:gg\n",
                      images={READER: f"sha256:aa {TAG}", "hermes-agent:g": "sha256:gg"})
    assert mod.drift_main(run, environ={"COMPOSE_PROJECT_NAME": "pack"}) == 0
    assert "2 of 2 running service(s) match" in capsys.readouterr().out


def test_a_tag_moved_under_a_running_container_is_drift(capsys):
    """The #1033 incident: a local build moved the tag; the container still runs the old ID."""
    run = fake_docker(CONFIG, inspect="okengine-reader sha256:old\ngateway sha256:gg\n",
                      images={READER: f"sha256:new {TAG}", "hermes-agent:g": "sha256:gg"})
    assert mod.drift_main(run, environ={"COMPOSE_PROJECT_NAME": "pack"}) == 1
    out = capsys.readouterr().out
    assert f"DRIFTED okengine-reader ({READER} -> sha256:new, running sha256:old)" in out


def test_drift_is_reported_whichever_way_the_ids_sort_and_ids_are_shortened(capsys):
    low, high = "sha256:" + "a" * 64, "sha256:" + "f" * 64
    run = fake_docker({"services": {"r": {"image": "r:t"}}}, inspect=f"r {low}\n",
                      images={"r:t": high})
    assert mod.drift_main(run, environ={}) == 1
    assert f"DRIFTED r (r:t -> {high[:19]}, running {low[:19]})" in capsys.readouterr().out


def test_a_reference_that_names_no_image_is_unresolved(capsys):
    run = fake_docker(CONFIG, inspect="okengine-reader sha256:aa\n", images={})
    assert mod.drift_main(run, environ={}) == 1
    assert f"UNRESOLVED okengine-reader ({READER})" in capsys.readouterr().out


def test_each_distinct_reference_is_resolved_once():
    config = {"services": {"a": {"image": "x:1"}, "b": {"image": "x:1"}}}
    run = fake_docker(config, inspect="a sha256:aa\nb sha256:aa\n", images={"x:1": "sha256:aa"})
    assert mod.drift_main(run, environ={}) == 0
    assert sum(1 for c in run.calls if c[1:3] == ["image", "inspect"]) == 1


def test_a_running_service_with_no_image_reference_anywhere_is_unpinned_and_fails(capsys):
    config = {"services": {"built": {"build": "."}}}
    result = mod.compare({}, {"built": "sha256:aa"}, lambda ref: pytest.fail("resolved"))
    assert result == {**EMPTY, "unpinned": ["built"]}
    run = fake_docker(config, inspect="built sha256:aa\nmalformed\nfour part line x\n")
    assert mod.collect(run) == ({}, {"built": "sha256:aa"}, {}), \
        "only `<service> <id> [<ref>]` lines count"
    assert mod.drift_main(run, environ={}) == 1, "a service nothing can judge is not a pass"
    assert "UNPINNED built" in capsys.readouterr().out
    later = mod.compare({"z": "x:1"}, {"a-unpinned": "sha256:aa", "z": "sha256:bb"},
                        lambda ref: ("sha256:bb", ""))
    assert later["unpinned"] == ["a-unpinned"] and later["in_sync"] == ["z"], \
        "an unpinned service must not stop the remaining services being judged"


# --- okengine#1042 [24]: a running service the profile-filtered config omits is still judged ----

def test_a_hand_started_profile_service_is_judged_by_its_own_reference(capsys):
    """[24]: `docker compose --profile review up -d` with COMPOSE_PROFILES unset. `config` omits
    review-write; `ps` lists it. Before, it landed in `unpinned` and the run exited 0."""
    review = f"okengine-review-write:{TAG}"
    run = fake_docker(CONFIG, ps="c1 c2\n",
                      inspect=f"okengine-reader sha256:aa {READER}\n"
                              f"okengine-review-write sha256:old {review}\n",
                      images={READER: f"sha256:aa {TAG}", review: f"sha256:moved {TAG}"})
    assert mod.drift_main(run, environ={"COMPOSE_PROJECT_NAME": "pack"}) == 1
    out = capsys.readouterr().out
    assert "DRIFTED okengine-review-write" in out and f"UNCONFIGURED okengine-review-write ({review})" in out


def test_an_unconfigured_service_in_sync_is_reported_but_passes(capsys):
    review = f"okengine-review-write:{TAG}"
    run = fake_docker(CONFIG, inspect=f"okengine-review-write sha256:aa {review}\n",
                      images={review: f"sha256:aa {TAG}"})
    assert mod.drift_main(run, environ={"COMPOSE_PROJECT_NAME": "pack"}) == 0
    assert f"UNCONFIGURED okengine-review-write ({review})" in capsys.readouterr().out


# --- okengine#1042 [12]: an engine image on a shared tag is flagged, though the tag resolves ----

@pytest.mark.parametrize("ref", [
    "okengine-reader:unpinned",                          # the skeleton's pre-deploy placeholder
    "okengine-reader:latest", "okengine-reader",          # the pre-#1033 shared tag
    "okengine-reader:okengine-v0.15.0-1807fa73",          # hand-pinned, not deployment-scoped
    "okengine-reader:okengine-v1.2.3-abc1234.other",      # another deployment's scoped tag
])
def test_an_engine_image_on_a_shared_tag_is_flagged(capsys, ref):
    run = fake_docker({"services": {"okengine-reader": {"image": ref}}},
                      inspect="okengine-reader sha256:aa\n", images={ref: "sha256:aa unattributed"})
    assert mod.drift_main(run, environ={"COMPOSE_PROJECT_NAME": "pack"}) == 1
    assert f"SHARED okengine-reader ({ref})" in capsys.readouterr().out


def test_scoped_tags_are_recognized():
    assert mod.is_scoped(READER) and mod.is_scoped(READER, "pack")
    assert mod.is_scoped("okengine-mcp:okengine-v1-abcd-dirty.p_2", "p_2")
    assert not mod.is_scoped(READER, "other")
    assert not mod.is_scoped("okengine-mcp:okengine-v1-abc.p")         # sha too short
    assert not mod.is_scoped("okengine-mcp:xokengine-v1-abcd.p")
    assert mod.tag_of("reg:5000/okengine-mcp:t@sha256:" + "0" * 64) == "t"
    assert mod.tag_of("okengine-mcp") == ""


def test_a_non_engine_image_is_never_shared_or_unattributed():
    result = mod.compare({"pg": "postgres:17-alpine"}, {"pg": "sha256:aa"},
                         lambda ref: ("sha256:aa", ""), project="pack")
    assert result == {**EMPTY, "in_sync": ["pg"]}


def test_an_unresolved_service_does_not_stop_later_services_being_judged():
    result = mod.compare({"a": "x:gone", "b": "x:1"}, {"a": "sha256:aa", "b": "sha256:bb"},
                         lambda ref: None if ref == "x:gone" else ("sha256:bb", ""))
    assert result["unresolved"] == ["a (x:gone)"] and result["in_sync"] == ["b"]


def test_a_non_engine_service_does_not_stop_later_engine_services_being_judged():
    configured = {"a-postgres": "postgres:17-alpine", "z-reader": "okengine-reader:latest"}
    result = mod.compare(configured, {"a-postgres": "sha256:aa", "z-reader": "sha256:bb"},
                         lambda ref: ("sha256:aa" if ref.startswith("postgres") else "sha256:bb", ""),
                         project="pack")
    assert result["shared"] == ["z-reader (okengine-reader:latest)"]


# --- okengine#1042 [14]: an engine image deploy.sh did not build is UNATTRIBUTED ----------------

@pytest.mark.parametrize("label", ["", "unattributed", "okengine-v1.2.3-fff0000.pack"])
def test_an_image_whose_label_does_not_name_its_tag_is_unattributed(label):
    result = mod.compare({"okengine-reader": READER}, {"okengine-reader": "sha256:aa"},
                         lambda ref: ("sha256:aa", label), project="pack")
    assert result["in_sync"] == ["okengine-reader"]
    assert result["unattributed"] == [
        f"okengine-reader ({READER}, {mod.LABEL}={label or 'absent'})"]


def test_resolver_reads_the_id_and_the_provenance_label():
    seen = []

    def run(args):
        seen.append(args)
        return {"a": f"sha256:aa|{TAG}|<no value>\n", "b": "sha256:bb|<no value>|<no value>\n",
                "c": "sha256:cc|\n", "d": "\n", "e": f"sha256:ee|<no value>|{'f' * 40}\n",
                "f": "sha256:ff\n"}.get(args[-1])
    resolve = mod.resolver(run)
    assert resolve("a") == ("sha256:aa", TAG, "")
    assert resolve("b") == ("sha256:bb", "", ""), "`<no value>` is an absent label"
    assert resolve("c") == ("sha256:cc", "", ""), "nil labels"
    assert resolve("e") == ("sha256:ee", "", "f" * 40), "okengine#1048: CI's revision label"
    assert resolve("f") == ("sha256:ff", "", "")
    assert resolve("d") is None and resolve("missing") is None
    assert mod.LABEL in seen[0][4] and mod.REVISION_LABEL in seen[0][4]
    # Live replay 2026-10-11: `index .Config.Labels` on postgres (nil labels) failed the inspect,
    # so every postgres read UNRESOLVED. `with` guards the nil map.
    assert seen[0][4].startswith("{{.Id}}|{{with .Config.Labels}}")


def test_render_override_labels_only_built_services():
    text = mod.render_override({"a": "okengine-reader", "b": "okengine-mcp"}, {"a"})
    doc = yaml.safe_load("services:\n" + text)["services"]
    assert doc["a"]["build"] == {"labels": {mod.LABEL: "${OKENGINE_ENGINE_BUILD_TAG:-unattributed}"}}
    assert "build" not in doc["b"], "a build entry would create a build an image-only service lacks"


def test_override_cli_labels_the_built_engine_services(tmp_path, capsys):
    _write(tmp_path / "docker-compose.yml", {"services": {
        "okengine-mcp": {"build": {"context": "e", "dockerfile": "okengine-mcp/Dockerfile"}},
        "okengine-reader": {"image": "okengine-reader"}}})
    assert mod.main(["override", str(tmp_path)], environ={}) == 0
    doc = yaml.safe_load("services:\n" + capsys.readouterr().out)["services"]
    assert mod.LABEL in doc["okengine-mcp"]["build"]["labels"] and "build" not in doc["okengine-reader"]


@pytest.mark.parametrize("kwargs", [
    {"fail": ("compose config",)},
    {"fail": ("compose ps",)},
    {"fail": ("inspect --format",)},
    {"config": "not json"},
    {"config": "[1, 2]"},
    {"ps": "", "inspect": ""},
])
def test_what_cannot_be_read_is_undetectable_not_clean(capsys, kwargs):
    args = {"config": CONFIG, "inspect": "okengine-reader sha256:aa\n", **kwargs}
    assert mod.drift_main(fake_docker(**args)) == 2
    assert "UNDETECTABLE" in capsys.readouterr().err


def test_run_returns_stdout_or_none(monkeypatch):
    assert mod._run([sys.executable, "-c", "print('hi')"]) == "hi\n"
    assert mod._run([sys.executable, "-c", "raise SystemExit(3)"]) is None
    killed = "import os, signal; print('partial', flush=True); os.kill(os.getpid(), signal.SIGKILL)"
    assert mod._run([sys.executable, "-c", killed]) is None, "a killed docker is not an answer"
    assert mod._run(["/nonexistent/docker-binary"]) is None


# --- CLI --------------------------------------------------------------------------------------

def test_cli_tag_reads_the_project_from_the_pack_env(tmp_path, capsys):
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME='okcti-test'\n", encoding="utf-8")
    assert mod.main(["tag", str(tmp_path), "--release", "v1.2.3", "--sha", "abc1234"],
                    environ={}) == 0
    assert capsys.readouterr().out == "okengine-v1.2.3-abc1234.okcti-test\n"


def test_cli_process_environment_beats_the_pack_env_as_compose_does(tmp_path, capsys):
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=fromfile\n", encoding="utf-8")
    assert mod.main(["tag", str(tmp_path), "--release", "v1", "--sha", "abc1234"],
                    environ={"COMPOSE_PROJECT_NAME": "fromshell"}) == 0
    assert capsys.readouterr().out.strip().endswith(".fromshell")


def test_cli_override_prints_entries_for_the_pack(tmp_path, capsys):
    _write(tmp_path / "docker-compose.yml", {"services": {"okengine-mcp": {"image": "okengine-mcp"}}})
    assert mod.main(["override", str(tmp_path)], environ={}) == 0
    assert capsys.readouterr().out == f"  okengine-mcp:\n    image: okengine-mcp:{PIN}\n"


def test_cli_errors_exit_one_with_a_message(tmp_path, capsys):
    _write(tmp_path / "docker-compose.yml", "services: [unterminated\n")
    assert mod.main(["override", str(tmp_path)], environ={}) == 1
    assert mod.main(["tag", str(tmp_path), "--release", "", "--sha", "a"], environ={}) == 1
    assert capsys.readouterr().err.count("engine_images:") == 2


@pytest.mark.parametrize("argv", [[], ["tag", "p", "--sha", "a"], ["tag", "p", "--release", "v1"],
                                  ["override"]])
def test_cli_refuses_incomplete_arguments(argv):
    with pytest.raises(SystemExit):
        mod.main(argv, environ={})


def test_the_script_runs_as_a_command(tmp_path):
    import subprocess
    out = subprocess.run([sys.executable, str(SCRIPT), "tag", str(tmp_path / "Pack"),
                          "--release", "v1", "--sha", "abc1234"], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}, check=True)
    assert out.stdout == "okengine-v1-abc1234.pack\n"


def test_cli_drift_runs_against_docker(monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "drift_main", lambda environ: seen.append(environ) or 0)
    assert mod.main(["drift"], environ={"A": "1"}) == 0 and seen == [{"A": "1"}]


def test_drift_judges_against_the_deployment_dirs_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=pack\n", encoding="utf-8")
    assert mod._current_project({}) == "pack"
    assert mod._current_project({"COMPOSE_PROJECT_NAME": "--"}) is None


def test_post_deploy_verify_runs_the_drift_check_as_a_warning():
    text = (REPO / "scripts/post_deploy_verify.sh").read_text(encoding="utf-8")
    start = text.index("# image tag drift (okengine#1033)")
    block = text[start:text.index("\nesac\n", start) + 6]
    assert 'scripts/engine_images.py" drift' in block
    assert "bad " not in block, "tag drift is a WARN: the running stack itself is unchanged"


# --- okengine#1042 [25]: the Dockerfile map is complete, and an unknown engine build is refused --

def test_every_engine_dockerfile_in_the_repo_is_mapped():
    found = {p.relative_to(REPO).as_posix() for p in REPO.glob("okengine-*/Dockerfile*")}  # glob-ok: repo Dockerfiles
    assert found == set(mod.ENGINE_DOCKERFILES), (
        "an engine Dockerfile is missing from engine_images.ENGINE_DOCKERFILES (or a mapped one is "
        f"gone); deploy.sh would leave its image on a shared tag: {found ^ set(mod.ENGINE_DOCKERFILES)}")


@pytest.mark.parametrize("build", [
    {"context": "${ENGINE_DIR}", "dockerfile": "okengine-newsvc/Dockerfile"},
    {"context": "${ENGINE_DIR}", "dockerfile": "./okengine-mcp/Dockerfile.other"},
    "${ENGINE_DIR}/okengine-newsvc",
])
def test_an_unmapped_engine_dockerfile_is_refused_not_left_on_latest(build):
    """Negative fixture for [25]: before, this returned no engine service and the override skipped
    it, so a seventh engine image would have stayed on the shared `:latest` tag."""
    with pytest.raises(ValueError, match="ENGINE_DOCKERFILES"):
        mod.engine_services_in([{"services": {"new": {"build": build, "image": "okengine-newsvc"}}}])


def test_pack_owned_builds_are_still_ignored():
    docs = [{"services": {"a": {"build": {"context": ".", "dockerfile": "docker/Dockerfile"}},
                          "b": {"build": "./mysvc"}}}]
    assert mod.engine_services_in(docs) == {}


# --- okengine#1042 [8]: one compose project, one deployment directory ---------------------------

def _owners(out):
    return lambda args: out


def test_claim_accepts_a_project_only_this_directory_runs(tmp_path):
    pack = tmp_path / "x"
    pack.mkdir()
    assert mod._cmd_claim(type("A", (), {"pack": str(pack)}), {}, _owners(f"{pack}\n{pack}\n")) == 0
    assert mod._cmd_claim(type("A", (), {"pack": str(pack)}), {}, _owners("")) == 0, \
        "a first deploy has no containers yet"


def test_claim_accepts_the_same_directory_reached_through_a_symlink(tmp_path):
    """Live replay 2026-10-11: one deployment's containers record both `~/Source/okcti-test` and
    `~/MEGA/Source/okcti-test` (~/Source is a symlink). That is one owner, not two."""
    real = tmp_path / "MEGA" / "Source"
    (real / "okcti-test").mkdir(parents=True)
    (tmp_path / "Source").symlink_to(real)
    owners = f"{tmp_path}/Source/okcti-test\n{real}/okcti-test\n"
    args = type("A", (), {"pack": str(tmp_path / "Source" / "okcti-test")})
    assert mod._cmd_claim(args, {}, _owners(owners)) == 0


def test_claim_refuses_a_second_deployment_with_the_same_basename(tmp_path, capsys):
    """[8]: <a>/x and <b>/x both derive project `x` and tag `okengine-...x`; B's compose up would
    recreate A's containers onto B's directory."""
    a, b = tmp_path / "a" / "x", tmp_path / "b" / "x"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert mod.project_name(a, {}) == mod.project_name(b, {})
    assert mod._cmd_claim(type("A", (), {"pack": str(b)}), {}, _owners(f"{a}\n")) == 1
    err = capsys.readouterr().err
    assert f"already belongs to {str(a)!r}, not {str(b)!r}" in err and "COMPOSE_PROJECT_NAME" in err
    assert mod._cmd_claim(type("A", (), {"pack": str(a)}), {}, _owners(f"{b}\n\n")) == 1, \
        "refused whichever way the two paths sort"
    assert f"already belongs to '<unlabeled>', {str(b)!r}" in capsys.readouterr().err


def test_claim_honors_a_unique_compose_project_name(tmp_path):
    b = tmp_path / "b" / "x"
    b.mkdir(parents=True)
    seen = []

    def run(args):
        seen.append(args)
        return ""
    (b / ".env").write_text("COMPOSE_PROJECT_NAME=x-b\n", encoding="utf-8")
    assert mod._cmd_claim(type("A", (), {"pack": str(b)}), {}, run) == 0
    assert "label=com.docker.compose.project=x-b" in seen[0]


def test_claim_treats_an_unlabeled_container_as_foreign(tmp_path):
    assert mod.foreign_owners(tmp_path, ["", str(tmp_path)]) == [""]


def test_claim_is_undetectable_when_docker_cannot_answer(tmp_path, capsys):
    assert mod._cmd_claim(type("A", (), {"pack": str(tmp_path)}), {}, _owners(None)) == 2
    assert "UNDETECTABLE" in capsys.readouterr().err


def test_claim_cli_dispatch_and_errors(tmp_path, monkeypatch, capsys):
    """The CLI must use the docker boundary as it is at call time: CI runners have no docker,
    and a default bound at import made this exit 2 there while passing on a docker host."""
    seen = []
    monkeypatch.setattr(mod, "_run", lambda args: seen.append(args) or "")
    pack = tmp_path / "p"
    pack.mkdir()
    assert mod.main(["claim", str(pack)], environ={}) == 0
    assert seen and seen[0][:3] == ["docker", "ps", "-a"]
    assert mod.main(["claim", str(pack)], environ={"COMPOSE_PROJECT_NAME": "--"}) == 1
    assert "engine_images:" in capsys.readouterr().err


# --- okengine#1042 [13]/[31]: no printed remediation rebuilds an engine image without the override

_UNSCOPED_COMPOSE = re.compile(r"docker compose\s+-f\s+\S*docker-compose\.yml\S*\s+(build|up)\b")


def unscoped_compose_remediations(texts: dict[str, str]) -> list[str]:
    """`file:line` for every `docker compose -f docker-compose.yml build|up`: it drops the engine
    image override in COMPOSE_FILE and builds onto the shared, unscoped tag."""
    return [f"{name}:{n}" for name, text in texts.items()
            for n, line in enumerate(text.splitlines(), 1) if _UNSCOPED_COMPOSE.search(line)]


def test_no_script_rebuilds_an_engine_image_around_the_override():
    texts = {p.name: p.read_text(encoding="utf-8") for p in sorted((REPO / "scripts").glob("*.sh"))}  # glob-ok: engine scripts
    assert texts, "no scripts scanned"
    assert unscoped_compose_remediations(texts) == []


def test_the_unscoped_remediation_detector_flags_the_pre_fix_hint():
    old = """echo "       docker compose -f '$PACK_DIR/docker-compose.yml' build okengine-mcp &&" >&2"""
    up = """echo "       docker compose -f '$PACK_DIR/docker-compose.yml' up -d okengine-mcp" >&2"""
    probe = """docker compose -f "$PACK_DIR/docker-compose.yml" exec -T okengine-mcp sh"""
    assert unscoped_compose_remediations({"s": "\n".join([old, up, probe])}) == ["s:1", "s:2"]


def test_the_stage_step_points_a_stale_read_mcp_at_deploy_sh():
    text = (REPO / "scripts/deploy-cron-scripts.sh").read_text(encoding="utf-8")
    hint = text[text.index("mcp_rebuild_hint() {"):]
    hint = hint[:hint.index("}")]
    assert "scripts/deploy.sh" in hint
    assert text.count("mcp_rebuild_hint\n") == 2, "both stale-MCP branches print the hint"


# --- okengine#1048: images deploy.sh --release-digests loads from CI ----------------------------

CI_REVISION = "abc1234" + "0" * 33


def test_a_ci_built_image_on_the_scoped_tag_of_its_commit_is_attributed():
    result = mod.compare({"okengine-reader": READER}, {"okengine-reader": "sha256:aa"},
                         lambda ref: ("sha256:aa", "", CI_REVISION), project="pack")
    assert result == {**EMPTY, "in_sync": ["okengine-reader"]}


@pytest.mark.parametrize("revision", ["fff1234" + "0" * 33, "abc1234", "", "ABC1234" + "0" * 33])
def test_a_revision_label_for_another_commit_or_malformed_is_unattributed(revision):
    result = mod.compare({"okengine-reader": READER}, {"okengine-reader": "sha256:aa"},
                         lambda ref: ("sha256:aa", "", revision), project="pack")
    assert result["unattributed"] == [f"okengine-reader ({READER}, {mod.LABEL}=absent)"]


def test_a_dirty_scoped_tag_is_never_attributed_to_a_ci_image():
    dirty = "okengine-reader:okengine-v1.2.3-abc1234-dirty.pack"
    result = mod.compare({"okengine-reader": dirty}, {"okengine-reader": "sha256:aa"},
                         lambda ref: ("sha256:aa", "", CI_REVISION), project="pack")
    assert result["unattributed"] and not result["shared"]


def test_a_digest_reference_is_pinned_not_shared():
    ref = "okengine-reader@sha256:" + "b" * 64
    result = mod.compare({"okengine-reader": ref}, {"okengine-reader": "sha256:aa"},
                         lambda ref: ("sha256:aa", "", CI_REVISION), project="pack")
    assert result == {**EMPTY, "in_sync": ["okengine-reader"]}


def test_a_digest_reference_without_provenance_is_unattributed_not_shared():
    ref = "okengine-reader@sha256:" + "b" * 64
    result = mod.compare({"okengine-reader": ref}, {"okengine-reader": "sha256:aa"},
                         lambda ref: ("sha256:aa", "", ""), project="pack")
    assert result["unattributed"] and not result["shared"]


# --- okengine#1044 [17]: the verifier's WARN names every failing drift finding --------------------
# post_deploy_verify.sh built its WARN from a hand-typed `grep -E 'DRIFTED|...|UNATTRIBUTED'` of
# drift's output, pinned only by a substring test. A failing kind added to drift_main without the
# grep made the verifier warn "not pinned to what is running: " and name nothing. These run the
# real verifier against a stub engine whose engine_images.py is the real module with collect and
# compare replaced, so the shell and the Python side are judged together.

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

_STUB_DRIFT = '''import json, os, sys
sys.path.insert(0, {scripts!r})
import engine_images as m
extra = os.environ.get("W1044_EXTRA_KIND")
if extra:
    m.FAILING_KINDS = (*m.FAILING_KINDS, extra)
    m.REPORTED_KINDS = (*m.REPORTED_KINDS, extra)
kinds = json.loads(os.environ["W1044_KINDS"])
m.collect = lambda run=None: ({{"svc": "repo:t"}}, {{"svc": "repo:t"}}, {{}})
m.compare = lambda *a, **k: {{"in_sync": [], **{{kind: [] for kind in m.REPORTED_KINDS}},
                              **{{kind: ["svc-" + kind] for kind in kinds}}}}
sys.exit(m.drift_main())
'''


def _stub_verifier_engine(tmp_path: Path) -> Path:
    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    for entry in REPO.iterdir():
        if entry.name not in ("scripts", ".git"):
            (engine / entry.name).symlink_to(entry)
    for entry in (REPO / "scripts").iterdir():
        if entry.name not in ("engine_images.py", "post_deploy_verify.sh"):
            (engine / "scripts" / entry.name).symlink_to(entry)
    shutil.copy(REPO / "scripts" / "post_deploy_verify.sh", engine / "scripts")
    (engine / "scripts" / "engine_images.py").write_text(
        _STUB_DRIFT.format(scripts=str(REPO / "scripts")), encoding="utf-8")
    return engine


def _verify_drift(tmp_path: Path, kinds: list[str], extra: str = "") -> str:
    engine = _stub_verifier_engine(tmp_path)
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "W1044_KINDS": json.dumps(kinds), "W1044_EXTRA_KIND": extra}
    proc = subprocess.run(["bash", str(engine / "scripts" / "post_deploy_verify.sh")],
                          cwd=deployment, capture_output=True, text=True, timeout=300, env=env)
    out = proc.stdout + proc.stderr
    return next((line for line in out.splitlines()
                 if "engine image tag(s) not pinned to what is running" in line), "")


needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")


@needs_bash
@pytest.mark.parametrize("kind", ["drifted", "unresolved", "unpinned", "shared", "unattributed"])
def test_the_verifier_warning_names_each_failing_drift_finding(tmp_path, kind):
    warning = _verify_drift(tmp_path, [kind])
    assert f"{kind.upper()} svc-{kind}" in warning, warning or "no drift WARN was printed"


@needs_bash
def test_a_failing_kind_added_later_is_still_named_by_the_verifier(tmp_path):
    """The negative fixture for a hand-typed filter: a kind the shell has never heard of."""
    warning = _verify_drift(tmp_path, ["regressed"], extra="regressed")
    assert "REGRESSED svc-regressed" in warning, warning or "no drift WARN was printed"


def test_the_failing_kinds_are_a_subset_of_the_reported_ones():
    assert set(mod.FAILING_KINDS) == {"drifted", "unresolved", "unpinned", "shared", "unattributed"}
    assert set(mod.FAILING_KINDS) <= set(mod.REPORTED_KINDS)
    assert "unconfigured" in mod.REPORTED_KINDS and "unconfigured" not in mod.FAILING_KINDS


# --- okengine#1044 [26]: the project name is the one Compose runs, including `name:` and links ---
# project_name read only COMPOSE_PROJECT_NAME and the RESOLVED directory name. Compose's order is
# COMPOSE_PROJECT_NAME, then the compose files' top-level `name:`, then the directory basename as
# spelled (a symlink is not resolved). Two packs that both declare `name: shared` were claimed as
# two different projects, and the claim (which fails open on an empty owner list) passed while the
# second deploy recreated the first one's containers.

def _compose(pack: Path, body: str, name: str = "docker-compose.yml") -> None:
    pack.mkdir(parents=True, exist_ok=True)
    (pack / name).write_text(body, encoding="utf-8")


def test_project_name_follows_the_compose_files_top_level_name(tmp_path):
    pack = tmp_path / "pack-dir"
    _compose(pack, "name: Shared.Stack\nservices: {}\n")
    assert mod.project_name(pack, {}) == "sharedstack"


def test_compose_project_name_still_beats_the_files_name(tmp_path):
    pack = tmp_path / "pack-dir"
    _compose(pack, "name: shared\nservices: {}\n")
    assert mod.project_name(pack, {"COMPOSE_PROJECT_NAME": "mine"}) == "mine"


def test_a_later_compose_file_name_overrides_an_earlier_one(tmp_path):
    pack = tmp_path / "pack-dir"
    _compose(pack, "name: first\nservices: {}\n")
    _compose(pack, "name: second\n", "extra.yml")
    env = {"COMPOSE_FILE": "docker-compose.yml:extra.yml"}
    assert mod.project_name(pack, env) == "second"


def test_the_default_override_file_name_counts_when_compose_file_is_unset(tmp_path):
    pack = tmp_path / "pack-dir"
    _compose(pack, "services: {}\n")
    _compose(pack, "name: from-override\n", "docker-compose.override.yml")
    assert mod.project_name(pack, {}) == "from-override"


def test_an_interpolated_name_is_refused_not_guessed(tmp_path):
    pack = tmp_path / "pack-dir"
    _compose(pack, "name: ${STACK}\nservices: {}\n")
    with pytest.raises(ValueError, match="COMPOSE_PROJECT_NAME"):
        mod.project_name(pack, {})


@pytest.mark.parametrize("body", ["services: {}\n", "- not a mapping\n", "name: ''\n", ": [\n"])
def test_a_file_without_a_usable_name_falls_back_to_the_directory(tmp_path, body):
    pack = tmp_path / "pack-dir"
    _compose(pack, body)
    assert mod.project_name(pack, {}) == "pack-dir"


def test_a_symlinked_pack_dir_is_named_by_its_link_not_its_target(tmp_path):
    target = tmp_path / "real-target"
    target.mkdir()
    link = tmp_path / "link-name"
    link.symlink_to(target)
    assert mod.project_name(link, {}) == "link-name"


def test_claim_refuses_two_packs_that_declare_the_same_compose_name(tmp_path, capsys):
    a, b = tmp_path / "pack-a", tmp_path / "pack-b"
    _compose(a, "name: shared\nservices: {}\n")
    _compose(b, "name: shared\nservices: {}\n")
    asked = []

    def run(args):
        asked.append(args)
        return f"{a}\n"
    assert mod._cmd_claim(type("A", (), {"pack": str(b)}), {}, run) == 1
    assert "label=com.docker.compose.project=shared" in asked[0]
    assert "already belongs to" in capsys.readouterr().err


def test_drift_reads_the_logical_working_directory(tmp_path, monkeypatch):
    target = tmp_path / "real-target"
    target.mkdir()
    link = tmp_path / "link-name"
    link.symlink_to(target)
    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))
    assert mod._current_project({}) == "link-name"
    monkeypatch.setenv("PWD", str(tmp_path / "elsewhere"))   # a stale PWD is not trusted
    assert mod._current_project({}) == "real-target"


def test_cli_project_prints_the_compose_project(tmp_path, capsys):
    pack = tmp_path / "pack-dir"
    _compose(pack, "name: shared\nservices: {}\n")
    assert mod.main(["project", str(pack)], {}) == 0
    assert capsys.readouterr().out == "shared\n"


@needs_bash
def test_the_verifier_checks_image_provenance_under_the_compose_project(tmp_path):
    """post_deploy_verify derived the project a third way, `${COMPOSE_PROJECT_NAME:-$(basename
    "$PWD")}`, which ignores `name:` and a COMPOSE_PROJECT_NAME set in .env."""
    engine = _stub_verifier_engine(tmp_path)
    (engine / "scripts" / "engine_images.py").unlink()
    shutil.copy(SCRIPT, engine / "scripts" / "engine_images.py")
    (engine / "scripts" / "image_drift.py").unlink()
    (engine / "scripts" / "image_drift.py").write_text(
        "import sys\nopen(sys.argv[sys.argv.index('--project') + 1] + '.asked', 'w').close()\n",
        encoding="utf-8")
    deployment = tmp_path / "deployment"
    _compose(deployment, "name: shared\nservices: {}\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "W1044_KINDS": "[]"}
    env.pop("COMPOSE_PROJECT_NAME", None)
    subprocess.run(["bash", str(engine / "scripts" / "post_deploy_verify.sh")], cwd=deployment,
                   capture_output=True, text=True, timeout=300, env=env)
    assert (deployment / "shared.asked").exists(), sorted(p.name for p in deployment.iterdir())


# --- okengine#1044 [25]: host ports another deployment already publishes are refused pre-compose --
# Every pack defaults to port_offset 0 and the cockpit is reader+1, so offsets 0 and 1 (or two packs
# left at 0, or another stack on 9200) collide. Each pack validated clean alone and compose-preview
# never looked at ports, so the first sign was `port is already allocated` from `compose up` after
# the gateway had been recreated: a half-up deployment. `ports` asks Compose what this deployment
# will publish and docker what other projects publish now, before anything is recreated.

# `docker ps` from a five-pack host (2026-10-12), names neutralized; `other-stack` is a non-engine
# compose project holding the engine's base ports.
LIVE_PS = """\
okcti-test\tokcti-gateway\t
okcti-test\tokcti-mcp\t8730/tcp
okcti-test\tokcti-cockpit\t0.0.0.0:9401->9200/tcp
okcti-test\tokcti-reader\t0.0.0.0:9400->9200/tcp
okpack-market\tmarket-cockpit\t127.0.0.1:9700->9200/tcp
okpack-market\tokpack-market-reader\t127.0.0.1:9600->9200/tcp
okpack-vendor-risk-test\tokpack-vendor-risk-cockpit-o550\t0.0.0.0:9751->9200/tcp
okpack-vendor-risk-test\tokpack-vendor-risk-reader-o550\t0.0.0.0:9750->9200/tcp
other-stack\tother-mcp\t0.0.0.0:8730->8730/tcp, [::]:8730->8730/tcp
other-stack\tother-reader\t0.0.0.0:9200->9200/tcp, [::]:9200->9200/tcp
other-stack\tother-pg\t127.0.0.1:5433->5432/tcp
proxy\tproxy-haproxy\t0.0.0.0:8404->8404/tcp, 8080/tcp, 0.0.0.0:7000-7001->7000-7001/udp
\tunlabeled\t0.0.0.0:6000->6000/tcp
"""


def _config(*ports):
    """`docker compose config --format json` for a deployment publishing `ports`
    (service, host_ip, published, protocol)."""
    services = {}
    for service, host_ip, published, protocol in ports:
        entry = {"mode": "ingress", "target": 9200, "published": published, "protocol": protocol}
        if host_ip is not None:
            entry["host_ip"] = host_ip
        services.setdefault(service, {"image": "x", "ports": []})["ports"].append(entry)
    return json.dumps({"name": "ignored", "services": services})


def _docker(config: str | None, ps: str | None = LIVE_PS):
    asked, cwds = [], []

    def run(args, cwd=None):
        asked.append(args)
        cwds.append(cwd)
        if args[:2] == ["docker", "compose"]:
            return config
        return ps
    run.asked = asked
    run.cwds = cwds
    return run


def _ports(tmp_path, config, ps=LIVE_PS, project="okpack-new"):
    pack = tmp_path / project
    pack.mkdir(exist_ok=True)
    run = _docker(config, ps)
    rc = mod._cmd_ports(type("A", (), {"pack": str(pack)}), {}, run)
    return rc, run


@pytest.mark.parametrize("planned, holder", [
    (("okengine-reader", "127.0.0.1", "9200", "tcp"), "other-reader"),     # offset 0 vs another stack
    (("okengine-reader", None, "9401", "tcp"), "okcti-cockpit"),          # reader at offset 1 vs cockpit
    (("okengine-cockpit", "0.0.0.0", "9600", "tcp"), "okpack-market-reader"),  # wildcard vs loopback
    (("okengine-reader", "127.0.0.1", "8730", "tcp"), "other-mcp"),
    (("svc", None, "7001", "udp"), "proxy-haproxy"),                        # inside a published range
    (("svc", None, "6000", "tcp"), "unlabeled"),                            # no compose project at all
])
def test_a_port_another_deployment_publishes_is_refused(tmp_path, capsys, planned, holder):
    rc, _ = _ports(tmp_path, _config(planned))
    assert rc == 1
    err = capsys.readouterr().err
    assert holder in err and planned[2] in err and "port_offset" in err


@pytest.mark.parametrize("planned", [
    ("okengine-reader", "127.0.0.1", "9500", "tcp"),          # a free port
    ("okengine-reader", "10.1.2.3", "9600", "tcp"),           # same port, different specific address
    ("svc", None, "7000", "tcp"),                             # the udp range does not hold tcp
    ("svc", None, "8080", "tcp"),                             # exposed, not published
    ("svc", None, "", "tcp"),                                 # ephemeral host port
])
def test_a_port_nobody_else_publishes_is_accepted(tmp_path, planned):
    rc, _ = _ports(tmp_path, _config(planned))
    assert rc == 0


def test_a_redeploy_does_not_collide_with_its_own_containers(tmp_path):
    """The deployment's own running reader/cockpit hold its ports until compose recreates them."""
    planned = _config(("okengine-reader", None, "9400", "tcp"), ("okengine-cockpit", None, "9401", "tcp"))
    rc, run = _ports(tmp_path, planned, project="okcti-test")
    assert rc == 0
    assert run.asked[0][:4] == ["docker", "compose", "--project-directory", str(tmp_path / "okcti-test")]


def test_a_deployment_publishing_nothing_is_accepted(tmp_path):
    assert _ports(tmp_path, _config())[0] == 0


@pytest.mark.parametrize("config, ps", [
    (None, LIVE_PS),               # compose config failed (bad compose file, missing variable)
    (_config(("s", None, "1", "tcp")), None),   # docker ps failed
    ("not json", LIVE_PS),
    ("[]", LIVE_PS),
], ids=["config-failed", "ps-failed", "config-not-json", "config-not-an-object"])
def test_unanswerable_port_questions_are_undetectable_not_a_pass(tmp_path, capsys, config, ps):
    rc, _ = _ports(tmp_path, config, ps)
    assert rc == 2
    assert "UNDETECTABLE" in capsys.readouterr().err


def test_ports_cli_dispatch(tmp_path, monkeypatch):
    pack = tmp_path / "okpack-new"
    pack.mkdir()
    monkeypatch.setattr(mod, "_run", _docker(_config(("s", None, "9200", "tcp"))))
    assert mod.main(["ports", str(pack)], {}) == 1
    monkeypatch.setattr(mod, "_run", _docker(_config(("s", None, "9999", "tcp"))))
    assert mod.main(["ports", str(pack)], {}) == 0


def test_a_holder_bound_on_ipv4_and_ipv6_is_reported_once(tmp_path, capsys):
    assert _ports(tmp_path, _config(("okengine-reader", "127.0.0.1", "9200", "tcp")))[0] == 1
    assert capsys.readouterr().err.count("held by other-reader") == 1


# --- okengine#1044 [10]/[11]: no published doc builds engine images around deploy.sh ------------
# The provenance label is declared only in the deploy.sh-generated override and stamped from
# OKENGINE_ENGINE_BUILD_TAG, which only deploy.sh sets. INSTALL.md's "exactly what deploy.sh runs"
# sequence set the variable on a bare `compose up --build` that has no override, and three other
# docs rebuilt with a bare `--build`: every such image is UNATTRIBUTED, and an operator who sees
# that warning on every healthy manual deploy learns to ignore the one meant to catch a real
# rebuild from the wrong ENGINE_DIR.

_DOCS = [REPO / "INSTALL.md", REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]  # glob-ok: published docs tree
#: A command line (optionally behind VAR=value prefixes), not prose that names the command.
_BARE_BUILD = re.compile(r"^\s*(?:[A-Z_]+=\S+\s+)*docker compose\b[^\n`]*\bup\b[^\n`]*--build")


def _doc_lines():
    for doc in _DOCS:
        if doc.is_file():
            for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
                yield doc.relative_to(REPO).as_posix(), number, line


def test_no_doc_sets_the_build_label_variable_by_hand():
    """Setting it by hand would launder a rebuild from any ENGINE_DIR into an attributed image."""
    hits = [f"{doc}:{n}" for doc, n, line in _doc_lines()
            if f"{mod.BUILD_VAR}=" in line]
    assert not hits, f"only deploy.sh may set {mod.BUILD_VAR}: {hits}"


def test_every_documented_bare_compose_build_says_it_is_unlabeled():
    """A doc may show the bare command (INSTALL's step-by-step does) only on a line that says the
    result is unlabeled; anything else must go through deploy.sh."""
    hits = [f"{doc}:{n}: {line.strip()}" for doc, n, line in _doc_lines()
            if _BARE_BUILD.search(line) and "UNLABELED" not in line and "unlabeled" not in line]
    assert not hits, "a doc rebuilds engine images with a bare compose --build:\n" + "\n".join(hits)


def test_the_bare_build_detector_catches_the_shapes_it_claims():
    for line in ("ENGINE_DIR=x docker compose up -d --build",
                 "docker compose --profile review up -d --build",
                 "ENGINE_DIR=$E OKENGINE_ENGINE_BUILD_TAG=$T docker compose up -d --build"):
        assert _BARE_BUILD.search(line), line
    for line in ("docker compose up -d", "docker compose logs -f x", "bash scripts/deploy.sh --rebuild",
                 "prose naming a bare `docker compose up --build`"):
        assert not _BARE_BUILD.search(line), line


def test_install_no_longer_claims_its_manual_sequence_is_exactly_deploy_sh():
    text = (REPO / "INSTALL.md").read_text(encoding="utf-8")
    assert "exactly what `deploy.sh` runs" not in text


# --- mutation-hardening of the port model -------------------------------------------------------

@pytest.mark.parametrize("planned", [
    ("svc", None, "9200", "udp"),               # held as tcp only
    ("svc", "127.0.0.2", "9600", "tcp"),        # a different loopback address than the holder's
    ("svc", None, "8402-8403", "tcp"),          # a range ending just below a held port
    ("svc", None, "7002", "udp"),               # just past a held range
], ids=["other-protocol", "other-specific-address", "range-below", "past-held-range"])
def test_near_misses_are_accepted(tmp_path, planned):
    assert _ports(tmp_path, _config(planned))[0] == 0


@pytest.mark.parametrize("planned", [
    ("svc", "127.0.0.1", "9600", "tcp"),        # the holder's exact specific address
    ("svc", None, "8403-8404", "tcp"),          # a range whose LAST port is held
], ids=["same-specific-address", "range-end-held"])
def test_exact_hits_are_refused(tmp_path, planned):
    assert _ports(tmp_path, _config(planned))[0] == 1


def test_conflict_lines_name_wildcards_and_unlabeled_holders(tmp_path, capsys):
    ps = "\t\t0.0.0.0:6000->6000/tcp\n"
    assert _ports(tmp_path, _config(("svc", None, "6000", "tcp")), ps=ps)[0] == 1
    assert ("svc wants *:6000/tcp, held by <unnamed> (compose project '<none>')"
            in capsys.readouterr().err)


def test_an_ephemeral_port_does_not_hide_a_later_published_one(tmp_path):
    config = json.dumps({"services": {"svc": {"ports": [
        {"target": 1, "published": "", "protocol": "tcp"},
        {"target": 2, "published": "9200", "protocol": "tcp"}]}}})
    assert _ports(tmp_path, config)[0] == 1


def test_a_missing_compose_file_does_not_hide_a_later_files_name(tmp_path):
    pack = tmp_path / "pack-dir"
    _compose(pack, "name: declared\nservices: {}\n")
    assert mod.project_name(pack, {"COMPOSE_FILE": "absent.yml:docker-compose.yml"}) == "declared"


# --- v0.15.14 audit: `ports` reads the PACK's compose model from any caller cwd ------------------
# `--project-directory` does not move where Compose looks for compose files, so `compose config` run
# from the deployer's cwd found none, `ports` exited 2 (UNDETECTABLE) and deploy.sh only warned: the
# documented `bash $ENGINE_DIR/scripts/deploy.sh /path/to/pack` was never port-checked.

_COMPOSE_LIKE_DOCKER = """#!/bin/sh
echo "cwd=$PWD args=$*" >> "{log}"
case "$*" in
  *"config --format json"*)
    [ -f docker-compose.yml ] || {{ echo "no configuration file provided: not found" >&2; exit 1; }}
    echo '{config}' ; exit 0 ;;
  "ps --format"*) printf 'other-stack\\tother-reader\\t0.0.0.0:9200->9200/tcp\\n' ; exit 0 ;;
esac
exit 1
"""


def test_ports_reads_the_packs_compose_model_from_another_cwd(tmp_path):
    """Real invocation: the script as deploy.sh runs it, from a cwd that is not the pack, against
    a docker that (like Compose) only finds compose files in its own cwd."""
    import os
    import subprocess
    pack = tmp_path / "okpack-new"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text(_COMPOSE_LIKE_DOCKER.format(
        log=log, config=_config(("okengine-reader", "127.0.0.1", "9200", "tcp"))), encoding="utf-8")
    docker.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PATH"] = f"{bindir}:/usr/bin:/bin"
    proc = subprocess.run([sys.executable, str(SCRIPT), "ports", str(pack)], cwd=elsewhere,
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, proc.stderr
    assert "held by other-reader" in proc.stderr, proc.stderr
    config_calls = [line for line in log.read_text(encoding="utf-8").splitlines()
                    if "config --format json" in line]
    assert config_calls and all(line.startswith(f"cwd={pack} ") for line in config_calls), config_calls


def test_ports_runs_compose_config_with_the_pack_as_cwd(tmp_path):
    rc, run = _ports(tmp_path, _config())
    assert rc == 0
    assert run.cwds[0] == str(tmp_path / "okpack-new")
    assert run.cwds[1] is None          # `docker ps` is host-wide; it has no project directory
