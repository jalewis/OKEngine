#!/usr/bin/env python3
"""Build the dependency-free OKEngine wheel reproducibly from src/okengine."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.14.4"
DIST_INFO = f"okengine-{VERSION}.dist-info"
STAMP = (2026, 1, 1, 0, 0, 0)
RUNTIME_MEMBERS = {
    "tools/schema_validator.py": "tools/schema_validator.py",
    "tools/policy_plane.py": "tools/policy_plane.py",
    "okengine-mcp/output_contract_enforce.py": "output_contract_enforce.py",
    "okengine-mcp/converge.py": "converge.py",
    "scripts/cron/id_lib.py": "id_lib.py",
    "scripts/cron/schema_lib.py": "schema_lib.py",
    "scripts/cron/id_index.py": "id_index.py",
    "scripts/cron/okf_migrate.py": "okf_migrate.py",
    "config/base-schema.yaml": "okengine/data/base-schema.yaml",
}


def _digest(data: bytes) -> str:
    value = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={value}"


def build(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"okengine-{VERSION}-py3-none-any.whl"
    members = {
        path.relative_to(ROOT / "src").as_posix(): path.read_bytes()
        for path in sorted((ROOT / "src/okengine").rglob("*.py"))
    }
    members.update({target: (ROOT / source).read_bytes()
                    for source, target in RUNTIME_MEMBERS.items()})
    members[f"{DIST_INFO}/METADATA"] = (
        "Metadata-Version: 2.4\nName: okengine\n"
        f"Version: {VERSION}\nSummary: Open Knowledge Engine\n"
        "Requires-Python: >=3.11.4\n\n"
    ).encode()
    members[f"{DIST_INFO}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: okengine-build-wheel\n"
        "Root-Is-Purelib: true\nTag: py3-none-any\n"
    ).encode()
    members[f"{DIST_INFO}/entry_points.txt"] = (
        "[console_scripts]\n"
        "okengine-cron = okengine.cli:cron\n"
        "okengine-framework = okengine.cli:framework\n"
    ).encode()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(members.items()):
        writer.writerow((name, _digest(data), len(data)))
    writer.writerow((f"{DIST_INFO}/RECORD", "", ""))
    members[f"{DIST_INFO}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data, compresslevel=9)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="artifacts/wheel")
    args = parser.parse_args(argv)
    print(build(Path(args.out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
