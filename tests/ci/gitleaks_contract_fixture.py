"""Exercise the real pinned scanner across provenance and credential boundaries."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
binary = str(Path(sys.argv[1]).resolve())
output = Path(sys.argv[2])
entry = json.loads((ROOT / "ci/tooling/lock.json").read_text())["pg313"]["provenance"].copy()
fingerprint = entry.pop("key")
assert fingerprint == hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
fixture_key = base64.b64encode(hashlib.sha256(b"disposable scanner credential fixture").digest()).decode()
cases = [
    ("provenance_at_10k", "ci/tooling/lock.json", f'"key": "{fingerprint}"', 9942, 0),
    ("provenance_at_100k", "ci/tooling/lock.json", f'"key": "{fingerprint}"', 99942, 0),
    ("hash_outside_provenance", "config.json", f'"key": "{fingerprint}"', 9942, 1),
    ("other_field_in_provenance", "ci/tooling/lock.json", f'"api_key": "{fingerprint}"', 9942, 1),
    ("credential_in_provenance", "ci/tooling/lock.json", f'"key": "{fixture_key}"', 9942, 1),
    ("credential_at_100k", "config.json", f'"api_key": "{fixture_key}"', 99942, 1),
    ("extra_credential_on_hash_line", "ci/tooling/lock.json",
     f'"key": "{fingerprint}", "api_key": "{fixture_key}"', 9942, 1),
]
results = []
with tempfile.TemporaryDirectory() as temporary:
    base = Path(temporary)
    for name, relative, line, padding, expected in cases:
        tree = base / name
        path = tree / relative
        path.parent.mkdir(parents=True)
        path.write_text("\n" * padding + "      " + line + "\n")
        report = base / (name + ".json")
        completed = subprocess.run(
            [binary, "dir", ".", "--config", str(ROOT / ".gitleaks.toml"),
             "--no-banner", "--redact", "--exit-code", "1", "--report-format", "json",
             "--report-path", str(report)], cwd=tree, capture_output=True, text=True, timeout=60)
        findings = json.loads(report.read_text())
        result = {"case": name, "exit_code": completed.returncode, "expected": expected,
                  "findings": len(findings)}
        results.append(result)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(results, indent=2) + "\n")
        assert completed.returncode == expected, result
        assert bool(findings) == bool(expected), result
        assert all(item["Secret"] == "REDACTED" for item in findings), "report must redact fixtures"
print(json.dumps({"gitleaks_contract": "passed", "cases": len(results)}))
