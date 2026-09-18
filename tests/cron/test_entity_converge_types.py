"""Regression: entity_converge must never merge pages of different declared types."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "entity_converge.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="entity_converge absent")


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("entity_converge", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["entity_converge"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_never_merges_pages_of_different_types():
    """A shared name across types is a COLLISION, not an identity.

    Regression: name adjacency alone tombstoned the actor `APT3` as a "Duplicate of" the malware
    `shotput`, and the actor `Fox Kitten` as a duplicate of the malware `pay2key`. Threat-intel
    tooling is routinely named after the group that wields it, so this is the common case, and
    both actors were destroyed — every assessment keyed to them lost its subject.
    """
    clusters = _load().clusters
    records = {
        "entities/a/apt3": {"type": "actor", "title": "APT3",
                            "aliases": ["SHOTPUT", "Gothic Panda"]},
        "entities/s/h/shotput": {"type": "malware", "title": "SHOTPUT",
                                 "aliases": ["APT3", "Backdoor.APT.CookieCutter"]},
    }
    assert clusters(records) == [], "an actor and a malware must never cluster together"


def test_still_merges_same_type_duplicates():
    """The type guard must not disarm converge for the case it exists to handle."""
    clusters = _load().clusters
    records = {
        "entities/a/apt3": {"type": "actor", "title": "APT3",
                            "aliases": ["Gothic Panda", "Buckeye"]},
        "entities/a/apt-3": {"type": "actor", "title": "APT3",
                             "aliases": ["Gothic Panda", "Buckeye"]},
    }
    assert clusters(records) == [["entities/a/apt-3", "entities/a/apt3"]]


def test_undeclared_type_is_unknown_not_a_mismatch():
    """A page with no `type` is judged on identity alone — absence is not disagreement."""
    clusters = _load().clusters
    records = {
        "entities/a/apt3": {"type": "actor", "title": "APT3", "aliases": ["Gothic Panda"]},
        "entities/a/apt-3": {"title": "APT3", "aliases": ["Gothic Panda"]},
    }
    assert clusters(records) == [["entities/a/apt-3", "entities/a/apt3"]]
