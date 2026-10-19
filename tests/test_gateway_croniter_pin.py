"""The gateway image's scheduler croniter is pinned by the engine and agrees with CI's locks.

Hermes's frozen ``uv.lock`` (pin v2026.9.14) installs croniter 6.0.0, which shifts next-fire by an
hour across DST: fall-back fires an hour late, spring-forward fires an hour early and then again on
time. CI and the host lock 6.2.4, so every DST test passed against a croniter the gateway never ran.
``build-engine-image.sh`` therefore installs a vendored, checksum-verified croniter 6.2.4 into the
Hermes venv after Hermes is built (an engine-side addition: Hermes and its lock stay unmodified)
and runs the behavioral DST self-test in the same layer, so an image whose croniter regresses does
not build.

These tests pin that the block exists, that its version matches every CI lock and the manifest
record, and run the block itself as the build runs it, including a tampered-wheel negative fixture.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SH = REPO / "scripts" / "build-engine-image.sh"
LOCKS = sorted((REPO / "ci" / "tooling").glob("*.requirements.lock"))
BEGIN = "# --- BEGIN scheduler croniter override ---"
END = "# --- END scheduler croniter override ---"
SELFTEST_IN_IMAGE = "/opt/hermes/scripts/cron/croniter_dst_selftest.py"

needs_bash = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sha256sum"),
                                reason="bash/sha256sum not available")


def _block() -> str:
    text = SH.read_text(encoding="utf-8")
    assert BEGIN in text and END in text, "build-engine-image.sh no longer pins the gateway croniter"
    return text[text.index(BEGIN):text.index(END) + len(END)]


def _pin() -> tuple[str, str, Path]:
    block = _block()
    wheel = re.search(r'CRONITER_WHEEL="\$ENGINE_DIR/(vendor/python-build/croniter-([0-9.]+)-py3-none-any\.whl)"',
                      block)
    digest = re.search(r'CRONITER_SHA256="([0-9a-f]{64})"', block)
    assert wheel and digest, "the croniter override must name a vendored wheel and its sha256"
    return wheel.group(2), digest.group(1), REPO / wheel.group(1)


def lock_drift(pinned: str, locks: dict[str, str]) -> tuple[list[str], int]:
    """(lock files whose croniter pin differs from the image's, how many locks pin croniter). A lock
    without croniter (a lane that does not test scheduling) is not drift."""
    drift, pinning = [], 0
    for name, text in sorted(locks.items()):
        m = re.search(r"^croniter==(\S+)\s*$", text, re.MULTILINE)
        if m:
            pinning += 1
            if m.group(1) != pinned:
                drift.append(f"{name}: {m.group(1)}")
    return drift, pinning


def _public_snapshot_tree() -> bool:
    """The public snapshot excludes ci/tooling/ and publish-snapshot.sh itself; the source tree
    always carries the script, so its locks can never be skipped there."""
    return not (REPO / "scripts" / "publish-snapshot.sh").is_file()


def test_the_image_pins_the_same_croniter_as_every_ci_lock():
    version, _digest, _wheel = _pin()
    if not LOCKS and _public_snapshot_tree():
        pytest.skip("ci/tooling/ is publish-excluded and absent from this snapshot")
    assert LOCKS, "ci/tooling/*.requirements.lock not found"
    drift, pinning = lock_drift(version, {p.name: p.read_text(encoding="utf-8") for p in LOCKS})
    assert pinning >= 4, f"only {pinning} CI lock(s) pin croniter; the comparison lost its subject"
    assert not drift, (f"gateway image pins croniter {version} but CI tests against {drift}; "
                       "bump the vendored wheel and the locks together")


def test_lock_drift_detects_a_mismatch():
    """Negative fixture for the comparison above."""
    assert lock_drift("6.2.4", {"a.lock": "croniter==6.2.4\n", "b.lock": "x==1\ncroniter==6.0.0\n",
                                "c.lock": "pytz==1\n"}) == (["b.lock: 6.0.0"], 2)


def test_the_vendored_wheel_is_the_pinned_version_with_the_pinned_digest():
    version, digest, wheel = _pin()
    assert wheel.is_file(), f"{wheel} is not vendored"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == digest
    with zipfile.ZipFile(wheel) as zf:
        meta = zf.read(f"croniter-{version}.dist-info/METADATA").decode()
    assert f"\nVersion: {version}\n" in meta
    # --no-deps is only safe while 6.2.4 needs nothing Hermes's lock does not already install.
    requires = sorted(line.split(":", 1)[1].strip() for line in meta.splitlines()
                      if line.startswith("Requires-Dist:"))
    assert requires == ["python-dateutil"], requires


def test_the_manifest_records_the_override():
    version, _digest, _wheel = _pin()
    manifest = yaml.safe_load((REPO / "engine-manifest.yaml").read_text(encoding="utf-8"))
    override = manifest["runtime"]["dependency_overrides"]["croniter"]
    assert override["version"] == version
    assert override["hermes_lock"] == "6.0.0"
    assert override["wheel"] == f"vendor/python-build/croniter-{version}-py3-none-any.whl"
    assert "DST" in override["reason"]


def test_the_override_is_installed_after_hermes_and_before_the_build():
    text = SH.read_text(encoding="utf-8")
    block = text.index(BEGIN)
    assert text.index('scripts/build_engine_wheel.py') < block, \
        "install the override after the engine wheel, i.e. after Hermes's own venv sync"
    assert block < text.index('if [ "${SKIP_BUILD:-0}" = "1" ]'), \
        "SKIP_BUILD (CI's assembled-tree inspection) must see the override"


def _run_block(engine: Path, work: Path) -> subprocess.CompletedProcess:
    """Execute the sentinel block exactly as the build script does (set -euo pipefail)."""
    script = "set -euo pipefail\n" + _block() + "\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ["PATH"], "ENGINE_DIR": str(engine),
                               "WORK": str(work)})


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "hermes"
    work.mkdir()
    (work / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return work


@needs_bash
def test_the_block_bakes_the_wheel_and_a_build_time_selftest(tmp_path):
    version, _digest, _wheel = _pin()
    work = _work(tmp_path)
    proc = _run_block(REPO, work)
    assert proc.returncode == 0, proc.stderr
    name = f"croniter-{version}-py3-none-any.whl"
    assert (work / ".okengine-build" / name).is_file()
    runs = [line for line in (work / "Dockerfile").read_text().splitlines() if line.startswith("RUN")]
    assert len(runs) == 1, runs
    run = runs[0]
    assert f"--no-deps --python /opt/hermes/.venv/bin/python /opt/hermes/.okengine-build/{name}" in run
    # The self-test runs in the SAME layer, under the interpreter cron-plus runs in: an image whose
    # croniter computes DST wrong fails to build.
    assert run.endswith(f"&& /opt/hermes/.venv/bin/python -I {SELFTEST_IN_IMAGE}")
    assert (REPO / "scripts" / "cron" / "croniter_dst_selftest.py").is_file()


@needs_bash
def test_a_tampered_wheel_stops_the_build(tmp_path):
    """Negative fixture: a vendored wheel that does not match the pinned digest is refused."""
    version, _digest, wheel = _pin()
    engine = tmp_path / "engine"
    (engine / "vendor" / "python-build").mkdir(parents=True)
    tampered = engine / "vendor" / "python-build" / wheel.name
    tampered.write_bytes(wheel.read_bytes() + b"\0")
    work = _work(tmp_path)
    proc = _run_block(engine, work)
    assert proc.returncode != 0
    assert "croniter wheel checksum mismatch" in proc.stderr
    assert (work / "Dockerfile").read_text() == "FROM scratch\n", "nothing may be appended"
    assert not (work / ".okengine-build" / f"croniter-{version}-py3-none-any.whl").exists()
