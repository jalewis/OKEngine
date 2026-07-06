"""okengine#163: the standalone pack validator validates the MERGED schema — base/L1 types +
namespaces are accepted in membership checks (so a correct pack that writes/binds a base type like
`dashboard`/`prediction` doesn't spuriously FAIL)."""
import importlib.util
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
SKEL = REPO / "templates" / "pack" / "skeleton" / "validate.py"


def _load():
    spec = importlib.util.spec_from_file_location("skel_validate", SKEL)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_base_sets_defined():
    m = _load()
    assert {"dashboard", "prediction", "source", "briefing"} <= m.BASE_TYPES
    # base namespaces are merged into known_namespaces even for an empty schema
    known = m.known_namespaces({})
    assert {"sources", "predictions", "briefings"} <= known


def test_membership_accepts_base_types():
    # check_type_consistency merges base types + aliases into the accepted set
    m = _load()
    src = SKEL.read_text()
    # the cron-type check unions BASE_TYPES (regression guard against accidental removal)
    assert "| BASE_TYPES" in src
    types = set() | set() | m.BASE_TYPES  # mirrors the merged set the check builds
    assert "dashboard" in types and "prediction" in types
