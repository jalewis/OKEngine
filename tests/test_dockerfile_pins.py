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

REPO = Path(__file__).resolve().parent.parent
DOCKERFILES = [
    REPO / "okengine-mcp" / "Dockerfile",
    REPO / "okengine-reader" / "Dockerfile",
    REPO / "okengine-cockpit" / "Dockerfile",
]

_FROM_DIGEST = re.compile(r"^FROM\s+python:3\.13-slim-trixie@(sha256:[0-9a-f]{64})", re.M)
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


@pytest.mark.parametrize("df", DOCKERFILES, ids=lambda p: p.parent.name)
def test_base_image_is_digest_pinned(df):
    """#21: every image Dockerfile must pin the base to a sha256 digest (not the floating tag),
    or its build isn't reproducible against the rest of the set."""
    m = _FROM_DIGEST.search(df.read_text())
    assert m, f"{df.parent.name}/Dockerfile does not digest-pin python:3.13-slim-trixie (floating tag = unreproducible)"


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


def test_iwe_sha_agrees_across_the_image_set():
    shas = {}
    for df in DOCKERFILES:
        t = df.read_text()
        m = _IWE_SHA.search(t)
        if m:
            shas[df.parent.name] = m.group(1)
    assert len(set(shas.values())) == 1, f"image set verifies IWE against divergent shas: {shas}"
