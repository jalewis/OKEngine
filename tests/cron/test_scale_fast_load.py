"""Scale hardening (okengine#74): schema_lib.fast_load (libyaml) parses frontmatter; the bench
harness generates a vault. The audits use fast_load on the per-page hot path."""
import importlib.util, sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m


def test_fast_load():
    sl = _load("schema_lib", "scripts/cron/schema_lib.py")
    assert sl.fast_load("type: entity\nname: X\nsources:\n- sources/a/b\n") == {
        "type": "entity", "name": "X", "sources": ["sources/a/b"]}
    assert sl.fast_load("") is None


def test_audits_use_fast_load():
    for f in ("conformance_audit", "grounding_audit", "review_queue"):
        assert "fast_load" in (REPO / "scripts/cron" / f"{f}.py").read_text(), f


def test_bench_generates(tmp_path):
    b = _load("bench_vault", "scripts/bench_vault.py")
    b.gen_vault(tmp_path, 50)
    assert sum(1 for _ in (tmp_path / "wiki").rglob("*.md")) >= 50
