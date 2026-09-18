"""okengine#563 — the cockpit's grounding gate must honour the SAME evidence policy as
`review_autoverify`, because the two surfaces independently decide whether a page is publishable.

The gap: `review_autoverify` publishes an actor on registry-graded PROSE evidence (1xA or 2xB
from the pack Admiralty `source_registry`), clearing `needs_review`. The cockpit re-derived its
own verdict and counted only citations resolving to a source PAGE, so a page whose only source was a
graded publisher LABEL still rendered "ungrounded" -> blocking -> "Unverified draft — quarantined".
On one live CTI deployment that hid 861 of the 1120 actor pages the lane had auto-verified:
published by policy, quarantined by render.

This is the multi-surface-contract class: nothing enforced the agreement, so the surfaces drifted.
The last test here is the standing detector — it asserts the invariant directly rather than any one
symptom, so a future change to either definition of "grounded" fails here.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-cockpit" / "app.py"

# mirrors the shape review_autoverify grades against: {name: {reliability: A-F}}
REGISTRY = (
    "source_registry:\n"
    "  Graded Feed B:\n    reliability: B\n"
    "  Some Wire Service:\n    reliability: A\n"
)


def _load(vault, monkeypatch, registry=REGISTRY):
    (vault / "schema.yaml").write_text(registry, encoding="utf-8")
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = m
    spec.loader.exec_module(m)
    return m


def _badges(m, fm, ptype="actor"):
    return m._quality_badges(fm, "", ptype, m._provenance(fm, ""), [])


def _labels(badges):
    return [str(b.get("label")) for b in badges]


def test_registry_graded_prose_source_is_not_ungrounded(tmp_path, monkeypatch):
    """The reported shape: one prose source, graded B by the registry, no source page."""
    m = _load(tmp_path, monkeypatch)
    b = _badges(m, {"type": "actor", "sources": ["Graded Feed B"]})
    assert "ungrounded" not in _labels(b), _labels(b)
    assert "1 graded source" in _labels(b), _labels(b)


def test_graded_source_badge_does_not_quarantine_the_page(tmp_path, monkeypatch):
    """The badge is the mechanism, the trust gate is the SYMPTOM the user saw — assert the banner
    itself is gone, not merely that one label changed."""
    m = _load(tmp_path, monkeypatch)
    fm = {"type": "actor", "sources": ["Graded Feed B"], "review_status": "auto-verified"}
    st = m._page_trust_state("entities/a/n/some-actor.md", fm, _badges(m, fm))
    assert st["state"] == "verified", st
    assert st["reasons"] == [], st


def test_ungraded_prose_source_still_quarantines(tmp_path, monkeypatch):
    """The fix must not blanket-clear: a publisher absent from the registry is still ungrounded, and
    review_autoverify would not have published it either (no grade -> no derived confidence)."""
    m = _load(tmp_path, monkeypatch)
    fm = {"type": "actor", "sources": ["some-random-slug-nobody-graded"]}
    b = _badges(m, fm)
    assert "ungrounded" in _labels(b), _labels(b)
    assert m._page_trust_state("entities/x.md", fm, b)["state"] == "quarantined"


def test_no_sources_at_all_is_still_bad(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert "no sources" in _labels(_badges(m, {"type": "actor"}))


def test_absent_registry_reports_undetectable_not_ungrounded(tmp_path, monkeypatch):
    """An empty/missing source_registry cannot tell graded from ungraded. Blocking every page on it
    would be a vacuous FAIL that mislabels sourced pages as unsourced, so the surface must say the
    check is undetectable — the repo's "missing key = WARN, never a vacuous verdict" rule, which
    review_autoverify already applies for the same missing key."""
    m = _load(tmp_path, monkeypatch, registry="types: {}\n")
    fm = {"type": "actor", "sources": ["Graded Feed B"]}
    b = _badges(m, fm)
    assert "grounding unverifiable" in _labels(b), _labels(b)
    assert "ungrounded" not in _labels(b), _labels(b)
    assert m._page_trust_state("entities/x.md", fm, b)["state"] == "verified"


def test_bare_url_source_is_ungrounded_even_without_a_registry(tmp_path, monkeypatch):
    """The undetectable escape must not swallow a verdict we can actually reach: registry keys are
    PUBLISHER NAMES, so a bare URL can never be graded and is ungrounded whether or not the registry
    loaded. Guards the Gentlemen regression (test_cockpit_panels) against this change."""
    m = _load(tmp_path, monkeypatch, registry="types: {}\n")
    fm = {"type": "actor", "sources": ["https://example.invalid/rumor"]}
    b = _badges(m, fm)
    assert "ungrounded" in _labels(b), _labels(b)
    assert "grounding unverifiable" not in _labels(b), _labels(b)
    assert m._page_trust_state("entities/g/x.md", fm, b)["state"] == "quarantined"


def test_source_page_citation_still_grounds_without_the_registry(tmp_path, monkeypatch):
    """The pre-existing path must be untouched: a real source-page ref grounds a page on its own."""
    m = _load(tmp_path, monkeypatch, registry="types: {}\n")
    b = _badges(m, {"type": "actor", "sources": ["sources/news/some-article.md"]})
    assert "ungrounded" not in _labels(b) and "grounding unverifiable" not in _labels(b), _labels(b)


# --- the standing detector -----------------------------------------------------------------------
def test_autoverify_publishable_evidence_is_never_quarantined_by_the_cockpit(tmp_path, monkeypatch):
    """THE INVARIANT: any evidence shape review_autoverify publishes on must render un-quarantined.

    Asserted against review_autoverify's real band table rather than a restatement of it, so if
    that policy widens (new band or floor) and the cockpit is not taught to match, this fails.
    """
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    sys.modules.pop("review_autoverify", None)
    spec = importlib.util.spec_from_file_location(
        "review_autoverify", REPO / "scripts" / "cron" / "review_autoverify.py")
    rav = importlib.util.module_from_spec(spec)
    sys.modules["review_autoverify"] = rav
    spec.loader.exec_module(rav)

    # one distinctly-named registry publisher per grade the bands require, so `consensus` (which
    # counts distinct publishers) is satisfiable at each floor
    needed = sorted({(grade, floor) for grade, floor, _ in rav._BANDS})
    reg_lines, cases = ["source_registry:"], []
    for grade, floor in needed:
        names = [f"Pub {grade}{i}" for i in range(floor)]
        reg_lines += [f"  {n}:\n    reliability: {grade}" for n in names]
        cases.append((grade, floor, names))
    m = _load(tmp_path, monkeypatch, registry="\n".join(reg_lines) + "\n")

    for grade, floor, names in cases:
        graded = {grade: list(names)}
        assert rav._derive_confidence(graded, rav._consensus(graded)) is not None, (
            f"fixture drift: {floor}x{grade} should be publishable per _BANDS")
        fm = {"type": "actor", "sources": list(names), "review_status": "auto-verified"}
        st = m._page_trust_state("entities/a/x.md", fm, _badges(m, fm))
        assert st["state"] == "verified", (
            f"{floor}x{grade} is publishable by review_autoverify but the cockpit quarantines it: "
            f"{st['reasons']} — the two surfaces disagree about what 'grounded' means")
