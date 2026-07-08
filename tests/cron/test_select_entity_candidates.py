"""Regression: the entity-backfill digest must not crash when an entity page
carries a non-string `tags` member (e.g. a bare YAML year `2024`) or a scalar
`tags` value. The write path coerces a scalar STRING but not numeric scalars or
list members (okengine#196), so such a page reaches storage verbatim and the
naive `", ".join(e["tags"][:5])` would raise TypeError, aborting the whole
digest for the entire vault.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "select_entity_candidates.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path, home: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["HERMES_HOME"] = str(home)
    sys.modules.pop("select_entity_candidates", None)
    spec = importlib.util.spec_from_file_location("select_entity_candidates", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write(vault: Path, rel: str, body: str) -> None:
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_non_string_tag_member_does_not_crash_digest(tmp_path, capsys):
    vault = tmp_path / "vault"
    home = tmp_path / "home"
    # a source (so the wake-gate sees new work and the digest is actually emitted)
    _write(vault, "sources/s1.md", "---\ntype: source\npublisher: Acme\n---\n# A source\n")
    # entity with a bare-year int in its tag list AND one with a scalar int tag
    _write(vault, "entities/a/acme.md", "---\ntype: vendor\ntags: [2024, ai-labs]\n---\n# Acme\n")
    _write(vault, "entities/b/beta.md", "---\ntype: vendor\ntags: 2024\n---\n# Beta\n")

    m = _load(vault, home)
    rc = m.main()  # must not raise TypeError
    out = capsys.readouterr().out

    assert rc == 0
    assert "entities/acme" in out
    assert "tags: 2024, ai-labs" in out   # numeric member coerced, not crashed
    assert "entities/beta" in out
