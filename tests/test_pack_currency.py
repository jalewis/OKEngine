"""okengine#896: a deployment's installed packs are compared with where they came from.

Every engine roll restamped `engine.version`, so validate reported the pin current while five of
six deployments ran packs months behind the library (one of them on six composed packs). These
tests build a throwaway pack repo + deployment and check each answer the detector can give.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from scripts import framework_validate, pack_currency

CHANGELOG = """# Changelog

## 0.4.0 — unreleased

Migration impact: not released yet, never pending.

## 0.3.0 — 2026-09-20

- Rename `technique` to `method`.

Migration impact: breaking — retype: technique pages to method.

## 0.2.1 — 2026-09-10

Migration impact: none — runtime compatibility release only.

## 0.2.0 — 2026-09-01

Migration impact: an old change the deployment already has.

## 0.1.0 — 2026-08-01

Migration impact: an even older change, also installed.
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def _pack(root: Path, name: str, version: str, changelog: str = "") -> None:
    d = root / "packs" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(f"name: {name}\nversion: {version}\n", encoding="utf-8")
    if changelog:
        (d / "CHANGELOG.md").write_text(changelog, encoding="utf-8")


@pytest.fixture
def library(tmp_path):
    """A pack repo whose origin/main has alpha 0.3.0 -- and a checkout left on an OLD branch."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _pack(upstream, "alpha", "0.3.0", CHANGELOG)
    _pack(upstream, "beta", "0.1.0")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "packs")
    checkout = tmp_path / "library"
    subprocess.run(["git", "clone", "-q", str(upstream), str(checkout)], check=True)
    _git(checkout, "checkout", "-q", "-b", "stale-branch")
    _pack(checkout, "alpha", "0.1.0")  # the working tree says 0.1.0; origin/main says 0.3.0
    return checkout


def _deployment(tmp_path: Path, name: str, version: str, composed: dict | None = None,
                state: dict | None = None) -> Path:
    dep = tmp_path / "deploy"
    dep.mkdir()
    (dep / "pack.yaml").write_text(f"name: {name}\nversion: {version}\n", encoding="utf-8")
    domains = dep / ".okengine" / "installed-domains"
    domains.mkdir(parents=True)
    for pack, ver in (composed or {}).items():
        (domains / f"{pack}.json").write_text(json.dumps({"pack": pack, "pack_version": ver}))
    if state is not None:
        (dep / ".okengine" / "migrations-state.json").write_text(json.dumps(state))
    return dep


def test_without_configured_sources_the_answer_is_undetectable_never_current(tmp_path):
    dep = _deployment(tmp_path, "alpha", "0.3.0")
    findings = pack_currency.assess(dep, [])
    assert [lvl for lvl, _ in findings] == ["warn"]
    assert findings[0][1].startswith("UNDETECTABLE: no pack sources configured")


def test_a_pack_behind_its_source_names_the_span_and_only_its_migration_impacts(tmp_path, library):
    dep = _deployment(tmp_path, "alpha", "0.2.0")
    [(level, message)] = pack_currency.assess(dep, [str(library)])
    assert level == "warn"
    assert "alpha (host) is BEHIND: installed 0.2.0, source 0.3.0" in message
    assert "0.3.0: breaking — retype: technique pages to method" in message
    assert "0.2.1: none" in message
    assert "already has" not in message, "0.2.0 is installed, so its impact is not pending"
    assert "even older" not in message, "releases before the installed one are not pending"
    assert "not released yet" not in message, "only releases up to the source version count"


def test_the_source_is_the_fetched_main_not_the_checkouts_working_tree(tmp_path, library):
    """The first live run read a library checkout left on an old branch and called a current
    pack AHEAD of it; the source is origin/main, and the finding names that commit."""
    dep = _deployment(tmp_path, "alpha", "0.3.0")
    [(level, message)] = pack_currency.assess(dep, [str(library)])
    assert level == "ok", message
    assert "alpha (host) 0.3.0 matches its source" == message


def test_a_pack_ahead_of_its_source_is_flagged_for_upstreaming(tmp_path, library):
    dep = _deployment(tmp_path, "beta", "0.1.1")
    [(level, message)] = pack_currency.assess(dep, [str(library)])
    assert level == "warn" and "beta (host) is AHEAD of its source" in message
    sha = subprocess.run(["git", "-C", str(library), "rev-parse", "origin/main"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert re.search(rf"@origin/main {sha[:8]} \d{{4}}-\d{{2}}-\d{{2}}\)", message), \
        "the finding names the exact commit (8-char sha) and its date"


def test_the_migration_state_record_wins_over_pack_yaml(tmp_path, library):
    """A reconcile can land the new pack.yaml while its migrations are still pending; the state
    record is the installed version (framework_upgrade.installed_pack_version)."""
    dep = _deployment(tmp_path, "alpha", "0.3.0", state={"pack_versions": {"alpha": "0.2.1"}})
    [(level, message)] = pack_currency.assess(dep, [str(library)])
    assert level == "warn" and "installed 0.2.1, source 0.3.0" in message, message


def test_composed_packs_are_each_checked_and_an_unknown_one_is_undetectable(tmp_path, library):
    dep = _deployment(tmp_path, "alpha", "0.3.0", composed={"beta": "0.0.9", "private-x": "1.0.0"})
    findings = dict((msg.split(" ", 1)[0].rstrip(":"), (lvl, msg))
                    for lvl, msg in pack_currency.assess(dep, [str(library)]))
    assert findings["alpha"][0] == "ok"
    assert findings["beta"][0] == "warn" and "beta (composed) is BEHIND" in findings["beta"][1]
    assert findings["private-x"] == ("warn",
                                     "private-x: UNDETECTABLE, no configured source knows this pack")


def test_the_first_source_that_knows_a_pack_wins(tmp_path, library):
    """The plain source directory sits INSIDE a git repo on purpose: git must not answer for it."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "enclosing repo")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    private = tmp_path / "private"
    _pack(private, "alpha", "9.9.9")          # a plain directory: read as its working tree
    dep = _deployment(tmp_path, "alpha", "0.3.0")
    [(level, _)] = pack_currency.assess(dep, [str(library), str(private)])
    assert level == "ok"
    [(level, message)] = pack_currency.assess(dep, [str(private), str(library)])
    assert level == "warn" and "source 9.9.9" in message and "(working tree" in message


