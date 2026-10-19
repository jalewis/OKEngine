"""dedup-same-url: one URL at several seats, merged through the REAL write path (okengine#886).

End-to-end tests import this repo's write_server as the authenticated `cron:dedup-same-url`
actor, held to its own output_contract from jobs.json. Nothing on the write path is stubbed
except where a test injects a refusal to prove the lane stops.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"
CONTRACT = next(j for j in json.loads((REPO / "config/engine-crons.json").read_text())
                if j["name"] == "dedup-same-url")["output_contract"]
URL = "https://lab.test/paper"
UID = "sources:url-" + hashlib.sha256(URL.encode()).hexdigest()[:20]
LONG = "A full abstract of the paper. " * 30          # > 600 non-space chars
_FRESH = ("dedup_same_url", "write_server", "output_contract_enforce", "engine_package",
          "backfill_ids")


def _page(root: Path, rel: str, fm: dict, body: str = "Body text long enough to count.") -> Path:
    p = root / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body + "\n")
    return p


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split("---")[1])


def _queue(root: Path) -> str:
    q = root / "wiki" / "_review-queue.md"
    return q.read_text() if q.is_file() else ""


@pytest.fixture
def lane(tmp_path, monkeypatch):
    saved_modules = {n: sys.modules.get(n) for n in _FRESH}
    saved_path = list(sys.path)
    saved_env = {v: os.environ.pop(v, None)
                 for v in ("OKENGINE_WRITE_ACTOR", "OKENGINE_OUTPUT_CONTRACT_MODE")}

    def load(*, contract: bool = True):
        (tmp_path / "wiki" / "sources").mkdir(parents=True, exist_ok=True)
        (tmp_path / "schema.yaml").write_text(yaml.safe_dump({
            "types": {"source": {"required": ["type"], "extensible": True},
                      "entity": {"required": ["type"], "extensible": True},
                      "concept": {"required": ["type"], "extensible": True}},
            "permissions": {"default": {"create": True, "update": True, "delete": False}},
        }))
        jobs = [{"name": "dedup-same-url", "output_contract": CONTRACT}] if contract else []
        (tmp_path / "jobs.json").write_text(json.dumps({"jobs": jobs}))
        monkeypatch.setenv("WIKI_PATH", str(tmp_path))
        monkeypatch.setenv("OKENGINE_CRON_JOBS", str(tmp_path / "jobs.json"))
        monkeypatch.setenv("OKENGINE_MCP_DIR", str(REPO / "okengine-mcp"))
        monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-09-26")
        sys.path.insert(0, str(CRON))
        for mod in _FRESH:
            sys.modules.pop(mod, None)
        mod = importlib.import_module("dedup_same_url")
        return mod, tmp_path

    yield load
    for var, value in saved_env.items():
        os.environ.pop(var, None) if value is None else os.environ.__setitem__(var, value)
    sys.path[:] = saved_path
    for name, module in saved_modules.items():
        sys.modules.pop(name, None) if module is None else sys.modules.__setitem__(name, module)


def _pair(root: Path) -> None:
    _page(root, "sources/a", {"type": "source", "id": UID, "title": "Sparse Attention", "url": URL,
                              "raw": "raw/a.md", "created": "2026-09-02"})
    _page(root, "sources/b", {"type": "source", "title": "Sparse Attention at Scale", "url": URL,
                              "raw": "raw/b.md", "hf_upvotes": 12, "created": "2026-09-01"}, LONG)
    _page(root, "entities/e/e", {"type": "entity", "id": "entities:e",
                               "sources": ["sources/b", "sources/bb"]}, "cites [[sources/b]]")
    _page(root, "entities/t/t", {"type": "entity", "id": "entities:t", "status": "tombstoned",
                               "sources": ["sources/b"]})
    _page(root, "_logs/2026-09-26", {"type": "log"}, "wrote sources/b")


def test_same_url_pages_merge_onto_the_url_id_holder(lane, capsys):
    mod, root = lane()
    _pair(root)
    assert mod.main(["--apply"]) == 0
    out = capsys.readouterr().out
    survivor, loser = _fm(root / "wiki/sources/a.md"), _fm(root / "wiki/sources/b.md")
    assert survivor["id"] == UID, "the survivor keeps its id — an id never moves"
    assert survivor["raw"] == ["raw/a.md", "raw/b.md"] and survivor["hf_upvotes"] == 12
    assert "full abstract" in (root / "wiki/sources/a.md").read_text(), "longest body wins"
    assert loser.get("status") == "tombstoned", out
    assert loser["superseded_by"] == "sources/a"
    assert loser["id"] == "sources:b", "an id-less loser gets a stem id to be retired"
    ent = _fm(root / "wiki/entities/e/e.md")
    assert ent["sources"] == ["sources/a", "sources/bb"], "path-boundary: sources/bb untouched"
    assert "[[sources/a]]" in (root / "wiki/entities/e/e.md").read_text()
    assert _fm(root / "wiki/entities/t/t.md")["sources"] == ["sources/b"], "tombstoned referrer"
    assert "sources/b" in (root / "wiki/_logs/2026-09-26.md").read_text(), "history untouched"
    assert '"losers": 1' in out


def test_dry_run_writes_nothing(lane, capsys):
    mod, root = lane()
    _pair(root)
    before = {p: p.read_text() for p in (root / "wiki").rglob("*.md")}
    mod.main([])
    assert {p: p.read_text() for p in (root / "wiki").rglob("*.md")} == before
    assert "DRY RUN" in capsys.readouterr().out


def test_an_impostor_is_excluded_and_flagged_never_fused(lane):
    """REGRESSION: `ImportAI 453` filed under issue 447's URL must not be merged into 447."""
    mod, root = lane()
    _page(root, "sources/i1", {"type": "source", "title": "Issue 447: Alpha beta", "url": URL})
    _page(root, "sources/i2", {"type": "source", "title": "Issue #447 - Alpha beta gamma",
                               "url": URL})
    _page(root, "sources/i3", {"type": "source", "title": "Issue 453 - Delta epsilon",
                               "url": URL})
    mod.main(["--apply"])
    assert _fm(root / "wiki/sources/i3.md").get("status") != "tombstoned"
    assert "sources/i3" in _queue(root), "the impostor is queued for review with a reason"
    retired = [n for n in ("i1", "i2") if _fm(root / f"wiki/sources/{n}.md").get("status")]
    assert len(retired) == 1


