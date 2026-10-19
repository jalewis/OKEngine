"""No test may replace the global ``sqlite3.connect`` (okengine#900).

The full suite records per-test coverage contexts (``--cov-context=test``) for coverage-guided
mutation selection. coverage.py then writes its data through ``sqlite3`` at every test-phase
boundary, so a test that swaps the shared ``sqlite3.connect`` breaks coverage's own write and
errors EVERY later test in the shard (measured: an INTERNALERROR on
tests/cron/test_reap_stale_sessions.py). Patch the module-under-test's reference instead.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_GLOBAL_CONNECT_PATCH = re.compile(
    r"""(?:setattr|patch\.object)\(\s*(?:[\w.]+\.)?sqlite3\s*,\s*["']connect["']"""
    r"""|patch\(\s*["']sqlite3\.connect["']"""
    r"""|\bsqlite3\.connect\s*=(?!=)""")


def global_connect_patches(text: str) -> list[int]:
    """Line numbers of ``text`` that replace the process-wide ``sqlite3.connect``."""
    return [number for number, line in enumerate(text.splitlines(), 1)
            if _GLOBAL_CONNECT_PATCH.search(line)]


def test_no_test_replaces_the_global_sqlite3_connect():
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for path in sorted((REPO / "tests").rglob("*.py"))
        if path.resolve() != Path(__file__).resolve()
        for line in global_connect_patches(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "these tests replace the global sqlite3.connect, which coverage.py needs to record "
        "per-test contexts; patch the module under test's sqlite3 reference instead: "
        + ", ".join(offenders))


def test_the_detector_rejects_every_global_patch_form():
    """Negative fixture: the forms that broke coverage, and the forms that must stay legal."""
    for bad in ('monkeypatch.setattr(m.sqlite3, "connect", fake)',
                "monkeypatch.setattr(sqlite3, 'connect', fake)",
                'mock.patch.object(sqlite3, "connect")',
                'with patch("sqlite3.connect"):',
                "sqlite3.connect = fake"):
        assert global_connect_patches(bad) == [1], bad
    for good in ('monkeypatch.setattr(m, "sqlite3", isolated)',
                 "conn = sqlite3.connect(path)",
                 "assert sqlite3.connect == original"):
        assert global_connect_patches(good) == [], good
