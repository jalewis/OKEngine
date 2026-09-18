"""Cross-Dockerfile supply-chain consistency (invariant-audit #11/#21).

The three image-rebuild surfaces — okengine-mcp, okengine-reader, okengine-cockpit — share a
base image and the IWE binary, and docs/supply-chain.md promises both are digest/sha pinned
"across the image set". Cockpit was added later and silently shipped WITHOUT the base-digest pin
or the IWE sha256 check, while a grep of the version pin looked consistent (the gap is an ABSENCE
of two lines, not a wrong value). These lock the set together so a new/edited Dockerfile can't
drift off the pinned base or skip the integrity check.
"""
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DOCKERFILES = [
    REPO / "okengine-mcp" / "Dockerfile",
    REPO / "okengine-reader" / "Dockerfile",
    REPO / "okengine-cockpit" / "Dockerfile",
]

_FROM_DIGEST = re.compile(
    r"^ARG\s+PYTHON_BASE_IMAGE=python:3\.13-slim-trixie@(sha256:[0-9a-f]{64})$", re.M)
_IWE_SHA = re.compile(r"ARG\s+IWE_SHA256=([0-9a-f]{64})")

MAKEFILE = REPO / "Makefile"


def _smoke_recipe() -> str:
    """Return the `docker-smoke` target's recipe (the tab-indented command lines) as one string."""
    lines, out, in_target = MAKEFILE.read_text().splitlines(), [], False
    for ln in lines:
        if ln.startswith("docker-smoke:"):
            in_target = True
            continue
        if in_target:
            if ln.startswith("\t"):
                out.append(ln.strip())
            elif ln.strip():  # first non-blank, non-tab line ends the recipe
                break
    return "\n".join(out)


def test_docker_smoke_builds_mcp_from_repo_root():
    """okengine#55 regression: the mcp Dockerfile COPYs the shared scripts/cron/kb_* wrappers (repo-
    root-relative), so ANY build of it — including `make docker-smoke` — MUST use the repo root as
    build context (`-f okengine-mcp/Dockerfile ... .`). A bare `docker build ... okengine-mcp`
    (subdir context) fails to resolve those COPYs, silently breaking the smoke gate. This pins the
    Makefile recipe to the Dockerfile's actual COPY reach so the two can't drift apart again."""
    mcp_df = (REPO / "okengine-mcp" / "Dockerfile").read_text()
    # Precondition: the mcp image really does COPY from outside its own dir. If this stops being
    # true, the context requirement is gone and this guard should be revisited (fail loudly).
    assert re.search(r"^COPY\s+scripts/", mcp_df, re.M), \
        "mcp Dockerfile no longer COPYs scripts/ from the repo root — revisit the docker-smoke context guard"
    recipe = _smoke_recipe()
    assert "-f okengine-mcp/Dockerfile" in recipe and re.search(r"okengine-mcp:smoke\s+\.\s*$", recipe, re.M), \
        "docker-smoke must build the mcp image from the repo root: `-f okengine-mcp/Dockerfile -t okengine-mcp:smoke .`"
    assert not re.search(r"\bokengine-mcp:smoke\s+okengine-mcp\b", recipe), \
        "docker-smoke builds mcp with the okengine-mcp/ subdir as context — its repo-root COPYs will not resolve"


def test_mcp_image_bakes_composed_tier_dependencies():
    """tier_lib's composed-schema path must be reachable in the shipped MCP image."""
    dockerfile = (REPO / "okengine-mcp" / "Dockerfile").read_text()
    assert "scripts/cron/schema_lib.py" in dockerfile
    assert "config/base-schema.yaml ./config/base-schema.yaml" in dockerfile


@pytest.mark.parametrize("df", DOCKERFILES, ids=lambda p: p.parent.name)
def test_base_image_is_digest_pinned(df):
    """#21: every image Dockerfile must pin the base to a sha256 digest (not the floating tag),
    or its build isn't reproducible against the rest of the set."""
    text = df.read_text()
    m = _FROM_DIGEST.search(text)
    assert m, f"{df.parent.name}/Dockerfile does not digest-pin python:3.13-slim-trixie (floating tag = unreproducible)"
    assert "FROM ${PYTHON_BASE_IMAGE}" in text, (
        f"{df.parent.name}/Dockerfile declares the pin but does not build from it")


@pytest.mark.parametrize("df", DOCKERFILES, ids=lambda p: p.parent.name)
def test_iwe_download_is_sha_verified(df):
    """#11: every Dockerfile that downloads IWE must verify it with sha256sum -c, or a swapped
    upstream tarball bakes an unverified binary."""
    t = df.read_text()
    if "iwe.tgz" not in t:
        pytest.skip(f"{df.parent.name}/Dockerfile does not download IWE")
    assert _IWE_SHA.search(t), f"{df.parent.name}/Dockerfile downloads IWE with no ARG IWE_SHA256"
    assert "sha256sum -c" in t, f"{df.parent.name}/Dockerfile downloads IWE with no sha256sum -c verification"


def test_base_digest_agrees_across_the_image_set():
    """All three images must build from the SAME base digest (supply-chain.md promises this) — a
    split base means the 'reproducible image set' guarantee is a lie."""
    digests = {}
    for df in DOCKERFILES:
        m = _FROM_DIGEST.search(df.read_text())
        if m:
            digests[df.parent.name] = m.group(1)
    assert len(set(digests.values())) == 1, f"image set builds from divergent base digests: {digests}"