def test_when_no_two_titles_agree_every_member_is_flagged(lane):
    """Three different papers under one URL: nobody knows which owns it, so all are flagged."""
    mod, root = lane()
    for n, title in (("p1", "Graph search planning"), ("p2", "Diffusion batching serving")):
        _page(root, f"sources/{n}", {"type": "source", "title": title, "url": URL})
    mod.main(["--apply"])
    for n in ("p1", "p2"):
        assert _fm(root / f"wiki/sources/{n}.md").get("status") != "tombstoned"
        assert f"sources/{n}" in _queue(root)


def test_matching_issue_numbers_merge_despite_different_wording(lane):
    mod, root = lane()
    _page(root, "sources/w1", {"type": "source", "title": "Issue 453: breaking agents", "url": URL})
    _page(root, "sources/w2", {"type": "source", "title": "Issue #453 - mirror code benchmark",
                               "url": URL})
    groups, _ = mod.plan(mod._write_server(), mod.live_sources(mod._write_server()))
    assert len(groups) == 1 and len(groups[0]["losers"]) == 1 and not groups[0]["excluded"]


def test_placeholder_urls_are_never_grouped(lane, capsys):
    mod, root = lane()
    for n in ("x1", "x2"):
        _page(root, f"sources/{n}", {"type": "source", "title": f"Paper {n}",
                                     "url": "https://arxiv.test/abs/[pending]"})
    mod.main(["--apply"])
    assert '"groups": 0' in capsys.readouterr().out
    assert all(_fm(root / f"wiki/sources/{n}.md").get("status") != "tombstoned"
               for n in ("x1", "x2"))


def test_placeholders_are_repaired_only_from_one_agreeing_capture(lane, capsys):
    mod, root = lane()
    (root / "raw").mkdir()
    (root / "raw/one.md").write_text("---\nurl: https://arxiv.test/abs/1\n---\nbody\n")
    (root / "raw/rec.json").write_text('{"payload": {"source_url": "https://arxiv.test/abs/2"}}')
    (root / "raw/a.md").write_text("---\nurl: https://arxiv.test/abs/3\n---\n")
    (root / "raw/b.md").write_text("---\ncanonical_url: https://arxiv.test/abs/4\n---\n")
    (root / "raw/bad.md").write_text("---\nurl: [unclosed\n---\n")
    (root / "raw/list.md").write_text("---\n- x\n---\n")
    ph = "https://arxiv.test/abs/[pending]"
    _page(root, "sources/md", {"type": "source", "url": ph, "raw": "raw/one.md"})
    _page(root, "sources/js", {"type": "source", "url": ph, "raw": ["raw/rec.json"]})
    _page(root, "sources/amb", {"type": "source", "url": ph, "raw": ["raw/a.md", "raw/b.md"]})
    _page(root, "sources/none", {"type": "source", "url": ph,
                                 "raw": ["raw/missing.md", "raw/bad.md", "raw/list.md"]})
    mod.main(["--placeholders"])
    assert "planned" in capsys.readouterr().out
    assert _fm(root / "wiki/sources/md.md")["url"] == ph, "dry run by default"
    mod.main(["--placeholders", "--apply"])
    out = capsys.readouterr().out
    assert _fm(root / "wiki/sources/md.md")["url"] == "https://arxiv.test/abs/1", out
    assert _fm(root / "wiki/sources/js.md")["url"] == "https://arxiv.test/abs/2"
    assert "raw captures disagree (2 URLs)" in out
    assert "no raw capture carries a real URL" in out
    assert '"repaired": 2' in out and '"unrepairable": 2' in out


