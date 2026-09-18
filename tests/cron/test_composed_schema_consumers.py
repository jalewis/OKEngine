"""Cron consumers must use schema_lib so extension-composed policy is visible."""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"
_RAW_READ = re.compile(
    r"schema\.yaml[^\n]{0,100}read_(?:text|bytes)|read_(?:text|bytes)[^\n]{0,100}schema\.yaml"
)


def _raw_schema_reads(source: str) -> list[str]:
    return [line.strip() for line in source.splitlines() if _RAW_READ.search(line)]


def test_detector_rejects_direct_raw_schema_read():
    defect = 'schema = yaml.safe_load((vault / "schema.yaml").read_text())'
    assert _raw_schema_reads(defect) == [defect]


def test_cron_modules_do_not_bypass_composed_schema():
    offenders = {
        path.relative_to(REPO).as_posix(): _raw_schema_reads(path.read_text(encoding="utf-8"))
        for path in CRON.glob("*.py")
        if _raw_schema_reads(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, (
        "cron code reads raw schema.yaml and therefore ignores enabled extension composition; "
        "use schema_lib.merged_schema(), governing_schema(), or a schema_lib accessor"
    )
