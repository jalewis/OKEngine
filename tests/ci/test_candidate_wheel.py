"""Real wheel handoff rejects missing, stale, and corrupted candidate artifacts."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('candidate_wheel', Path(__file__).parents[2] / 'ci/candidate_wheel.py')
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


@pytest.fixture
def wheel(tmp_path, monkeypatch):
    for key, value in dict(CI_COMMIT_SHA='a' * 40, CI_PROJECT_ID='24', CI_PIPELINE_ID='123').items():
        monkeypatch.setenv(key, value)
    directory = tmp_path / 'wheel'
    assert mod.main(['build', '--directory', str(directory)]) == 0
    return directory


def test_actual_wheel_round_trip(wheel, capsys):
    assert mod.main(['verify', '--directory', str(wheel)]) == 0
    path = Path(capsys.readouterr().out.splitlines()[-1])
    assert path.is_file()
    import zipfile
    with zipfile.ZipFile(path) as archive:
        assert 'okengine/__init__.py' in archive.namelist()
        assert 'tools/schema_validator.py' in archive.namelist()


@pytest.mark.parametrize('fault', ['missing-manifest', 'missing-wheel', 'corrupt-wheel', 'source',
                                  'pipeline', 'project', 'inputs', 'schema', 'filename', 'path', 'tag'])
def test_invalid_wheel_artifacts_fail_closed(wheel, fault):
    path = wheel / 'manifest.json'
    data = json.loads(path.read_text())
    if fault == 'missing-manifest':
        path.unlink()
    elif fault == 'missing-wheel':
        (wheel / data['filename']).unlink()
    elif fault == 'corrupt-wheel':
        (wheel / data['filename']).write_bytes(b'corrupt')
    else:
        if fault in ('source', 'pipeline', 'project'):
            field = {'source': 'CI_COMMIT_SHA', 'pipeline': 'CI_PIPELINE_ID', 'project': 'CI_PROJECT_ID'}[fault]
            data['identity'][field] = 'other'
        elif fault == 'inputs':
            data['inputs']['scripts/build_engine_wheel.py'] = 'other'
        elif fault == 'schema':
            data['schema'] = 2
        else:
            data['filename'] = {'filename': None, 'path': '../outside-py3-none-any.whl',
                                'tag': 'okengine-platform.whl'}[fault]
        path.write_text(json.dumps(data))
    with pytest.raises((ValueError, FileNotFoundError)):
        mod.verify(wheel)


def test_context_contains_only_verified_wheel(wheel, tmp_path):
    result = mod.context(wheel, tmp_path)
    assert list(result.iterdir()) == [result / 'wheel']
    files = list((result / 'wheel').iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == mod.verify(wheel).read_bytes()


def test_context_rejects_copy_corruption(wheel, tmp_path, monkeypatch):
    # Copy is a filesystem boundary, not the wheel validation behavior under test.
    from unittest.mock import create_autospec
    def corrupted_copy(source, destination):
        Path(destination).write_bytes(b'corrupt')
    monkeypatch.setattr(mod.shutil, 'copyfile', create_autospec(mod.shutil.copyfile, side_effect=corrupted_copy))
    with pytest.raises(ValueError, match='changed while preparing'):
        mod.context(wheel, tmp_path)


def test_engine_source_change_invalidates_wheel(wheel, tmp_path, monkeypatch):
    import shutil
    source = tmp_path / 'changed-source'
    for name in mod.inputs():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mod.ROOT / name, target)
    changed = source / 'src/okengine/__init__.py'
    changed.write_text(changed.read_text() + '\n# changed source input\n')
    monkeypatch.setattr(mod, 'ROOT', source)
    with pytest.raises(ValueError, match='source inputs'):
        mod.verify(wheel)


def test_runtime_wheel_recipes_match_the_shared_builder():
    mod.verify_stages(mod.ROOT)


@pytest.mark.parametrize('change', ['RUN python', 'COPY src/', 'WORKDIR /build', 'AS wheel'])
def test_changed_runtime_recipe_cannot_be_hidden_by_shared_wheel(tmp_path, change):
    for name in mod.DOCKERFILES.values():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((mod.ROOT / name).read_text())
    target = tmp_path / mod.DOCKERFILES['reader']
    target.write_text(target.read_text().replace(change, change + '-changed'))
    with pytest.raises(ValueError):
        mod.verify_stages(tmp_path)


def test_new_package_input_requires_matching_runtime_copy_recipe(monkeypatch):
    monkeypatch.setitem(mod.builder.RUNTIME_MEMBERS, 'new-input.py', 'new-input.py')
    with pytest.raises(ValueError, match='every declared package input'):
        mod.verify_stages(mod.ROOT)


def test_builder_uses_only_the_declared_disposable_inputs(tmp_path, monkeypatch):
    import shutil
    source = tmp_path / 'isolated-source'
    for name in mod.inputs():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mod.ROOT / name, target)
    original = mod.builder.build(tmp_path / 'original')
    monkeypatch.setattr(mod.builder, 'ROOT', source)
    isolated = mod.builder.build(tmp_path / 'isolated')
    assert isolated.read_bytes() == original.read_bytes()
