"""Documentation and content-tree checks for :mod:`framework_validate`."""
from __future__ import annotations

from pathlib import Path


def check_docs(pack: Path, report) -> None:
    """Require an operator-useful README and an explicit distribution license."""
    readme = pack / "README.md"
    if not readme.is_file():
        report.fail("README.md", "missing — a pack must ship a README (what it ingests, deploy, layout)")
        return
    text = readme.read_text(encoding="utf-8", errors="replace")
    headings = [line for line in text.splitlines() if line.lstrip().startswith("## ")]
    if len(text.strip()) < 200 or not headings:
        report.fail("README.md", "stub — document the pack (what it ingests, deploy, layout)")
        return
    heading_text = "\n".join(
        line.lower() for line in text.splitlines() if line.lstrip().startswith("#")
    )
    if not any(
        key in heading_text
        for key in ("deploy", "install", "bring up", "quickstart", "getting started")
    ):
        report.fail(
            "README.md Deploy section",
            "no Deploy/Install/Quickstart heading — document how to bring the pack up",
        )
    else:
        report.ok("README.md", f"{len(headings)} section(s)")
    if not any(key in text.lower() for key in ("layout", "services", "structure", "schema")):
        report.warn("README.md sections", "no layout/structure section found")
    license_path = next(
        (
            pack / name
            for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")
            if (pack / name).is_file()
        ),
        None,
    )
    if license_path and license_path.read_text(encoding="utf-8", errors="replace").strip():
        report.ok("LICENSE", license_path.name)
    else:
        report.fail("LICENSE", "missing — every pack must ship a license (LICENSE / LICENSE.md / COPYING)")


def check_wiki(pack: Path, report) -> None:
    if not (pack / "wiki").is_dir():
        report.warn("wiki/", "absent — the content tree the engine compiles into")
        return
    report.ok("wiki/", "present")
