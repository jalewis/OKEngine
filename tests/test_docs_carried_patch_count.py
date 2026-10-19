"""okengine#1032 [28]: every document that states the carried-patch count states the real one.

CLAUDE.md said "the 16 carried Hermes core-file patches" long after apply.sh switched to the 24
artifacts in patches/target-v2026.9.14/, and patches/README.md kept a 30-file budget after the
inventory moved to 35. A reader following either edited or audited the wrong thing. The count is
derived here from the directory engine-manifest.yaml names as `runtime.carried_patches`, and the
budget from that series' inventory policy.

CLAUDE.md is excluded from the public snapshot, so its check is guarded with is_file().
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
_COUNT_RE = re.compile(r"\b(\d+) carried (?:Hermes )?(?:core-file )?patch(?:es| artifacts)\b")


def _series() -> Path:
    manifest = yaml.safe_load((REPO / "engine-manifest.yaml").read_text(encoding="utf-8"))
    return REPO / str(manifest["runtime"]["carried_patches"]).rstrip("/")


def _count() -> int:
    return len(list(_series().glob("*.patch")))


def count_errors(text: str, count: int, *, series: str | None = None) -> list[str]:
    """Stated carried-patch counts that disagree with `count`, and a missing series path."""
    errors = [f"states {m.group(1)} carried patches, the series has {count}"
              for m in _COUNT_RE.finditer(text) if int(m.group(1)) != count]
    if series is not None and series not in text:
        errors.append(f"does not name the active series {series}")
    return errors


def test_the_active_series_is_what_apply_sh_selects_for_the_pin():
    manifest = yaml.safe_load((REPO / "engine-manifest.yaml").read_text(encoding="utf-8"))
    pin = manifest["runtime"]["pinned_tag"]
    apply_sh = (REPO / "patches" / "apply.sh").read_text(encoding="utf-8")
    assert f'{pin}) PATCHDIR="$PATCHROOT/{_series().name}"' in apply_sh
    assert _count() > 0


# Records of a past state (bump records, dated reviews, the changelog) state the count of their day.
_HISTORICAL = ("CHANGELOG.md", "docs/hermes-upgrades/", "docs/reviews/")


def _current_docs() -> list:
    """Every current-state document: root and docs/ markdown, patches/ READMEs, the manifest.
    okengine#1044 [28]: a hard-coded list left docs/technical-reference.md at "10" unchecked."""
    found = [*REPO.glob("*.md"), *REPO.glob("docs/**/*.md"), *REPO.glob("patches/**/*.md"),
             REPO / "engine-manifest.yaml"]
    rels = sorted({p.relative_to(REPO).as_posix() for p in found if p.is_file()})
    return [r for r in rels if not r.startswith(_HISTORICAL)]


def test_the_scan_covers_the_documents_that_state_the_count():
    docs = _current_docs()
    assert {"INSTALL.md", "engine-manifest.yaml", "docs/technical-reference.md"} <= set(docs)
    assert not any(d.startswith(_HISTORICAL) for d in docs)


@pytest.mark.parametrize("doc", _current_docs())
def test_no_current_document_states_a_stale_carried_patch_count(doc):
    assert count_errors((REPO / doc).read_text(encoding="utf-8"), _count()) == []


@pytest.mark.parametrize("doc", ["INSTALL.md", "engine-manifest.yaml", "CLAUDE.md",
                                 "docs/technical-reference.md"])
def test_documented_carried_patch_count_matches_the_series(doc):
    path = REPO / doc
    if not path.is_file():
        pytest.skip(f"{doc} is excluded from this checkout (public snapshot)")
    text = path.read_text(encoding="utf-8")
    assert _COUNT_RE.search(text), f"{doc} no longer states the carried-patch count"
    series = _series().relative_to(REPO).as_posix() if doc == "CLAUDE.md" else None
    assert count_errors(text, _count(), series=series) == []


def test_patches_readme_budget_matches_the_inventory_policy():
    policy = json.loads((_series() / "inventory.json").read_text(encoding="utf-8"))["policy"]
    readme = (REPO / "patches" / "README.md").read_text(encoding="utf-8")
    stated = re.search(r"The budget is (\d+) patches and (\d+) distinct touched upstream files",
                       readme)
    assert stated, "patches/README.md no longer states the patch budget"
    assert (int(stated.group(1)), int(stated.group(2))) == (
        policy["maximum_patch_count"], policy["maximum_touched_upstream_files"])


def test_a_stale_count_or_missing_series_is_reported():
    """Negative fixture: the pre-fix CLAUDE.md line."""
    stale = "patches/   the 16 carried Hermes core-file patches + apply.sh"
    errors = count_errors(stale, 24, series="patches/target-v2026.9.14")
    assert errors == ["states 16 carried patches, the series has 24",
                      "does not name the active series patches/target-v2026.9.14"]
    assert count_errors("24 carried patch artifacts in patches/target-v2026.9.14/", 24,
                        series="patches/target-v2026.9.14") == []


# --- okengine#1044 [28]: the bump docs give an apply.sh sequence apply.sh accepts --------------

_OVERRIDE = "OKENGINE_ALLOW_UNPINNED_HERMES=1"
_CASE = 'case "$PIN"'


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n#", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def bump_doc_errors(text: str) -> list:
    """A Hermes-bump procedure must name the override for an unpinned checkout and the `case`
    arm a new pin needs; without them apply.sh exits 2 or 3 and the doc gives no way forward."""
    return [f"does not mention {needle}" for needle in (_OVERRIDE, _CASE) if needle not in text]


def test_apply_sh_still_has_the_override_and_the_pin_case_the_docs_name():
    apply_sh = (REPO / "patches" / "apply.sh").read_text(encoding="utf-8")
    assert '"${OKENGINE_ALLOW_UNPINNED_HERMES:-0}" = 1' in apply_sh
    assert f"{_CASE} in" in apply_sh


@pytest.mark.parametrize("doc,heading", [
    ("INSTALL.md", "## Upgrading Hermes"),
    ("docs/supply-chain.md", "### Hermes-Agent (the pinned runtime)"),
    ("patches/README.md", "**Dropped at v0.18.0"),
    ("CLAUDE.md", "## Core rules"),
])
def test_every_hermes_bump_procedure_gives_a_sequence_apply_sh_accepts(doc, heading):
    path = REPO / doc
    if not path.is_file():
        pytest.skip(f"{doc} is excluded from this checkout (public snapshot)")
    assert bump_doc_errors(_section(path.read_text(encoding="utf-8"), heading)) == []


def test_a_bump_procedure_without_the_override_is_reported():
    """Negative fixture: the pre-fix INSTALL.md section."""
    stale = ("## Upgrading Hermes\nBump the pin: `git checkout <new-tag>` in the Hermes checkout, "
             "re-run\n`patches/apply.sh` (rebase any patch that fails), rebuild.\n")
    assert bump_doc_errors(stale) == [f"does not mention {_OVERRIDE}", f"does not mention {_CASE}"]
