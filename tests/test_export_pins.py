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