def test_tombstoned_members_do_not_form_a_group(lane, capsys):
    mod, root = lane()
    _page(root, "sources/live", {"type": "source", "title": "T", "url": URL})
    _page(root, "sources/dead", {"type": "source", "title": "T", "url": URL,
                                 "status": "tombstoned"})
    (root / "wiki/sources/INDEX.md").write_text("[[sources/live]]\n")
    (root / "wiki/sources/gone.md").symlink_to(root / "nowhere.md")
    mod.main([])
    assert '"groups": 0' in capsys.readouterr().out


def test_without_its_contract_the_lane_writes_nothing(lane, capsys):
    """NEGATIVE: an authenticated writer, not an admin bypass."""
    mod, root = lane(contract=False)
    _pair(root)
    mod.main(["--apply"])
    assert "STOP: survivor update refused" in capsys.readouterr().out
    assert _fm(root / "wiki/sources/b.md").get("status") != "tombstoned"


@pytest.mark.parametrize("where", ["relink", "tombstone"])
def test_a_refused_write_stops_the_group_and_the_loser_stays_live(lane, where):
    mod, root = lane()
    _pair(root)
    W = mod._write_server()
    pages = mod.live_sources(W)
    groups, _ = mod.plan(W, pages)
    if where == "relink":
        real = W._update
        W._update = lambda page, *a: ("rejected: no" if page.startswith("entities/")
                                      else real(page, *a))
    else:
        W._tombstone = lambda *a, **k: "refused: no"
    log = mod.apply_group(W, groups[0], pages, set())
    assert log[-1].startswith("STOP")
    assert _fm(root / "wiki/sources/b.md").get("status") != "tombstoned"


def test_a_group_with_nothing_to_carry_goes_straight_to_retirement(lane):
    mod, root = lane()
    body = "Same body on both copies of the page."
    _page(root, "sources/k", {"type": "source", "id": UID, "title": "T", "url": URL,
                              "raw": "raw/k.md"}, body)
    _page(root, "sources/l", {"type": "source", "id": "sources:l", "title": "T", "url": URL,
                              "raw": "raw/k.md"}, body)
    W = mod._write_server()
    pages = mod.live_sources(W)
    groups, _ = mod.plan(W, pages)
    log = mod.apply_group(W, groups[0], pages, set())
    assert not any(line.startswith(("update", "stamp")) for line in log), log
    assert _fm(root / "wiki/sources/l.md")["status"] == "tombstoned"


def test_stem_ids_never_collide(lane):
    mod, root = lane()
    _page(root, "sources/dup", {"type": "source", "title": "T"})
    claimed = {"sources:dup", "sources:dup-2"}
    assert mod._stamp_stem_id("sources/dup", claimed) == "sources:dup-3"
    assert "sources:dup-3" in claimed


def test_title_consistency_rules(lane):
    mod, _root = lane()
    assert mod.consistent("Issue 447 x", "Issue 453 x") is False
    assert mod.consistent("Issue 447 alpha", "#447 completely different words") is True
    assert mod.consistent("Report 2026 on agents", "Agents report June 2026") is True
    assert mod.consistent(None, "Anything") is True
    assert mod.consistent("Graph planning search", "Diffusion serving batching") is False


def test_inbound_skips_unreadable_pages(lane):
    mod, root = lane()
    (root / "wiki/entities").mkdir(parents=True)
    (root / "wiki/entities/gone.md").symlink_to(root / "nowhere.md")
    _page(root, "entities/ok", {"type": "entity"}, "[[sources/a]] and sources/a.md")
    assert mod.inbound({"sources/a"}) == {"sources/a": {"entities/ok.md"}}


