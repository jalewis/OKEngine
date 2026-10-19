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
    # a LIVE flag is pending work whatever was signed (okengine#867): review_policy, the Cockpit and
    # the #842 reconciler all require it, and this queue used to hide it
    assert "NEEDS-REVIEW | [[entities/a/needs-vetted]]" in d
    assert "GROUNDING: **1**" in d
    assert "NEEDS-REVIEW: **2**" in d
    assert "UNVETTED: **2**" in d
    assert "**5 item(s) awaiting a human**" in d

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


def test_tombstoned_pages_never_enter_dashboard(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    entities = wiki / "entities"
    entities.mkdir(parents=True)
    (entities / "retired.md").write_text(
        "---\ntype: entity\nstatus: tombstoned\nneeds_review: true\n---\nbody\n")
    # This sorts after the tombstone and its status is lexically greater than "tombstoned".
    # It proves the filter is equality-based and that encountering a tombstone continues the
    # whole scan rather than terminating it.
    (entities / "z-active.md").write_text(
        "---\ntype: entity\nstatus: verified\nneeds_review: true\n---\nbody\n")
    (tmp_path / "schema.yaml").write_text(
        yaml.safe_dump({"okf": {"required": ["type"]},
                        "review_required_types": ["entity"]}))

    dashboard = _run(tmp_path, monkeypatch)

    assert "entities/retired" not in dashboard
    assert "NEEDS-REVIEW | [[entities/z-active]]" in dashboard


def test_split_and_main_edge_paths_and_overflow(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "review_queue_edges", REPO / "scripts/cron/review_queue.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    unreadable = tmp_path / "unreadable.md"; unreadable.write_text("x")
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs:
                        (_ for _ in ()).throw(OSError("race"))
                        if path == unreadable else original(path, *args, **kwargs))
    assert m._split(unreadable) == ({}, "")
    plain = tmp_path / "plain.md"; plain.write_text("body")
    assert m._split(plain) == ({}, "body")
    bad = tmp_path / "bad.md"; bad.write_text("---\n[broken\n---\nbody")
    bad_fm, bad_body = m._split(bad)
    assert bad_fm == {} and bad_body.strip() == "body"
    scalar = tmp_path / "scalar.md"; scalar.write_text("---\n- x\n---\nbody")
    scalar_fm, scalar_body = m._split(scalar)
    assert scalar_fm == {} and scalar_body.strip() == "body"
    assert m.main() == 1
    assert "wiki not found" in capsys.readouterr().err

    wiki = tmp_path / "wiki/entities"; wiki.mkdir(parents=True)
    (wiki / "INDEX.md").write_text("ignored")
    (wiki / "plain.md").write_text("body")
    monkeypatch.setattr(m, "WIKI", tmp_path / "wiki")
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "DASH", tmp_path / "wiki/dashboards/review-queue.md")
    assert m.main() == 0
    assert "empty — all clear" in m.DASH.read_text()
    for index in range(3):
        (wiki / f"p{index}.md").write_text(
            "---\ntype: entity\nneeds_review: true\n---\nbody")
    monkeypatch.setattr(m, "SAMPLES", 1)
    assert m.main() == 0
    assert "…and 2 more" in m.DASH.read_text()


def test_the_queue_lists_exactly_what_review_policy_requires(tmp_path, monkeypatch):
    """okengine#867: one membership rule. Same-day re-flag after sign-off and a grounding failure
    added after sign-off were the cases the old reviewed_on >= last_updated shortcut hid."""
    sys.path.insert(0, str(REPO / "src"))
    from okengine.review_policy import requires_review
    wiki = tmp_path / "wiki" / "entities" / "a"
    wiki.mkdir(parents=True)
    grounding = "\n## Grounding check\n- **unsupported** - claim not in source\n"
    pages = {
        "same-day-reflag": ("type: entity\nneeds_review: true\nreviewed_on: 2026-09-25\n"
                            "last_updated: '2026-09-25T15:00:00Z'\n", ""),
        "grounding-after-signoff": ("type: entity\nreviewed_on: 2026-09-25\nlast_updated: 2026-09-25\n",
                                    grounding),
        "signed-off-briefing": ("type: briefing\nreviewed_on: 2026-09-25\nlast_updated: 2026-09-25\n", ""),
        "edited-briefing": ("type: briefing\nreviewed_on: 2026-09-20\nlast_updated: 2026-09-25\n", ""),
        "approved-briefing": ("type: briefing\nreview_state: approved\nlast_updated: 2026-09-25\n", ""),
        "plain": ("type: entity\nlast_updated: 2026-09-25\n", ""),
    }
    for name, (fm, body) in pages.items():
        (wiki / f"{name}.md").write_text(f"---\n{fm}---\n# {name}\n{body}")
    (tmp_path / "schema.yaml").write_text(yaml.safe_dump({"okf": {"required": ["type"]},
                                                          "review_required_types": ["briefing"]}))
    queue = _run(tmp_path, monkeypatch)
    for name, (fm, body) in pages.items():
        required = requires_review(yaml.safe_load(fm), f"# {name}\n{body}", frozenset({"briefing"}))
        assert (f"entities/a/{name}]]" in queue) == required, (
            f"{name}: review_policy says required={required}, but the queue "
            f"{'omits' if required else 'lists'} it")
    assert "entities/a/same-day-reflag]]" in queue and "entities/a/grounding-after-signoff]]" in queue
