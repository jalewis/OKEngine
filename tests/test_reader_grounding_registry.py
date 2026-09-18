"""okengine#563 (reader half) — the reader's provenance strip must honour the same evidence policy
as `review_autoverify`, for the same reason the cockpit must.

The reader is the LESS severe half of the bug: it has no quarantine gate, so it hid nothing. It
merely labelled a page whose only source was a registry-graded publisher as
"N prose sources — ungrounded", directly contradicting the `auto_verified_basis` on that same page.
The cockpit carried the identical disagreement and additionally BLOCKED the page
(tests/test_cockpit_grounding_registry.py).

Kept as its own module rather than folded into the cockpit's so that a future divergence between the
two surfaces fails on the surface that actually diverged.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("nh3")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-reader" / "app.py"

REGISTRY = (
    "source_registry:\n"
    "  Graded Feed B:\n    reliability: B\n"
    "  Some Wire Service:\n    reliability: A\n"
)


def _load(vault, monkeypatch, registry=REGISTRY):
    (vault / "schema.yaml").write_text(registry, encoding="utf-8")
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("reader_app", None)
    spec = importlib.util.spec_from_file_location("reader_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["reader_app"] = m
    spec.loader.exec_module(m)
    return m


def test_graded_prose_source_is_counted_as_grounded(tmp_path, monkeypatch):
    """The reported shape: the page's only source is a registry-graded publisher label."""
    m = _load(tmp_path, monkeypatch)
    p = m._provenance({"type": "actor", "sources": ["Graded Feed B"]}, "")
    assert p["graded_sources"] == 1, p
    assert p["registry_available"] is True, p


def test_ungraded_prose_source_is_not_counted_as_graded(tmp_path, monkeypatch):
    """The fix must not blanket-clear: a publisher absent from the registry stays ungrounded."""
    m = _load(tmp_path, monkeypatch)
    p = m._provenance({"type": "actor", "sources": ["nobody-graded-this"]}, "")
    assert p["graded_sources"] == 0, p
    assert p["sources"] == 1, p


def test_absent_registry_is_reported_as_unavailable(tmp_path, monkeypatch):
    """An empty registry cannot distinguish graded from ungraded — the strip must be able to say so
    instead of asserting "ungrounded", which would be a verdict the data does not support."""
    m = _load(tmp_path, monkeypatch, registry="types: {}\n")
    p = m._provenance({"type": "actor", "sources": ["Graded Feed B"]}, "")
    assert p["registry_available"] is False, p
    assert p["graded_sources"] == 0, p


def test_source_page_citation_still_counted(tmp_path, monkeypatch):
    """The pre-existing signal is untouched: a real source-page ref still grounds the page."""
    m = _load(tmp_path, monkeypatch)
    p = m._provenance({"type": "actor", "sources": ["sources/news/some-article.md"]}, "")
    assert p["source_pages"] == 1, p


def test_reader_and_cockpit_agree_on_graded_prose(tmp_path, monkeypatch):
    """Cross-surface detector: both readers of `source_registry` must reach the same verdict for the
    same page. They are separate images with separate copies of this logic, so nothing but a test
    keeps them from drifting apart again."""
    cockpit_app = REPO / "okengine-cockpit" / "app.py"
    fm = {"type": "actor", "sources": ["Graded Feed B"]}

    r = _load(tmp_path, monkeypatch)
    reader_graded = r._provenance(fm, "")["graded_sources"]

    (tmp_path / "schema.yaml").write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    sys.path.insert(0, str(cockpit_app.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", cockpit_app)
    c = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = c
    spec.loader.exec_module(c)
    cockpit_graded = c._provenance(fm, "")["graded_sources"]

    assert reader_graded == cockpit_graded == 1, (
        f"reader says {reader_graded} graded source(s), cockpit says {cockpit_graded} — the two "
        f"surfaces disagree about the same page's evidence")