def test_no_sources_directory_is_a_clean_no_op(lane, capsys):
    mod, root = lane()
    (root / "wiki" / "sources").rmdir()
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == '{"wakeAgent": false}'


def test_a_repaired_placeholder_page_gets_its_url_identity(lane):
    """An id-less placeholder page cannot be updated; once its real URL is known it gets the
    URL-derived id (the identity every raw writer converges on) and then the URL."""
    mod, root = lane()
    (root / "raw").mkdir()
    (root / "raw/one.md").write_text("---\nurl: https://arxiv.test/abs/9\n---\n")
    _page(root, "sources/ph", {"type": "source", "url": "https://arxiv.test/abs/[pending]",
                               "raw": "raw/one.md"})
    mod.main(["--placeholders", "--apply"])
    fm = _fm(root / "wiki/sources/ph.md")
    assert fm["url"] == "https://arxiv.test/abs/9"
    assert fm["id"] == "sources:url-" + hashlib.sha256(b"https://arxiv.test/abs/9").hexdigest()[:20]


def test_claimed_ids_include_retired_pages_and_skip_unreadable_ones(lane):
    mod, root = lane()
    _page(root, "sources/live", {"type": "source", "id": "sources:live"})
    _page(root, "sources/dead", {"type": "source", "id": "sources:x", "status": "tombstoned"})
    _page(root, "sources/none", {"type": "source"})
    (root / "wiki/sources/gone.md").symlink_to(root / "nowhere.md")
    assert mod.claimed_ids(mod._write_server()) == {"sources:live", "sources:x"}


def test_edges_substantive_bodies_nested_links_bare_urls_and_ided_placeholders(lane):
    """Both bodies substantive and the survivor's replaced: the survivor is queued for review.
    Links are rewritten inside nested frontmatter; non-string values pass through; a source with
    no usable URL is not grouped; a placeholder page that already has an id keeps it."""
    mod, root = lane()
    _page(root, "sources/a", {"type": "source", "id": UID, "title": "Sparse Attention",
                              "url": URL}, "Short but substantive. " * 40)
    _page(root, "sources/b", {"type": "source", "title": "Sparse Attention at Scale",
                              "url": URL}, LONG * 2)
    _page(root, "sources/nourl", {"type": "source", "title": "No address", "url": "UNKNOWN"})
    _page(root, "entities/n/n", {"type": "entity", "id": "entities:n", "rank": 3,
                                 "evidence": {"primary": "sources/b", "weight": 2}})
    (root / "raw").mkdir()
    (root / "raw/r.md").write_text("---\nurl: https://arxiv.test/abs/7\n---\n")
    _page(root, "sources/idph", {"type": "source", "id": "sources:idph",
                                 "url": "https://arxiv.test/abs/[pending]", "raw": "raw/r.md"})
    mod.main(["--apply"])
    assert "sources/a" in _queue(root), "replaced substantive body is queued for review"
    ent = _fm(root / "wiki/entities/n/n.md")
    assert ent["evidence"] == {"primary": "sources/a", "weight": 2} and ent["rank"] == 3
    mod.main(["--placeholders", "--apply"])
    fm = _fm(root / "wiki/sources/idph.md")
    assert fm["id"] == "sources:idph" and fm["url"] == "https://arxiv.test/abs/7"


def test_generated_reports_are_not_relinked_and_do_not_stop_the_merge(lane):
    """okengine#886: a frontmatter-less dashboard and a `generator:`-stamped lint report mentioned a
    duplicate; relinking them was refused and the whole group stayed live. They are skipped, the
    real citation is still rewritten, and the loser is retired."""
    mod, root = lane()
    _pair(root)
    dash = root / "wiki" / "dashboards" / "corpus-audit.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text("# Corpus integrity audit\n\nsee `sources/b.md`\n", encoding="utf-8")
    _page(root, "lint-2026-07-29-v1", {"type": "lint", "generator": "wiki-health-audit"},
          "broken: [[sources/b]]")
    before = (dash.read_text(encoding="utf-8"), (root / "wiki" / "lint-2026-07-29-v1.md").read_text())

    assert mod.main(["--apply"]) == 0

    assert _fm(root / "wiki" / "sources" / "b.md").get("status") == "tombstoned"
    assert "sources/a" in (root / "wiki" / "entities" / "e" / "e.md").read_text(), \
        "a real citation is still rewritten to the survivor"
    assert (dash.read_text(encoding="utf-8"),
            (root / "wiki" / "lint-2026-07-29-v1.md").read_text()) == before, \
        "generated reports are left exactly as they were"
