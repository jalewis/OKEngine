#!/usr/bin/env python3
"""Build release images once; verify same-pipeline immutable images before reuse."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci import candidate_wheel

#: Services the smoke/e2e/resilience/performance stack runs from the verified override.
SMOKE_SERVICES = ('reader', 'okengine-mcp', 'cockpit', 'review-write', 'responses-gateway')
#: Product images the smoke stack does not run (okengine#1048). They are built here with the rest,
#: by the same recipe and candidate wheel, and each is booted once through its own entrypoint
#: (IMAGE_SMOKE) before its digest is recorded as tested.
BUILD_ONLY = ('projection', 'operation-runner')
SERVICES = SMOKE_SERVICES + BUILD_ONLY
LABEL = 'org.opencontainers.image.revision'
BASE = 'tests/e2e/smoke/docker-compose.smoke.yml'
BUILD_ONLY_FILE = 'tests/e2e/smoke/docker-compose.release-build.yml'
#: (command, health probe) per build-only image. The command runs through the image's own
#: entrypoint with no network; a probe, when present, runs inside the started container.
IMAGE_SMOKE = {
    # No PostgreSQL here: `--help` proves the interpreter, the wheel and every import resolve.
    'projection': (['python', '/app/service.py', '--help'], None),
    # The real CMD (uvicorn), then its own /healthz from inside the container.
    'operation-runner': (None, ['python', '-c',
                                "import urllib.request as u; "
                                "u.urlopen('http://127.0.0.1:8732/healthz', timeout=5).read()"]),
}
SMOKE_ATTEMPTS = 30


def docker(*args: str) -> str:
    return subprocess.check_output(['docker', *args], text=True).strip()


def identity() -> dict:
    return {key: os.environ[key] for key in
            ('CI_COMMIT_SHA', 'CI_PIPELINE_ID', 'CI_PROJECT_ID', 'CI_REGISTRY_IMAGE')}


def validate(manifest: dict, expected: dict) -> None:
    if manifest.get('identity') != expected or manifest.get('schema') != 1:
        raise ValueError('release image provenance does not match this candidate pipeline')
    images = manifest.get('images', {})
    if set(images) != set(SERVICES):
        raise ValueError('release image manifest has missing or unexpected services')
    for service, image in images.items():
        prefix = f"{expected['CI_REGISTRY_IMAGE']}/release/{service}@sha256:"
        if not isinstance(image, str) or not image.startswith(prefix) or not re.fullmatch(
                r'[0-9a-f]{64}', image[len(prefix):]):
            raise ValueError(f'invalid immutable image reference for {service}')


def inspect(image: str, sha: str) -> dict:
    result = json.loads(docker('image', 'inspect', image))[0]
    if result['Config'].get('Labels', {}).get(LABEL) != sha:
        raise ValueError(f'image source revision mismatch: {image}')
    return result


def build(directory: Path, expected: dict, wheel_directory: Path = Path('artifacts/candidate-wheel')) -> dict:
    wheel_context = candidate_wheel.context(wheel_directory, directory)
    tags = {service: f"{expected['CI_REGISTRY_IMAGE']}/release/{service}:"
            f"{expected['CI_PIPELINE_ID']}-{expected['CI_COMMIT_SHA']}" for service in SERVICES}
    override = directory / 'build.json'
    override.write_text(json.dumps({'services': {service: {
        'image': tag, 'build': {
            'labels': {LABEL: expected['CI_COMMIT_SHA']},
            **({'additional_contexts': {'wheel': str(wheel_context)}}
               if service in candidate_wheel.DOCKERFILES else {})
        }
    } for service, tag in tags.items()}}))
    # Paths in the first Compose file remain the source of build-context truth.
    docker('compose', '-f', BASE, '-f', BUILD_ONLY_FILE, '-f', str(override), 'build', *SERVICES)
    for service in BUILD_ONLY:
        smoke_image(service, tags[service])
    images, ids = {}, {}
    for service, tag in tags.items():
        docker('push', tag)
        result = inspect(tag, expected['CI_COMMIT_SHA'])
        repository = tag.rsplit(':', 1)[0]
        matches = [value for value in result['RepoDigests']
                   if value.startswith(repository + '@sha256:')]
        if len(matches) != 1:
            raise ValueError(f'no unique published digest for {service}')
        images[service] = matches[0]
        ids[service] = config_digest(result, service)
    manifest = {'schema': 1, 'identity': expected, 'images': images}
    validate(manifest, expected)
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    write_tested_digests(directory, expected, images, ids)
    return manifest


def config_digest(result: dict, service: str) -> str:
    """The image ID: the config digest, which survives push and pull under the classic store."""
    image_id = result.get('Id')
    if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise ValueError(f'no config digest (image ID) for {service}')
    return image_id


def write_tested_digests(directory: Path, expected: dict, images: dict, ids: dict) -> None:
    """okengine#1048: the record scripts/tested_digests.py checks running containers against
    (post_deploy_verify.sh, OKENGINE_TESTED_DIGESTS). Every image the release lanes test is here,
    by its pushed manifest digest and its config digest."""
    record = {'schema': 1, 'project_id': expected['CI_PROJECT_ID'],
              'pipeline_id': expected['CI_PIPELINE_ID'], 'commit_sha': expected['CI_COMMIT_SHA'],
              'images': {service: {'image': images[service], 'id': ids[service]}
                         for service in images}}
    (directory / 'tested-digests.json').write_text(json.dumps(record, indent=2) + '\n')


def smoke_image(service: str, image: str) -> None:
    """Boot a build-only image once, with no network; ValueError when it does not come up."""
    command, probe = IMAGE_SMOKE[service]
    if probe is None:
        docker('run', '--rm', '--network', 'none', image, *command)
        return
    container = docker('run', '-d', '--network', 'none', '--tmpfs', '/opt/vault', image)
    try:
        for _ in range(SMOKE_ATTEMPTS):
            try:
                docker('exec', container, *probe)
                return
            except subprocess.CalledProcessError:
                time.sleep(1)
        raise ValueError(f'{service} did not become healthy: {docker("logs", container)[-2000:]}')
    finally:
        docker('rm', '-f', container)


def consume(directory: Path, expected: dict) -> dict:
    manifest = json.loads((directory / 'manifest.json').read_text())
    validate(manifest, expected)
    # Only the images the smoke stack runs: the override must never name a service the smoke
    # compose lacks, or Compose would create it.
    smoke = {service: manifest['images'][service] for service in SMOKE_SERVICES}
    for image in smoke.values():
        docker('pull', image)
        result = inspect(image, expected['CI_COMMIT_SHA'])
        if image not in result['RepoDigests']:
            raise ValueError(f'pulled image digest mismatch: {image}')
    (directory / 'verified.json').write_text(json.dumps({'services': {
        service: {'image': image, 'pull_policy': 'never'} for service, image in smoke.items()}}))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('build', 'consume'))
    parser.add_argument('--directory', type=Path, default=Path('artifacts/release-images'))
    parser.add_argument('--wheel-directory', type=Path, default=Path('artifacts/candidate-wheel'))
    args = parser.parse_args(argv)
    args.directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    expected = identity()
    if args.mode == 'build':
        build(args.directory, expected, args.wheel_directory)
    else:
        consume(args.directory, expected)
    evidence = {'mode': args.mode, 'elapsed_seconds': time.monotonic() - started,
                'identity': expected, 'job_id': os.environ['CI_JOB_ID'],
                'cache_state': 'fresh per-job Docker daemon'}
    (args.directory / f'{args.mode}-timing.json').write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
