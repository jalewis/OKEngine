"""review_queue (okengine#69): builds the prioritized human-review queue; reviewed_on>=last_updated
clears an item; editing after sign-off returns it."""
import importlib.util, sys
from pathlib import Path
import pytest
yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent


def _run(tmp, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    spec = importlib.util.spec_from_file_location("review_queue", REPO / "scripts/cron/review_queue.py")
    m = importlib.util.module_from_spec(spec); sys.modules["review_queue"] = m; spec.loader.exec_module(m)
    assert m.main() == 0
    return (tmp / "wiki" / "dashboards" / "review-queue.md").read_text()


def test_queue(tmp_path, monkeypatch):
    e = tmp_path / "wiki" / "entities" / "a"; e.mkdir(parents=True)
    (e / "flagged.md").write_text("---\ntype: entity\nlast_updated: 2026-06-28\n---\n"
                                  "# x\n## Grounding check\n- **unsupported** — claim not in source\n")
    (e / "needs.md").write_text("---\ntype: entity\nneeds_review: true\nlast_updated: 2026-06-28\n---\n# n\n")
    (e / "clean.md").write_text("---\ntype: entity\nlast_updated: 2026-06-28\n---\n# c\n")
    br = tmp_path / "wiki" / "briefings"; br.mkdir(parents=True)
    (br / "unvetted.md").write_text("---\ntype: briefing\nlast_updated: 2026-06-28\n---\n# u\n")
    (br / "vetted.md").write_text("---\ntype: briefing\nlast_updated: 2026-06-20\n"
                                  "reviewed_on: 2026-06-25\nreviewed_by: jl\n---\n# v\n")
    (e / "needs-vetted.md").write_text("---\ntype: entity\nneeds_review: true\nlast_updated: 2026-06-20\n"
                                       "reviewed_on: 2026-06-25\nreviewed_by: jl\n---\n# nv\n")
    (br / "stale-vet.md").write_text("---\ntype: briefing\nlast_updated: 2026-06-28\n"
                                     "reviewed_on: 2026-06-20\nreviewed_by: jl\n---\n# s\n")  # edited after sign-off
    (tmp_path / "schema.yaml").write_text(yaml.safe_dump({"okf": {"required": ["type"]},
                                                          "review_required_types": ["briefing"]}))
    d = _run(tmp_path, monkeypatch)
    assert "GROUNDING | [[entities/a/flagged]]" in d   # wikilink: in-app navigation
    assert "NEEDS-REVIEW | [[entities/a/needs]]" in d
    assert "UNVETTED | [[briefings/unvetted]]" in d
    assert "briefings/stale-vet" in d                 # edited after sign-off -> re-review
    assert "entities/a/clean" not in d                # clean -> not queued
    assert "briefings/vetted" not in d                # signed off at current version -> cleared
    assert "entities/a/needs-vetted" not in d         # needs_review but signed off -> cleared

def test_queue_rows_are_wikilinks_not_relative_md_links(tmp_path, monkeypatch):
    """The reader renders [[wikilinks]] as in-app navigation; a file-relative
    (path.md) href walks the browser out of the SPA (review-caught). Pin the format."""
    import importlib.util, sys
    from pathlib import Path as P
    CRON = P(__file__).resolve().parent.parent.parent / "scripts" / "cron"
    spec = importlib.util.spec_from_file_location("review_queue", CRON / "review_queue.py")
    m = importlib.util.module_from_spec(spec); sys.modules["review_queue"] = m
    spec.loader.exec_module(m)
    vault = tmp_path; wiki = vault / "wiki"
    (wiki / "lacuna").mkdir(parents=True)
    (wiki / "lacuna" / "x.md").write_text("---\ntype: lacuna\nneeds_review: true\n---\nbody\n")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    m.WIKI = wiki; m.VAULT = vault; m.DASH = wiki / "dashboards" / "review-queue.md"
    m.main()
    out = (wiki / "dashboards" / "review-queue.md").read_text()
    assert "[[lacuna/x]]" in out, out
    assert "](lacuna/x.md)" not in out