def test_release_compose_passes_the_ci_base_override_to_every_python_build():
    compose = yaml.safe_load(
        (REPO / "tests/e2e/smoke/docker-compose.smoke.yml").read_text(encoding="utf-8"))
    missing = []
    for name, service in compose["services"].items():
        build = service.get("build")
        if not isinstance(build, dict):
            continue
        dockerfile = REPO / build["dockerfile"]
        if "PYTHON_BASE_IMAGE" not in dockerfile.read_text(encoding="utf-8"):
            continue
        value = (build.get("args") or {}).get("PYTHON_BASE_IMAGE", "")
        if not value.startswith("${OKENGINE_PYTHON_BASE_IMAGE:-") or "@sha256:" not in value:
            missing.append(name)
    assert not missing, f"release services missing the digest-pinned CI base override: {missing}"


# ── Node: official pinned image, never Debian's npm ─────────────────────────────────────────────
#
# okengine-mcp and okengine-cockpit used to `apt-get install nodejs npm`. Debian packages each of
# npm's JavaScript dependencies as a separate .deb, so that one line pulled 340 node-* packages --
# measured 446 apt packages / 187s (mcp) and 544 / 83s (cockpit), against 77 / 67s and 168 / 43s
# with Node copied from the official image. It also installed Node v20.19.2, below the v22 that
# @tobilu/qmd@2.5.3 declares it needs. These pin the replacement so the regression cannot return
# as an innocent-looking package name in an apt line.

NODE_DOCKERFILES = [
    REPO / "okengine-mcp" / "Dockerfile",
    REPO / "okengine-cockpit" / "Dockerfile",
]
_NODE_PIN = re.compile(r"^ARG\s+NODE_BASE_IMAGE=node@(sha256:[0-9a-f]{64})$", re.M)


def _apt_install_packages(text: str) -> set[str]:
    """Every package named in an `apt-get install` inside a RUN, with comments and backslash
    continuations resolved first -- a package on its own continued line, or a comment that merely
    MENTIONS a package, must be read correctly either way."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    joined = re.sub(r"\\\s*\n", " ", code)
    packages: set[str] = set()
    for match in re.finditer(r"apt-get\s+install\b([^&;|\n]*)", joined):
        packages.update(tok for tok in match.group(1).split() if not tok.startswith("-"))
    return packages


@pytest.mark.parametrize("df", NODE_DOCKERFILES, ids=lambda p: p.parent.name)
def test_node_comes_from_a_digest_pinned_official_image(df):
    text = df.read_text()
    assert _NODE_PIN.search(text), (
        f"{df.parent.name}/Dockerfile does not digest-pin NODE_BASE_IMAGE to an official node image")
    assert "FROM ${NODE_BASE_IMAGE} AS node" in text, (
        f"{df.parent.name}/Dockerfile declares the Node pin but never builds a stage from it")
    assert "COPY --from=node /usr/local/bin/node" in text, (
        f"{df.parent.name}/Dockerfile pins Node but does not copy the binary out of that stage")


@pytest.mark.parametrize("df", sorted(set(NODE_DOCKERFILES + DOCKERFILES)), ids=lambda p: p.parent.name)
def test_no_image_installs_debian_nodejs_or_npm(df):
    """THE REGRESSION. Re-adding either name reinstates hundreds of packages and an old Node."""
    offenders = _apt_install_packages(df.read_text()) & {"nodejs", "npm"}
    assert not offenders, (
        f"{df.parent.name}/Dockerfile apt-installs {sorted(offenders)}. Debian's npm pulls 340 node-* "
        "packages and a Node older than qmd supports -- copy Node from NODE_BASE_IMAGE instead.")


def test_the_apt_package_reader_is_not_fooled():
    """Guards the guard. A reader that only saw single-line installs, or that counted a comment,
    would let the test above pass while the package was really being installed."""
    continued = "RUN apt-get update && apt-get install -y --no-install-recommends \\\n    ripgrep npm \\\n    && rm -rf x\n"
    assert "npm" in _apt_install_packages(continued), "missed a package on a continued line"
    commented = "# we used to apt-get install nodejs npm here\nRUN apt-get install -y ripgrep\n"
    assert _apt_install_packages(commented) == {"ripgrep"}, "counted a package named only in a comment"


def test_node_digest_agrees_across_the_images_that_use_it():
    """Two images on different Node builds would make 'the release image set' two different runtimes."""
    digests = {df.parent.name: m.group(1) for df in NODE_DOCKERFILES if (m := _NODE_PIN.search(df.read_text()))}
    assert len(digests) == len(NODE_DOCKERFILES), f"not every Node image pins: {digests}"
    assert len(set(digests.values())) == 1, f"Node images build from divergent digests: {digests}"


def test_release_compose_passes_the_ci_node_override_to_every_node_build():
    """Release CI points OKENGINE_NODE_BASE_IMAGE at the project-registry mirror. A Node build that
    did not receive it would silently fall back to pulling from Docker Hub inside dind -- the exact
    dependency the mirror removes."""
    compose = yaml.safe_load(
        (REPO / "tests/e2e/smoke/docker-compose.smoke.yml").read_text(encoding="utf-8"))
    missing = []
    for name, service in compose["services"].items():
        build = service.get("build")
        if not isinstance(build, dict):
            continue
        if "NODE_BASE_IMAGE" not in (REPO / build["dockerfile"]).read_text(encoding="utf-8"):
            continue
        value = (build.get("args") or {}).get("NODE_BASE_IMAGE", "")
        if not value.startswith("${OKENGINE_NODE_BASE_IMAGE:-") or "@sha256:" not in value:
            missing.append(name)
    assert not missing, f"Node builds missing the digest-pinned CI override: {missing}"


def test_runtime_images_do_not_download_retired_iwe():
    offenders = [str(df.relative_to(REPO)) for df in DOCKERFILES
                 if "iwe-org/iwe/releases" in df.read_text()]
    assert not offenders, f"runtime images still download retired IWE: {offenders}"
