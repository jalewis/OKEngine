#!/usr/bin/env python3
"""hermes_patch_probe.py — prove the RUNNING gateway carries the engine's Hermes patch series.

okengine#1044 [29]. The carried patches include patch 01, the file-tool vault write guard: the only
thing that keeps Hermes' native file tools from being a weaker second write path around
okengine-write. Every compose-built sidecar is verified by content (image_drift.py), but the gateway
was verified only by build markers that a non-engine image (an upstream hermes-agent tag, a
hand-built image, an old tag left in a pack's compose file) does not have, and deployment_checks
skipped a missing marker on purpose. Post-deploy verification then reported green on a gateway with
none of the guards.

So this checks the code itself. For every patch in the series that apply.sh applies for the
manifest pin, it collects the lines the patch adds to runtime files (not `tests/`), drops any that a
LATER hunk of the series removes again (the series is sequential: a later patch may rewrite a line an
earlier one added), and requires each remaining line to be present in the gateway's /opt/hermes
copy of that file. One `tar` stream is read out of the gateway; nothing is written there.

Lines shorter than eight characters (`)`, `else:`) are skipped: they prove nothing about which code
is there. Lines a patch only REMOVES are not checked.

Exit codes: 0 every patch present, 1 a patch is missing (each named), 2 undetectable (no series for
the pin, or the gateway could not be read): not a pass.

Usage (from the deployment dir, as post_deploy_verify.sh runs it)::

    python3 <engine>/scripts/hermes_patch_probe.py --engine-dir <engine> [--service gateway]
"""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Mapping
from pathlib import Path
from subprocess import SubprocessError
from tarfile import TarError

HERMES_ROOT = "/opt/hermes"
MIN_LINE = 8
_PIN = re.compile(r"^\s*pinned_tag:\s*(\S+)", re.M)

#: (patch file name, runtime file, added line) — one expectation.
Expectation = tuple[str, str, str]
Runner = Callable[[list[str]], "bytes | None"]


def manifest_pin(engine: Path) -> str | None:
    """The Hermes pin apply.sh reads: the FIRST `pinned_tag:` in engine-manifest.yaml."""
    try:
        match = _PIN.search((engine / "engine-manifest.yaml").read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def series_dir(engine: Path, pin: str | None) -> Path | None:
    """The directory apply.sh takes the series from for `pin`: `patches/target-<pin>` when the
    engine carries a ported series for it, else the legacy `patches/` series (which apply.sh
    accepts only for its own pin; tests pin the agreement with apply.sh's case arms)."""
    if not pin:
        return None
    target = engine / "patches" / f"target-{pin}"
    if target.is_dir():
        return target
    legacy = engine / "patches"
    readme = legacy / "README.md"
    try:
        if pin in readme.read_text(encoding="utf-8"):
            return legacy
    except OSError:
        return None
    return None


def _runtime_path(header: str) -> str | None:
    path = header[4:].split("\t")[0].strip()
    if not path.startswith("b/"):
        return None                              # /dev/null: the file is deleted
    path = path[2:]
    return None if path.startswith("tests/") else path


def expectations(series: Path) -> list[Expectation]:
    """Each line the series adds to a runtime file and leaves in place, in series order."""
    steps: list[tuple[str, str, list[str], set[str]]] = []
    current = None
    for patch in sorted(series.glob("*.patch")):  # glob-ok: flat patch-series dir
        current = None
        for line in patch.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("+++ "):
                current = _runtime_path(line)
                if current:
                    steps.append((patch.name, current, [], set()))
                continue
            if current is None or line.startswith(("+++", "---")):
                continue
            text = line[1:].strip()
            if line.startswith("+") and len(text) >= MIN_LINE:
                steps[-1][2].append(text)
            elif line.startswith("-"):
                steps[-1][3].add(text)
    out: list[Expectation] = []
    for index, (name, path, added, _removed) in enumerate(steps):
        gone = set().union(*(removed for _n, other, _a, removed in steps[index + 1:]
                             if other == path))
        out += [(name, path, text) for text in added if text not in gone]
    return out


def missing(expected: list[Expectation], contents: Mapping[str, set[str]]) -> dict[str, list]:
    """patch name -> the (file, line) expectations the tree does not satisfy."""
    out: dict[str, list] = {}
    for name, path, text in expected:
        if text not in contents.get(path, set()):
            out.setdefault(name, []).append((path, text))
    return out


def read_tar(stream: bytes) -> dict[str, set[str]] | None:
    """{relative path: its stripped lines} from a tar stream, or None when it is not one."""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(stream))
    except (TarError, EOFError):
        return None
    out = {}
    with archive:
        for member in archive.getmembers():
            handle = archive.extractfile(member) if member.isfile() else None
            if handle is not None:
                text = handle.read().decode("utf-8", errors="replace")
                out[member.name] = {line.strip() for line in text.splitlines()}
    return out


def _run(args: list[str]) -> bytes | None:
    """stdout of `args`, even on a non-zero exit (GNU tar exits 2 when a requested file is absent,
    and the archive of the rest is the answer); None when nothing could be read."""
    try:
        proc = subprocess.run(args, capture_output=True, timeout=120)
    except (OSError, SubprocessError):
        return None
    return proc.stdout or None


def gateway_contents(service: str, files: list[str], run: Runner = _run) -> dict | None:
    stream = run(["docker", "compose", "exec", "-T", service, "tar", "-C", HERMES_ROOT, "-cf", "-",
                  *files])
    return read_tar(stream) if stream else None


def main(argv: list[str] | None = None, run: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine-dir", required=True)
    parser.add_argument("--service", default="gateway")
    args = parser.parse_args(argv)
    engine = Path(args.engine_dir)
    pin = manifest_pin(engine)
    series = series_dir(engine, pin)
    expected = expectations(series) if series else []
    if not expected:
        print(f"hermes patches: UNDETECTABLE — no patch series found for Hermes pin {pin!r} under "
              f"{engine / 'patches'}", file=sys.stderr)
        return 2
    files = sorted({path for _name, path, _text in expected})
    contents = gateway_contents(args.service, files, _run if run is None else run)
    if contents is None:
        print(f"hermes patches: UNDETECTABLE — cannot read {HERMES_ROOT} from the {args.service} "
              "container (not running, exec denied, or no tar in the image)", file=sys.stderr)
        return 2
    gaps = missing(expected, contents)
    patches = sorted({name for name, _path, _text in expected})
    print(f"hermes patches: {len(patches) - len(gaps)} of {len(patches)} carried patch(es) present "
          f"in {args.service} (pin {pin})")
    for name in sorted(gaps):
        path, text = gaps[name][0]
        total = sum(1 for n, _p, _t in expected if n == name)
        print(f"  MISSING {name}: {len(gaps[name])} of {total} added line(s) absent, e.g. "
              f"{path}: {text[:80]}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
