"""Immutable release-image handoff rejects stale and missing evidence before startup."""
import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import create_autospec

import pytest

SPEC = importlib.util.spec_from_file_location('release_images', Path(__file__).parents[2] / 'ci/release_images.py')
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)
ENV = dict(CI_COMMIT_SHA='a' * 40, CI_PIPELINE_ID='123', CI_PROJECT_ID='24',
           CI_REGISTRY_IMAGE='registry.invalid/project')


def manifest():
    return dict(schema=1, identity=ENV.copy(), images={
        service: f"{ENV['CI_REGISTRY_IMAGE']}/release/{service}@sha256:" + 'b' * 64
        for service in mod.SERVICES})


@pytest.mark.parametrize('change', [
    lambda m: m.update(schema=2),
    lambda m: m['identity'].update(CI_PIPELINE_ID='old'),
    lambda m: m['identity'].update(CI_COMMIT_SHA='c' * 40),
    lambda m: m['identity'].update(CI_PROJECT_ID='other'),
    lambda m: m['images'].pop('reader'),
    lambda m: m['images'].update(extra='invalid'),
    lambda m: m['images'].update(reader=None),
    lambda m: m['images'].update(reader='registry.invalid/other@sha256:' + 'b' * 64),
    lambda m: m['images'].update(reader=m['images']['reader'][:-1]),
])
def test_invalid_provenance_rejected(change):
    data = manifest()
    change(data)
    with pytest.raises(ValueError):
        mod.validate(data, ENV)


def boundary(monkeypatch, *, wrong_label=False, wrong_digest=False, duplicate=False, image_id='d' * 64):
    def execute(command, text):
        assert text is True
        if command[1:3] == ['image', 'inspect']:
            image = command[-1]
            repo = image.split('@')[0] if '@' in image else image.rsplit(':', 1)[0]
            digest = repo + '@sha256:' + ('c' if wrong_digest else 'b') * 64
            digests = [digest, digest] if duplicate else [digest]
            result = {'Config': {'Labels': {mod.LABEL: 'old' if wrong_label else ENV['CI_COMMIT_SHA']}},
                      'RepoDigests': digests}
            if image_id is not None:
                result['Id'] = 'sha256:' + image_id
            return json.dumps([result])
        return ''
    call = create_autospec(subprocess.check_output, side_effect=execute)
    monkeypatch.setattr(mod.subprocess, 'check_output', call)
    return call


def test_build_and_reuse_exact_images_without_rebuilding(tmp_path, monkeypatch):
    call = boundary(monkeypatch)
    for key, value in dict(ENV, CI_JOB_ID='42').items():
        monkeypatch.setenv(key, value)
    wheel_directory = tmp_path / 'candidate-wheel'
    mod.candidate_wheel.build(wheel_directory)
    assert mod.main(['build', '--directory', str(tmp_path), '--wheel-directory', str(wheel_directory)]) == 0
    override = json.loads((tmp_path / 'build.json').read_text())
    assert 'additional_contexts' not in override['services']['responses-gateway']['build']
    for name in ('reader', 'cockpit', 'okengine-mcp', 'review-write', 'projection', 'operation-runner'):
        context = Path(override['services'][name]['build']['additional_contexts']['wheel'])
        assert len(list((context / 'wheel').glob('*.whl'))) == 1
    built = json.loads((tmp_path / 'manifest.json').read_text())
    assert built == manifest()
    assert json.loads((tmp_path / 'build-timing.json').read_text())['elapsed_seconds'] >= 0
    call.reset_mock()
    assert mod.main(['consume', '--directory', str(tmp_path)]) == 0
    commands = [c.args[0] for c in call.call_args_list]
    assert len(commands) == 10
    assert all(c[1] in ('pull', 'image') for c in commands)
    override = json.loads((tmp_path / 'verified.json').read_text())
    # okengine#1048: only the smoke stack's services; a build-only service named in the override
    # would make Compose create it.
    assert {k: v['image'] for k, v in override['services'].items()} == {
        service: built['images'][service] for service in mod.SMOKE_SERVICES}
    assert all(v['pull_policy'] == 'never' for v in override['services'].values())


@pytest.mark.parametrize('failure', ['missing', 'malformed', 'stale', 'label', 'digest', 'pull'])
def test_consumer_fails_without_verified_override(tmp_path, monkeypatch, failure):
    call = boundary(monkeypatch, wrong_label=failure == 'label', wrong_digest=failure == 'digest')
    data = manifest()
    if failure == 'stale':
        data['identity']['CI_COMMIT_SHA'] = 'old'
    if failure != 'missing':
        (tmp_path / 'manifest.json').write_text('{' if failure == 'malformed' else json.dumps(data))
    if failure == 'pull':
        call.side_effect = subprocess.CalledProcessError(1, ['docker', 'pull'])
    with pytest.raises((ValueError, FileNotFoundError, subprocess.CalledProcessError)):
        mod.consume(tmp_path, ENV)
    assert not (tmp_path / 'verified.json').exists()
    if failure in ('missing', 'malformed', 'stale'):
        call.assert_not_called()


def test_builder_requires_unique_published_digest(tmp_path, monkeypatch):
    boundary(monkeypatch, duplicate=True)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    wheel_directory = tmp_path / 'candidate-wheel'
    mod.candidate_wheel.build(wheel_directory)
    with pytest.raises(ValueError, match='unique published digest'):
        mod.build(tmp_path, ENV, wheel_directory)
    assert not (tmp_path / 'manifest.json').exists()


