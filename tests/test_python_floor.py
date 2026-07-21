"""The declared Python floor must cover APIs used by runtime tooling."""
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_python_floor_covers_tarfile_data_filter():
    """tarfile.extractall(filter='data') is only available from Python 3.11.4."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11.4"' in pyproject
