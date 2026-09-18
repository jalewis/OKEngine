"""entity-backfill digest must be O(selected sources), never O(vault) — okengine#476.

The digest used to inline EVERY entity page so the model would not create duplicates. On a mature
vault that is the whole prompt (8,994 entities ≈ 116k tokens on okcti, 94% of the message) and it
overflows any context smaller than the entity list — silently, because the serving layer truncates
and the lane still returns a well-formed receipt. These tests pin the growth curve, not a literal
byte count: a digest whose size tracks vault size is the defect, whatever the constant.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cron" / "select_entity_candidates.py"


def _load(monkeypatch, vault: Path, home: Path):
    """Import the selector with WIKI_PATH/HERMES_HOME bound to a scratch vault."""
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("HERMES_HOME", str(home))
    # the selector imports siblings (selection_manifest, offpeak) by bare name, as it does at
    # runtime where cron-plus runs it from scripts/cron itself
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location(f"sel_{vault.name}", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _entity(slug: str, title: str) -> dict:
    return {"filename": f"{slug}.md", "slug": slug, "type": "actor", "tags": [], "title": title}


def test_unrelated_entities_are_not_emitted(monkeypatch, tmp_path):
    """An entity sharing no naming with the sources must not reach the prompt."""
    mod = _load(monkeypatch, tmp_path / "vault", tmp_path / "home")
    existing = [_entity(f"unrelated-actor-{i}", f"Unrelated Actor {i}") for i in range(500)]
    existing.append(_entity("lazarus-group", "Lazarus Group"))
    related = mod.related_entities(existing, ["A report on the Lazarus Group and its tooling."])
    slugs = {e["slug"] for e in related}
    assert "lazarus-group" in slugs, "an entity named in the sources must be surfaced"
    assert not any(s.startswith("unrelated-actor-") for s in slugs), (
        "entities unrelated to the selection leaked into the digest — this is the O(vault) defect"
    )


@pytest.mark.parametrize("vault_size", [10, 1_000, 50_000])
def test_related_set_does_not_grow_with_vault(monkeypatch, tmp_path, vault_size):
    """The join's output is bounded by the SELECTION, not by how big the wiki gets.

    This is the actual invariant. A vault 5,000x larger must not produce a larger digest when the
    same sources are being reconciled.
    """
    mod = _load(monkeypatch, tmp_path / "vault", tmp_path / "home")
    existing = [_entity(f"filler-entity-{i}", f"Filler Entity {i}") for i in range(vault_size)]
    existing.append(_entity("volt-typhoon", "Volt Typhoon"))
    related = mod.related_entities(existing, ["Volt Typhoon activity against critical infrastructure."])
    assert [e["slug"] for e in related] == ["volt-typhoon"], (
        f"digest content changed with vault size ({vault_size}) — growth must come from the "
        "selected sources only"
    )


def test_empty_selection_yields_nothing(monkeypatch, tmp_path):
    """No sources selected -> no entity hints. Never fall back to dumping the vault."""
    mod = _load(monkeypatch, tmp_path / "vault", tmp_path / "home")
    existing = [_entity(f"e-{i}", f"Entity {i}") for i in range(100)]
    assert mod.related_entities(existing, []) == []
    assert mod.related_entities(existing, ["", "   "]) == []


def test_generic_words_do_not_match_everything(monkeypatch, tmp_path):
    """Stopwords must not make every page 'related' — that reintroduces the O(vault) dump."""
    mod = _load(monkeypatch, tmp_path / "vault", tmp_path / "home")
    existing = [_entity(f"threat-actor-{i}", f"Threat Actor {i}") for i in range(200)]
    related = mod.related_entities(existing, ["A new threat actor report on security data."])
    assert len(related) < len(existing), (
        "generic vocabulary matched the whole vault — the stopword guard is not holding"
    )


def test_cap_is_declared_and_finite(monkeypatch, tmp_path):
    """A hard ceiling must exist so a pathological join can still not blow the window."""
    mod = _load(monkeypatch, tmp_path / "vault", tmp_path / "home")
    assert isinstance(mod.MAX_RELATED_ENTITIES, int)
    assert 0 < mod.MAX_RELATED_ENTITIES <= 1000, (
        "the related-entity cap must stay small enough to fit a modest context window"
    )
