"""The reader and cockpit both render PDF via weasyprint (pandoc `--pdf-engine`), but each pins it
in its OWN requirements.txt. They drifted once: the cockpit lagged at weasyprint 62.3 with pydyf
left UNPINNED, so pip installed pydyf 0.12.1 — which 62.3 can't drive ('super' object has no
attribute 'transform') — breaking ALL pdf export, and 62.3 still carried CVE-2025-68616.

Guard the shared PDF stack so the two images can't silently diverge again, and so pydyf can never
go unpinned (an unpinned transitive dep is exactly how this broke).
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQS = {
    "reader": REPO / "okengine-reader" / "requirements.txt",
    "cockpit": REPO / "okengine-cockpit" / "requirements.txt",
}
_MIN_WEASYPRINT = (68, 0)   # CVE-2025-68616 fixed in 68.0 (okengine#95)


def _pin(text: str, pkg: str) -> str | None:
    m = re.search(rf"^{re.escape(pkg)}==([0-9][\w.]*)", text, re.M | re.I)
    return m.group(1) if m else None


def test_pdf_stack_pinned_and_matched_across_reader_and_cockpit():
    texts = {k: p.read_text(encoding="utf-8") for k, p in REQS.items()}
    for pkg in ("weasyprint", "pydyf"):
        pins = {k: _pin(t, pkg) for k, t in texts.items()}
        assert all(pins.values()), (
            f"{pkg} must be PINNED (==) in BOTH reader and cockpit — got {pins}. An unpinned {pkg} "
            f"drifts to an incompatible release and breaks pdf export.")
        assert len(set(pins.values())) == 1, (
            f"{pkg} pins diverge across reader/cockpit: {pins}. They share the pandoc/weasyprint "
            f"PDF path, so the versions must match.")


def test_weasyprint_at_or_above_cve_floor():
    for k, p in REQS.items():
        ver = _pin(p.read_text(encoding="utf-8"), "weasyprint")
        tup = tuple(int(x) for x in re.findall(r"\d+", ver)[:2])
        assert tup >= _MIN_WEASYPRINT, (
            f"{k}: weasyprint {ver} is below the CVE-2025-68616 floor {_MIN_WEASYPRINT} (okengine#95)")


def test_reader_cockpit_share_every_common_dependency_pin():
    """okengine#95-class (invariant audit HIGH #2): the cockpit is the reader's twin — it imports
    fastapi + markdown too — but its pins lagged the reader's CVE fix (fastapi 0.115.6/starlette
    0.41.x, markdown 3.7) for releases because NOTHING compared their FULL pin sets (only weasyprint/
    pydyf were guarded) and CI pip-audit never scanned the cockpit. Every package pinned in BOTH must
    pin the SAME version — a CVE bump to one is now forced onto the other."""
    def pins(text):
        return dict(re.findall(r"^([A-Za-z0-9_.\[\]-]+)==([0-9][\w.]*)", text, re.M))
    rd = pins(REQS["reader"].read_text(encoding="utf-8"))
    ck = pins(REQS["cockpit"].read_text(encoding="utf-8"))
    common = set(rd) & set(ck)
    assert common, "reader/cockpit share no pinned deps — parser broke, not a real pass"
    drift = {p: (rd[p], ck[p]) for p in common if rd[p] != ck[p]}
    assert not drift, (f"reader/cockpit common-dependency pins diverge (reader, cockpit): {drift} — "
                       "sync them; a CVE fix to one twin must reach the other")


def test_cve_sensitive_deps_are_audited_in_ci_and_makefile():
    """The lag hid because the cockpit is a separate image/venv that no audit surface scanned.
    Every requirements file that ships in an image must be pip-audited in BOTH CI and the Makefile
    audit target — a new image whose deps aren't scanned fails HERE."""
    must = ("okengine-reader/requirements.txt", "okengine-cockpit/requirements.txt",
            "okengine-mcp/requirements.txt")
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    for req in must:
        assert req in ci, f"CI pip-audit does not scan {req} — a shipped image's deps are unaudited"
        assert req in mk, f"Makefile audit target does not scan {req}"
