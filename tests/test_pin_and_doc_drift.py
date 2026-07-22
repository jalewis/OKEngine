"""invariant-audit v0.11.5 batch-7 + okengine#209 — bind version/pin/count CONSTANTS that must agree
across scaffolders, docs, and the manifest to a single source of truth (engine-manifest.yaml /
patches/), so a bump that misses one goes RED instead of drifting silently."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "engine-manifest.yaml"


def _manifest_scalar(key: str) -> str:
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"\s*{re.escape(key)}:\s*(\S+)", line)
        if m:
            return m.group(1).strip()
    raise AssertionError(f"{key} not found in engine-manifest.yaml")


def test_new_pack_sh_reads_hermes_pin_from_manifest():  # invariant-audit #5/#43
    sh = (REPO / "templates" / "pack" / "new-pack.sh").read_text()
    assert "pinned_tag:" in sh and "engine-manifest.yaml" in sh, \
        "new-pack.sh must read the Hermes pin from the manifest, not a literal"
    assert 'HERMES_PIN="v2026' not in sh, "new-pack.sh still hardcodes a Hermes pin literal"


def test_apply_sh_reads_pin_from_manifest():  # invariant-audit #6
    sh = (REPO / "patches" / "apply.sh").read_text()
    assert "pinned_tag:" in sh and "engine-manifest.yaml" in sh
    assert 'PIN="v2026' not in sh, "apply.sh still hardcodes a Hermes pin literal"


def test_install_and_readme_hermes_pin_agree_with_manifest():  # invariant-audit #6
    pin = _manifest_scalar("pinned_tag")
    install = (REPO / "INSTALL.md").read_text()
    assert f"git checkout {pin}" in install, f"INSTALL.md checkout pin != manifest {pin}"
    readme = (REPO / "patches" / "README.md").read_text()
    assert pin in readme, f"patches/README.md pin != manifest {pin}"


def test_patch_count_literals_match_the_actual_patch_set():  # invariant-audit #42/#49
    n = len(list((REPO / "patches").glob("*.patch")))
    for rel in ("INSTALL.md", "engine-manifest.yaml"):
        text = (REPO / rel).read_text()
        assert re.search(rf"(?<!\d){n} (?:core-file|carried) patch", text), \
            f"{rel} does not state the actual patch count ({n})"
    # no STALE wrong-count phrasings survive
    for rel in ("INSTALL.md", "engine-manifest.yaml"):
        text = (REPO / rel).read_text()
        for wrong in (c for c in range(1, 20) if c != n):
            assert not re.search(rf"(?<!\d){wrong} (?:core-file|carried) patch", text), \
                f"{rel} carries a stale patch count ({wrong}, actual {n})"


def test_pyproject_version_matches_engine_release():  # invariant-audit #41
    release = _manifest_scalar("engine_release").lstrip("v")
    pp = (REPO / "pyproject.toml").read_text()
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', pp, re.MULTILINE)
    assert m, "pyproject.toml has no [project] version"
    assert m.group(1) == release, f"pyproject version {m.group(1)} != engine_release {release}"


def test_no_obsolete_engine_checkout_in_current_operator_docs():  # okengine#209 P0.1
    """The dangerous 'git checkout v0.2.0' (9 series stale) must not survive in current operator
    guides — following it installs an engine missing composition/bundle/extension/deploy behavior."""
    for rel in ("docs/deploy-a-new-domain.md", "docs/okf/guide-1-agent-wiki-pattern.md",
                "docs/okf/guide-2-building-an-agent-vault.md"):
        text = (REPO / rel).read_text()
        assert "checkout v0.2.0" not in text, f"{rel} still tells the operator to check out v0.2.0"
        assert "engine release `v0.2.0`" not in text, f"{rel} still declares itself normative for v0.2.0"