def test_a_single_pack_repo_and_a_catalog_file_are_sources_too(tmp_path):
    single = tmp_path / "single"
    single.mkdir()
    (single / "pack.yaml").write_text("name: solo\nversion: 0.2.0\n", encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"packs": [{"name": "listed", "version": "1.2.0"}]}))
    dep = _deployment(tmp_path, "solo", "0.2.0", composed={"listed": "1.1.0"})
    findings = pack_currency.assess(dep, [str(single), str(catalog)])
    assert findings[0] == ("ok", "solo (host) 0.2.0 matches its source")
    assert findings[1][0] == "warn" and "listed (composed) is BEHIND: installed 1.1.0, source 1.2.0" \
        in findings[1][1]


def test_an_unparseable_version_is_undetectable(tmp_path, library):
    dep = _deployment(tmp_path, "alpha", "latest")
    [(level, message)] = pack_currency.assess(dep, [str(library)])
    assert level == "warn" and message.startswith("alpha: UNDETECTABLE, unparseable version")


def test_validate_reports_the_finding_as_a_warning(tmp_path, library, monkeypatch):
    monkeypatch.setenv(pack_currency.SOURCES_ENV, str(library))
    dep = _deployment(tmp_path, "alpha", "0.2.0")
    report = framework_validate.Report()
    framework_validate.check_engine_version(dep, report)
    currency = [row for row in report.rows if row[1] == "pack currency"]
    assert [row[0] for row in currency] == ["WARN"], report.rows
    assert "alpha (host) is BEHIND" in currency[0][2]


def test_malformed_records_and_sources_degrade_to_undetectable_never_a_crash(tmp_path, monkeypatch):
    dep = tmp_path / "deploy"
    domains = dep / ".okengine" / "installed-domains"
    domains.mkdir(parents=True)
    (dep / "pack.yaml").write_text("name: [unclosed\n", encoding="utf-8")   # unparseable: no host
    (domains / "gamma.json").write_text("{not json", encoding="utf-8")      # named by its file
    source = tmp_path / "source"
    _pack(source, "gamma", "0.1.0")
    (source / "packs" / "broken").mkdir()
    (source / "packs" / "broken" / "pack.yaml").write_text("name: [x\n", encoding="utf-8")
    (source / "packs" / "nameless").mkdir()
    (source / "packs" / "nameless" / "pack.yaml").write_text("version: 1.0.0\n", encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"packs": [{"version": "1.0.0"}]}))  # an entry with no name
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))          # git unavailable: working tree
    findings = pack_currency.assess(
        dep, [str(tmp_path / "missing-catalog.json"), str(catalog), str(source)])
    assert findings == [("warn", "gamma: UNDETECTABLE, unparseable version (installed '', "
                                 "source '0.1.0')")], findings


def test_an_unreadable_migration_state_is_a_clean_fail_row_not_a_validate_crash(
        tmp_path, library, monkeypatch):
    """okengine#991: read_state now refuses a torn state file instead of reading it as empty. The
    currency check reads it on every `framework validate` once sources are configured (the fleet
    sets them), so it must report a FAIL row naming the file, and later rows must still run."""
    monkeypatch.setenv(pack_currency.SOURCES_ENV, str(library))
    dep = _deployment(tmp_path, "alpha", "0.3.0")
    (dep / ".okengine" / "migrations-state.json").write_text('{"pack_versions": {"alp')
    (dep / "engine.version").write_text("version: v0.0.1\n")
    report = framework_validate.Report()

    framework_validate.check_engine_version(dep, report)

    currency = [row for row in report.rows if row[1] == "pack currency"]
    assert [row[0] for row in currency] == ["FAIL"], report.rows
    assert "migrations-state.json" in currency[0][2] and "snapshot" in currency[0][2]
    assert any(row[1] == "engine.version" for row in report.rows), "later rows did not run"
