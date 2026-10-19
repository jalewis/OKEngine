"""okengine#997 (QA round 2): every read-side skip agrees with the ONE reserved-name predicate.

`schema_validator.is_reserved_basename` decides what the write path refuses, the validator exempts
and the minting guard never mints. The READ side has its own copies: the reader, backlinks, the
lint changeset, the critic and the cockpit rail each hide a set of "generated" names. Where those
disagree, a page is writable but invisible. Narrowing the write path to `index-pNN` made
`index-funds` exactly that, because every reader still hid `index-*`.

The invariant pinned here: a name a consumer hides is a name the write path refuses, so no
writable page is ever hidden, and no name the guard can mint is ever hidden.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "cron"))
from tools import schema_validator as sv  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _consumers():
    import backlink_lib
    import wiki_change_check
    from okengine.cockpit_services import navigation
    out = {
        "backlink_lib.skip_name": backlink_lib.skip_name,
        "wiki_change_check._skip": wiki_change_check._skip,
        "critic._is_generated": _load("select_critic_997",
                                      "extensions/okengine.critic/select_critic.py")._is_generated,
        "cockpit navigation._skip": navigation._skip,
        "cockpit navigation._bl_skip_name": navigation._bl_skip_name,
    }
    sys.path.insert(0, str(REPO / "okengine-reader"))
    out["reader rendering._skip"] = _load("rendering_997", "okengine-reader/rendering.py")._skip
    return out


BATTERY = [
    # reserved / generated
    "INDEX.md", "index.md", "Index.md", "INDEX-p02.md", "index-p02.md", "INDEX-glossary.md",
    "index-funds.md", "Index-funds.md", "index-sickness-pang-principle.md", "index-concept.md",
    "index-dashboard.md", "index-p02-concept.md", "_template.md", ".net-x.md", "health.md",
    "HEALTH.md", "log.md", "README.md", "AGENTS.md", "hot.md", "bundle.md",
    # writable near-misses
    "healthcare.md", "indexing.md", "indexshare.md", "reindex-funds.md", "concept-index.md",
    "dashboard-index.md", "dotnet-x.md", "health-publisher.md", "page-index.md", "a_b.md",
    "logistics.md", "x.index-y.md",
]


def _stand_ins(tmp_path):
    import okf_migrate
    importlib.reload(okf_migrate)
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    slugs = [n[:-3] for n in sv._OKF_RESERVED_DEFAULT] + [
        "HEALTH", "Index", ".net-x", "..x", "_scratch", "INDEX-glossary", "index-p01",
        "Index-P7", "index-funds", "index-sickness-pang-principle", "INDEX"]
    out = set()
    for slug in slugs:
        for fm in ({"type": "concept"}, {"type": "dashboard"}, {}, {"type": "Threat Note"}):
            out.add(okf_migrate.mintable_slug(tmp_path, "concepts", slug, fm) + ".md")
    return sorted(out)


@pytest.mark.parametrize("consumer", sorted(_consumers()))
def test_a_name_a_consumer_hides_is_a_name_the_write_path_refuses(consumer):
    skip = _consumers()[consumer]
    hidden_but_writable = [n for n in BATTERY if skip(n) and not sv.is_reserved_basename(n)]
    assert hidden_but_writable == [], (
        f"{consumer} hides {hidden_but_writable}, which the write path accepts: a writable page "
        f"no reader shows")


@pytest.mark.parametrize("consumer", sorted(_consumers()))
def test_no_stand_in_the_guard_mints_is_hidden(consumer, tmp_path):
    skip = _consumers()[consumer]
    stand_ins = _stand_ins(tmp_path)
    assert stand_ins and [n for n in stand_ins if skip(n)] == []
    assert [n for n in stand_ins if sv.is_reserved_basename(n)] == []


# The broader cron/extension scans skip ANY name starting `INDEX` (case-folded or not), inline.
# They agree with the guard: no stand-in it mints starts with `index`.
_BROAD = {
    "upper().startswith('INDEX') scans": lambda n: (n.startswith(("_", "."))
                                                    or n.upper().startswith("INDEX")),
    "startswith('INDEX') scans": lambda n: n.startswith(("_", ".")) or n.startswith("INDEX"),
}


@pytest.mark.parametrize("family", sorted(_BROAD))
def test_no_stand_in_is_hidden_by_the_broad_index_scans(family, tmp_path):
    assert [n for n in _stand_ins(tmp_path) if _BROAD[family](n)] == []


@pytest.mark.xfail(strict=True, reason=(
    "Known defect, follow-up to okengine#997: the inline `name.upper().startswith('INDEX')` scans "
    "(authority_enrich, review_autoverify, review_reconcile, condition_recheck, registry_coverage, "
    "schema_type_drain, source_normalize, inquiry_*, scope_prescore) also hide writable pages such "
    "as a live `entities/i/n/indexshare.md`. Narrow them to the generated index tree; this test "
    "then passes and strict xfail turns it red to be un-marked."))
def test_the_broad_index_scans_hide_no_writable_page():
    writable = [n for n in BATTERY if not sv.is_reserved_basename(n)]
    hidden = {f: [n for n in writable if pred(n)] for f, pred in _BROAD.items()}
    assert hidden == {f: [] for f in _BROAD}
