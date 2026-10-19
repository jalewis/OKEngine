#!/usr/bin/env python3
"""Create and verify one attributable engine wheel for a candidate pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_engine_wheel as builder


DOCKERFILES = {
    'reader': 'okengine-reader/Dockerfile',
    'cockpit': 'okengine-cockpit/Dockerfile',
    'okengine-mcp': 'okengine-mcp/Dockerfile',
    'review-write': 'okengine-mcp/Dockerfile.review',
    'projection': 'okengine-projection/Dockerfile',
    'operation-runner': 'okengine-operations/Dockerfile',
}
WHEEL_STAGE = (
    'FROM ${PYTHON_BASE_IMAGE} AS wheel',
    'WORKDIR /build',
    'COPY src/ ./src/',
    'COPY tools/schema_validator.py tools/policy_plane.py ./tools/',
    'COPY okengine-mcp/output_contract_enforce.py okengine-mcp/converge.py ./okengine-mcp/',
    'COPY scripts/cron/id_lib.py scripts/cron/schema_lib.py scripts/cron/id_index.py scripts/cron/okf_migrate.py ./scripts/cron/',
    'COPY scripts/build_engine_wheel.py ./scripts/build_engine_wheel.py',
    'COPY config/base-schema.yaml ./config/base-schema.yaml',
    'RUN python scripts/build_engine_wheel.py --out /wheel',
)


def verify_stages(root: Path) -> None:
    """Never hide a changed runtime package recipe behind a named-context override."""
    copied = {name for line in WHEEL_STAGE if line.startswith('COPY ')
              for name in line.split()[1:-1]}
    if set(builder.RUNTIME_MEMBERS) - copied:
        raise ValueError('runtime wheel stages do not copy every declared package input')
    for name in DOCKERFILES.values():
        lines = [' '.join(line.split()) for line in (root / name).read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        start = lines.index(WHEEL_STAGE[0])
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith('FROM '))
        if tuple(lines[start:end]) != WHEEL_STAGE:
            raise ValueError(f'runtime wheel recipe changed: {name}; qualify the new build semantics')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs() -> dict[str, str]:
    names = set(builder.RUNTIME_MEMBERS) | {'scripts/build_engine_wheel.py'}
    names.update(p.relative_to(ROOT).as_posix() for p in (ROOT / 'src/okengine').rglob('*.py'))
    return {name: digest(ROOT / name) for name in sorted(names)}


def identity() -> dict[str, str]:
    return {name: os.environ[name] for name in ('CI_COMMIT_SHA', 'CI_PROJECT_ID', 'CI_PIPELINE_ID')}


def build(directory: Path) -> Path:
    wheel = builder.build(directory)
    manifest = {'schema': 1, 'identity': identity(), 'inputs': inputs(),
                'filename': wheel.name, 'sha256': digest(wheel), 'builder_python': platform.python_version()}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return wheel


def verify(directory: Path) -> Path:
    manifest = json.loads((directory / 'manifest.json').read_text())
    if (manifest.get('schema') != 1 or manifest.get('identity') != identity()
            or manifest.get('inputs') != inputs()):
        raise ValueError('candidate wheel provenance does not match source inputs or pipeline')
    filename = manifest.get('filename')
    if (not isinstance(filename, str) or Path(filename).name != filename
            or not filename.endswith('-py3-none-any.whl')):
        raise ValueError('candidate wheel filename is invalid')
    wheel = directory / filename
    if digest(wheel) != manifest.get('sha256'):
        raise ValueError('candidate wheel digest mismatch')
    return wheel


def context(directory: Path, output: Path) -> Path:
    """Expose only the verified wheel at /wheel for BuildKit's named stage override."""
    wheel = verify(directory)
    verify_stages(ROOT)
    target = Path(tempfile.mkdtemp(prefix='wheel-context-', dir=output)).resolve()
    (target / 'wheel').mkdir()
    expected = json.loads((directory / 'manifest.json').read_text())['sha256']
    shutil.copyfile(wheel, target / 'wheel' / wheel.name)
    if digest(target / 'wheel' / wheel.name) != expected:
        raise ValueError('candidate wheel changed while preparing the build context')
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('build', 'verify'))
    parser.add_argument('--directory', type=Path, default=Path('artifacts/candidate-wheel'))
    args = parser.parse_args(argv)
    operation = build if args.mode == 'build' else verify
    print(operation(args.directory))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