TESTED_SPEC = importlib.util.spec_from_file_location(
    'tested_digests', Path(__file__).parents[2] / 'scripts/tested_digests.py')
tested_digests = importlib.util.module_from_spec(TESTED_SPEC)
TESTED_SPEC.loader.exec_module(tested_digests)


def test_build_records_every_tested_digest_in_the_list_the_deploy_verifier_reads(tmp_path, monkeypatch):
    """okengine#1048: the producer and the post-deploy consumer agree on one format."""
    boundary(monkeypatch)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    wheel_directory = tmp_path / 'candidate-wheel'
    mod.candidate_wheel.build(wheel_directory)
    mod.build(tmp_path, ENV, wheel_directory)
    record = json.loads((tmp_path / 'tested-digests.json').read_text())
    assert record['pipeline_id'] == ENV['CI_PIPELINE_ID']
    assert record['commit_sha'] == ENV['CI_COMMIT_SHA']
    assert record['project_id'] == ENV['CI_PROJECT_ID']
    assert set(record['images']) == set(mod.SERVICES)
    for service, entry in record['images'].items():
        assert entry == {'image': manifest()['images'][service], 'id': 'sha256:' + 'd' * 64}
    tested = tested_digests.load(tmp_path / 'tested-digests.json')
    assert tested.ids == {'sha256:' + 'd' * 64}
    assert tested.digests == {'sha256:' + 'b' * 64}
    assert tested.pipeline_id == ENV['CI_PIPELINE_ID']


@pytest.mark.parametrize('image_id', [None, 'D' * 64, 'd' * 63])
def test_build_refuses_an_image_without_a_config_digest(tmp_path, monkeypatch, image_id):
    boundary(monkeypatch, image_id=image_id)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    wheel_directory = tmp_path / 'candidate-wheel'
    mod.candidate_wheel.build(wheel_directory)
    with pytest.raises(ValueError, match='config digest'):
        mod.build(tmp_path, ENV, wheel_directory)
    assert not (tmp_path / 'tested-digests.json').exists()
    assert not (tmp_path / 'manifest.json').exists()


def test_build_only_images_are_built_with_the_release_and_booted_before_they_are_recorded(
        tmp_path, monkeypatch):
    """okengine#1048: projection and operation-runner are built by the same recipe and wheel,
    from the build-only compose file, and each runs through its own entrypoint first."""
    call = boundary(monkeypatch)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    wheel_directory = tmp_path / 'candidate-wheel'
    mod.candidate_wheel.build(wheel_directory)
    mod.build(tmp_path, ENV, wheel_directory)
    commands = [c.args[0] for c in call.call_args_list]
    compose = next(c for c in commands if c[1] == 'compose')
    assert compose[2:8] == ['-f', mod.BASE, '-f', mod.BUILD_ONLY_FILE, '-f', str(tmp_path / 'build.json')]
    assert set(compose[9:]) == set(mod.SERVICES)
    projection = f"{ENV['CI_REGISTRY_IMAGE']}/release/projection:123-{'a' * 40}"
    assert ['docker', 'run', '--rm', '--network', 'none', projection,
            'python', '/app/service.py', '--help'] in commands
    started = [c for c in commands if c[1:3] == ['run', '-d']]
    assert len(started) == 1 and started[0][-1].endswith(f"/release/operation-runner:123-{'a' * 40}")
    assert any(c[1] == 'exec' for c in commands) and any(c[1:3] == ['rm', '-f'] for c in commands)
    first_push = next(i for i, c in enumerate(commands) if c[1] == 'push')
    last_smoke = max(i for i, c in enumerate(commands) if c[1] in ('run', 'exec', 'rm'))
    assert last_smoke < first_push, 'an image must boot before any digest is published'


def test_a_build_only_image_that_never_becomes_healthy_fails_the_build(tmp_path, monkeypatch):
    call = boundary(monkeypatch)
    original = call.side_effect

    def unhealthy(command, text):
        if command[1] == 'exec':
            raise subprocess.CalledProcessError(1, command)
        if command[1] == 'logs':
            return 'boom'
        return original(command, text)

    call.side_effect = unhealthy
    monkeypatch.setattr(mod.time, 'sleep', lambda _: None)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    wheel_directory = tmp_path / 'candidate-wheel'
    mod.candidate_wheel.build(wheel_directory)
    with pytest.raises(ValueError, match='operation-runner did not become healthy: boom'):
        mod.build(tmp_path, ENV, wheel_directory)
    commands = [c.args[0] for c in call.call_args_list]
    assert sum(c[1] == 'exec' for c in commands) == mod.SMOKE_ATTEMPTS
    assert commands[-1][1:3] == ['rm', '-f'], 'the smoke container is always removed'
    assert not any(c[1] == 'push' for c in commands)
    assert not (tmp_path / 'tested-digests.json').exists()


def test_the_build_only_compose_file_builds_exactly_the_build_only_services():
    import yaml
    doc = yaml.safe_load((Path(__file__).parents[2] / mod.BUILD_ONLY_FILE).read_text())
    assert set(doc['services']) == set(mod.BUILD_ONLY) == set(mod.IMAGE_SMOKE)
    for service, spec in doc['services'].items():
        assert spec['build']['dockerfile'] == mod.candidate_wheel.DOCKERFILES[service]
        assert spec['image'].startswith('okengine-smoke-'), 'okengine#1033: never a live image name'
