from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scaffold_validation_uses_module_import_context():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "python -m scripts.framework_validate /tmp/pack --quiet" in workflow
    assert "python scripts/framework_validate.py /tmp/pack --quiet" not in workflow


def test_scaffold_compose_smoke_supplies_disposable_projection_credentials():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "OKENGINE_PROJECTION_WRITER_PASSWORD: ci-only-writer" in workflow
    assert "OKENGINE_PROJECTION_READER_PASSWORD: ci-only-reader" in workflow
