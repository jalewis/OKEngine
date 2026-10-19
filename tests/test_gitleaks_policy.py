"""Provenance exceptions must not exempt other paths, fields, or secret formats."""
import hashlib
from pathlib import Path
import re
import tomllib

import pytest


FINGERPRINT = hashlib.sha256(b"disposable provenance fixture").hexdigest()


@pytest.mark.parametrize("path,line,allowed", [
    ("ci/tooling/lock.json", f'    "key": "{FINGERPRINT}",', True),
    ("artifacts/ci-tooling-runtime.json", f'  "key": "{FINGERPRINT}"', True),
    ("./ci/tooling/lock.json", f'"key": "{FINGERPRINT}"', True),
    ("nested/ci/tooling/lock.json", f'"key": "{FINGERPRINT}"', False),
    ("config.json", f'"key": "{FINGERPRINT}"', False),
    ("ci/tooling/lock.json.backup", f'"key": "{FINGERPRINT}"', False),
    ("other-ci/tooling/lock.json", f'"key": "{FINGERPRINT}"', False),
    ("ci/tooling/lock.json", f'"api_key": "{FINGERPRINT}"', False),
    ("ci/tooling/lock.json", f'"key": "{FINGERPRINT[:-1]}"', False),
    ("ci/tooling/lock.json", f'"key": "{FINGERPRINT[:-1]}X"', False),
    ("ci/tooling/lock.json", f'"key": "{FINGERPRINT}", "api_key": "{FINGERPRINT}"', False),
])
def test_provenance_exception_requires_exact_path_and_hash_line(path, line, allowed):
    config = tomllib.loads((Path(__file__).parents[1] / ".gitleaks.toml").read_text())
    rule, = [r for r in config["rules"] if r["id"] == "generic-api-key"]
    exception, = rule["allowlists"]
    assert exception["condition"] == "AND"
    assert exception["regexTarget"] == "line"
    assert not exception.get("commits") and not exception.get("stopwords")
    path_matches = any(re.search(pattern, path) for pattern in exception["paths"])
    line_matches = any(re.search(pattern, line) for pattern in exception["regexes"])
    assert bool(path_matches and line_matches) is allowed
