"""okengine#997: the standing deployment check for content pages under a reserved/dot basename.

The daily in-gateway validator and `framework status`/`doctor` run deployment_checks over the live
vault — the earliest gate that sees vault content. A live offender FAILs (unmaintainable content
that has dropped out of INDEX.md); a vault whose only offenders are tombstones WARNs.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "deployment_checks.py"


def _load():
    spec = importlib.util.spec_from_file_location("deployment_checks", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_checks"] = m
    spec.loader.exec_module(m)
    return m


def _vault(tmp_path):
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\n")
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "h").mkdir(parents=True)
    (wiki / "HEALTH.md").write_text("---\ntype: dashboard\n---\n")            # generated: fine
    (wiki / "entities" / "h" / "INDEX.md").write_text("---\ntype: dashboard\n---\n")
    (wiki / "entities" / "h" / "healthcare.md").write_text("---\ntype: concept\n---\n")
    return tmp_path


def _run(C, root):
    C.configure(root, data=root / "nodata", hermes=root / "nohermes")
    return C.run(["reserved-basenames"])


def test_registered_in_the_daily_validator():
    C = _load()
    assert "reserved-basenames" in C.VALIDATE_CHECKS
    assert C.CHECKS["reserved-basenames"] is C.check_reserved_basenames


def test_live_reserved_page_fails(tmp_path):
    C = _load()
    root = _vault(tmp_path)
    (root / "wiki" / "entities" / "h" / "health.md").write_text("---\ntype: publisher\n---\n")
    findings = _run(C, root)
    assert [(lvl, area) for lvl, area, _ in findings] == [("FAIL", "reserved-basenames")]
    msg = findings[0][2]
    assert "1 content page(s) (1 live, 0 scaffold-shaped)" in msg and "entities/h/health.md" in msg
    assert "repair_malformed_slugs.py" in msg


def test_tombstone_only_warns_and_samples_are_capped(tmp_path):
    C = _load()
    root = _vault(tmp_path)
    for i in range(4):
        (root / "wiki" / "entities" / "h" / f".dot{i}.md").write_text(
            "---\ntype: concept\nstatus: tombstoned\n---\n")
    findings = _run(C, root)
    assert [(lvl, area) for lvl, area, _ in findings] == [("WARN", "reserved-basenames")]
    assert "4 content page(s) (0 live, 0 scaffold-shaped)" in findings[0][2] and findings[0][2].count(".dot") == 3
    assert " …" in findings[0][2]


def test_negative_fixture_clean_vault_reports_nothing(tmp_path):
    C = _load()
    assert _run(C, _vault(tmp_path)) == []


def test_no_wiki_reports_nothing(tmp_path):
    C = _load()
    assert _run(C, tmp_path) == []


def test_exactly_three_offenders_carry_no_ellipsis(tmp_path):
    C = _load()
    root = _vault(tmp_path)
    for name in ("hot", "log", "readme"):
        (root / "wiki" / "entities" / "h" / f"{name}.md").write_text("---\ntype: concept\n---\n")
    findings = _run(C, root)
    assert findings[0][0] == "FAIL" and "3 content page(s) (3 live, 0 scaffold-shaped)" in findings[0][2]
    assert " …" not in findings[0][2]


def test_scaffold_shaped_live_pages_only_warn(tmp_path):
    """A pack template read by name (`predictions/_template.md`) is a review item, not a FAIL; one
    live page under an OKF-reserved name still FAILs the run."""
    C = _load()
    root = _vault(tmp_path)
    tpl = root / "wiki" / "predictions" / "_template.md"
    tpl.parent.mkdir()
    tpl.write_text("---\ntype: prediction\n---\n")
    findings = _run(C, root)
    assert [(lvl, area) for lvl, area, _ in findings] == [("WARN", "reserved-basenames")]
    assert "1 content page(s) (1 live, 1 scaffold-shaped)" in findings[0][2]
    (root / "wiki" / "entities" / "h" / "log.md").write_text("---\ntype: concept\n---\n")
    assert _run(C, root)[0][0] == "FAIL"


@pytest.mark.parametrize("scaffolds,expected", [(1, "2 live, 1 scaffold-shaped"),
                                                (2, "3 live, 2 scaffold-shaped")])
def test_mixed_offenders_count_scaffolds_separately(tmp_path, scaffolds, expected):
    C = _load()
    root = _vault(tmp_path)
    (root / "wiki" / "entities" / "h" / "log.md").write_text("---\ntype: concept\n---\n")
    for i in range(scaffolds):
        (root / "wiki" / "entities" / "h" / f"_t{i}.md").write_text("---\ntype: concept\n---\n")
    findings = _run(C, root)
    assert findings[0][0] == "FAIL" and expected in findings[0][2]
    assert " …" not in findings[0][2]                       # at most three: no ellipsis


def test_a_real_offender_is_never_crowded_out_of_the_sample(tmp_path):
    """QA: three scaffold templates sorted before the one live content page and hid it."""
    C = _load()
    root = _vault(tmp_path)
    for ns in ("aa", "ab", "ac"):
        p = root / "wiki" / ns / "_template.md"
        p.parent.mkdir()
        p.write_text("---\ntype: prediction\n---\n")
    (root / "wiki" / "entities" / "h" / "log.md").write_text("---\ntype: concept\n---\n")
    msg = _run(C, root)[0][2]
    assert msg.index("entities/h/log.md") < msg.index("aa/_template.md")
    assert "ac/_template.md" not in msg and " …" in msg
