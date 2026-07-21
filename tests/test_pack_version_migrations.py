"""Pack-VERSION migrations on update (okengine#312).

Contract under test: `framework pull --update` (and install-domain over an existing member)
computes the pack-version span (installed, incoming], previews the pack's `migrations/m_*.py`
in dry-run by default, and under --apply-migrations runs them through the SAME runner as
engine-pin upgrades — snapshot, state record, roll-forward gate, automatic rollback. The
recorded installed version survives `framework reconcile` accepting pack.yaml.upstream.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "wiki").mkdir(parents=True)
    (v / "pack.yaml").write_text("name: okpack-mig\nversion: 0.1.0\n", encoding="utf-8")
    return v


def _migration(vault: Path, *, id_, frm, to, fail=False, fname=None):
    d = vault / "migrations"
    d.mkdir(exist_ok=True)
    body = (f'ID = "{id_}"\nFROM = "{frm}"\nTO = "{to}"\nDESCRIPTION = "test"\n'
            f'def apply(pack, dry_run):\n'
            f'    if not dry_run:\n'
            f'        (pack / "MIGRATED-{id_}.txt").write_text("done")\n')
    if fail:
        body += '        raise RuntimeError("boom mid-apply")\n'
    body += f'    return ["would write MIGRATED-{id_}.txt"]\n'
    stem = f"m_{frm}_{to}_{id_}".replace(".", "_")
    (d / (fname or f"{stem}.py")).write_text(body, encoding="utf-8")


def _state(vault: Path) -> dict:
    f = vault / ".okengine" / "migrations-state.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


# --- run_pack_migrations (unit) ----------------------------------------------

def test_dry_run_previews_and_floors_the_span(tmp_path, capsys):
    m = _load("framework_upgrade")
    v = _vault(tmp_path)
    _migration(v, id_="mig1", frm="0.1.0", to="0.2.0")
    assert m.run_pack_migrations(v, "okpack-mig", "0.1.0", "0.2.0", apply=False) == 0
    out = capsys.readouterr().out
    assert "would write MIGRATED-mig1.txt" in out and "dry-run" in out
    assert not (v / "MIGRATED-mig1.txt").exists()
    # the FLOOR is recorded so reconcile accepting pack.yaml.upstream can't erase the span
    assert _state(v)["pack_versions"]["okpack-mig"] == "0.1.0"


def test_apply_runs_records_and_is_idempotent(tmp_path, capsys):
    m = _load("framework_upgrade")
    m.VALIDATOR = lambda p: (True, "ok (test)")
    v = _vault(tmp_path)
    _migration(v, id_="mig1", frm="0.1.0", to="0.2.0")
    assert m.run_pack_migrations(v, "okpack-mig", "0.1.0", "0.2.0", apply=True) == 0
    assert (v / "MIGRATED-mig1.txt").read_text() == "done"
    st = _state(v)
    assert st["pack_versions"]["okpack-mig"] == "0.2.0"
    assert "mig1" in st["applied"]
    assert any(h.get("pack") == "okpack-mig" for h in st["history"])
    # idempotent re-update — same recorded version: silent no-op
    (v / "MIGRATED-mig1.txt").unlink()
    assert m.run_pack_migrations(v, "okpack-mig",
                                 m.installed_pack_version(v, "okpack-mig"), "0.2.0",
                                 apply=True) == 0
    assert not (v / "MIGRATED-mig1.txt").exists(), "already-applied migration must not re-run"
    # idempotent even when the CALLER passes a stale installed version (e.g. a lagging
    # manifest): the span re-selects mig1 but the applied-ID set filters it out
    capsys.readouterr()
    assert m.run_pack_migrations(v, "okpack-mig", "0.1.0", "0.2.0", apply=True) == 0
    assert not (v / "MIGRATED-mig1.txt").exists()
    assert "already applied" in capsys.readouterr().out


def test_multi_step_span_applies_in_order(tmp_path):
    m = _load("framework_upgrade")
    m.VALIDATOR = lambda p: (True, "ok (test)")
    v = _vault(tmp_path)
    _migration(v, id_="stepA", frm="0.1.0", to="0.2.0")
    _migration(v, id_="stepB", frm="0.2.0", to="0.3.0")
    assert m.run_pack_migrations(v, "okpack-mig", "0.1.0", "0.3.0", apply=True) == 0
    assert (v / "MIGRATED-stepA.txt").exists() and (v / "MIGRATED-stepB.txt").exists()
    assert _state(v)["pack_versions"]["okpack-mig"] == "0.3.0"


def test_failing_migration_rolls_back_via_snapshot(tmp_path, capsys):
    m = _load("framework_upgrade")
    m.VALIDATOR = lambda p: (True, "ok (test)")
    v = _vault(tmp_path)
    (v / "wiki" / "page.md").write_text("original", encoding="utf-8")
    _migration(v, id_="bad", frm="0.1.0", to="0.2.0", fail=True)
    assert m.run_pack_migrations(v, "okpack-mig", "0.1.0", "0.2.0", apply=True) == 1
    assert "ROLLED BACK" in capsys.readouterr().out
    assert not (v / "MIGRATED-bad.txt").exists(), "added file must be quarantined out"
    assert (v / "wiki" / "page.md").read_text() == "original"
    # state must NOT claim the new version — the span stays pending for a retry
    assert (_state(v).get("pack_versions") or {}).get("okpack-mig") != "0.2.0"


def test_validation_failure_rolls_back_and_keeps_span_pending(tmp_path, capsys):
    m = _load("framework_upgrade")
    m.VALIDATOR = lambda p: (False, "framework validate → exit 1 (test)")
    v = _vault(tmp_path)
    _migration(v, id_="mig1", frm="0.1.0", to="0.2.0")
    assert m.run_pack_migrations(v, "okpack-mig", "0.1.0", "0.2.0", apply=True) == 1
    assert "ROLLED BACK" in capsys.readouterr().out
    assert not (v / "MIGRATED-mig1.txt").exists()
    st = _state(v)
    assert (st.get("pack_versions") or {}).get("okpack-mig") != "0.2.0"
    assert "mig1" not in (st.get("applied") or [])


def test_unknown_installed_version_baselines_never_replays(tmp_path, capsys):
    """A pre-versioning install has no span — NEVER guess one from 0.0.0 (that would replay
    every migration ever shipped onto a live vault). Baseline at incoming, run nothing."""
    m = _load("framework_upgrade")
    v = _vault(tmp_path)
    _migration(v, id_="mig1", frm="0.1.0", to="0.2.0")
    for unknown in (None, "0.0.0"):
        assert m.run_pack_migrations(v, "okpack-mig", unknown, "0.2.0", apply=True) == 0
        assert not (v / "MIGRATED-mig1.txt").exists()
    assert _state(v)["pack_versions"]["okpack-mig"] == "0.2.0"
    assert "baselined" in capsys.readouterr().out


def test_equal_and_downgrade_are_noops(tmp_path, capsys):
    m = _load("framework_upgrade")
    v = _vault(tmp_path)
    _migration(v, id_="mig1", frm="0.1.0", to="0.2.0")
    assert m.run_pack_migrations(v, "okpack-mig", "0.2.0", "0.2.0", apply=True) == 0
    assert m.run_pack_migrations(v, "okpack-mig", "0.3.0", "0.2.0", apply=True) == 0
    assert "downgrade" in capsys.readouterr().out.lower()
    assert not (v / "MIGRATED-mig1.txt").exists()


def test_unparseable_to_version_fails_loud(tmp_path, capsys):
    """okengine#178 for the pack path: a migration whose to_version can't parse must ERROR,
    not be silently dropped from every span."""
    m = _load("framework_upgrade")
    v = _vault(tmp_path)
    _migration(v, id_="bad", frm="0.1.0", to="garbage", fname="m_bad.py")
    assert m.run_pack_migrations(v, "okpack-mig", "0.1.0", "0.2.0", apply=False) == 1
    assert "unparseable" in capsys.readouterr().err


def test_record_false_dry_run_writes_nothing(tmp_path):
    """install-domain's plan mode: the preview must be fully write-free."""
    m = _load("framework_upgrade")
    v = _vault(tmp_path)
    _migration(v, id_="mig1", frm="0.1.0", to="0.2.0")
    assert m.run_pack_migrations(v, "okpack-mig", "0.1.0", "0.2.0",
                                 apply=False, record=False) == 0
    assert not (v / ".okengine" / "migrations-state.json").exists()


def test_changelog_impact_covers_exactly_the_span():
    m = _load("framework_upgrade")
    meta = _load("engine_meta")
    text = ("# Changelog\n\n"
            "## 0.3.0 — 2026-07-19\n- big change\n- Migration impact: re-shelve cves/.\n\n"
            "## 0.2.0 — 2026-07-01\n- schema change, impact line forgotten\n\n"
            "## 0.1.0 — 2026-06-01\n- Migration impact: none — additive only.\n")
    lines = m.changelog_impact(text, "0.1.0", "0.3.0", meta)
    assert len(lines) == 2, lines                     # 0.1.0 is OUTSIDE (installed, incoming]
    assert any(l.startswith("0.3.0:") and "re-shelve" in l for l in lines)
    assert any(l.startswith("0.2.0:") and "no migration-impact line" in l for l in lines)


# --- framework pull --update (end-to-end over a local git repo) ---------------

def _git(args, env):
    subprocess.run(["git", *args], check=True, env=env, capture_output=True)


def _pack_repo(tmp_path: Path):
    """A standalone pack repo at v0.1.0 (root-level pack.yaml — pulled as owner/repo path)."""
    src = tmp_path / "pack-src"
    (src / "wiki").mkdir(parents=True)
    (src / "pack.yaml").write_text("name: okpack-mig\nversion: 0.1.0\ntrust: public\n",
                                   encoding="utf-8")
    (src / "schema.yaml").write_text(yaml.safe_dump(
        {"name": "okpack-mig", "types": {"note": {"required": ["type", "id"]}}}), encoding="utf-8")
    (src / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.0 — 2026-06-01\n- initial\n",
                                      encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    _git(["init", "-q", "-b", "main", str(src)], env)
    _git(["-C", str(src), "add", "-A"], env)
    _git(["-C", str(src), "commit", "-q", "-m", "v0.1.0"], env)
    return src, env


def _bump_upstream(src: Path, env):
    """Ship 0.2.0 upstream: version bump + CHANGELOG section + a pack-version migration."""
    (src / "pack.yaml").write_text("name: okpack-mig\nversion: 0.2.0\ntrust: public\n",
                                   encoding="utf-8")
    (src / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.2.0 — 2026-07-19\n- rework\n"
        "- Migration impact: stamps MIGRATED-e2e.txt.\n\n## 0.1.0 — 2026-06-01\n- initial\n",
        encoding="utf-8")
    d = src / "migrations"
    d.mkdir()
    (d / "m_0_1_0_0_2_0_e2e.py").write_text(
        'ID = "e2e"\nFROM = "0.1.0"\nTO = "0.2.0"\nDESCRIPTION = "e2e marker"\n'
        'def apply(pack, dry_run):\n'
        '    if not dry_run: (pack / "MIGRATED-e2e.txt").write_text("done")\n'
        '    return ["would write MIGRATED-e2e.txt"]\n', encoding="utf-8")
    _git(["-C", str(src), "add", "-A"], env)
    _git(["-C", str(src), "commit", "-q", "-m", "v0.2.0"], env)


def test_pull_update_spans_previews_then_applies(tmp_path, capsys):
    fp = _load("framework_pull")
    src, env = _pack_repo(tmp_path)
    dest = tmp_path / "deploy"
    assert fp.main([str(src), str(dest), "--no-validate",
                    "--catalog", str(tmp_path / "no-catalog.json")]) == 0
    # fresh pull baselines the installed version for future span computation
    assert _state(dest)["pack_versions"]["okpack-mig"] == "0.1.0"
    _bump_upstream(src, env)
    # 1) default = dry-run: migration previewed + changelog impact surfaced, nothing applied
    assert fp.main([str(src), str(dest), "--update", "--no-validate",
                    "--catalog", str(tmp_path / "no-catalog.json")]) == 0
    out = capsys.readouterr().out
    assert "pack version: 0.1.0 → 0.2.0" in out
    assert "stamps MIGRATED-e2e.txt" in out          # CHANGELOG span impact line
    assert "would write MIGRATED-e2e.txt" in out
    assert not (dest / "MIGRATED-e2e.txt").exists()
    # 2) --apply-migrations: runs through the runner (snapshot + state)
    assert fp.main([str(src), str(dest), "--update", "--apply-migrations", "--no-validate",
                    "--catalog", str(tmp_path / "no-catalog.json")]) == 0
    assert (dest / "MIGRATED-e2e.txt").read_text() == "done"
    st = _state(dest)
    assert st["pack_versions"]["okpack-mig"] == "0.2.0" and "e2e" in st["applied"]
    # 3) idempotent re-update: a no-op
    (dest / "MIGRATED-e2e.txt").unlink()
    assert fp.main([str(src), str(dest), "--update", "--apply-migrations", "--no-validate",
                    "--catalog", str(tmp_path / "no-catalog.json")]) == 0
    assert not (dest / "MIGRATED-e2e.txt").exists()


def test_span_survives_reconcile_of_pack_yaml(tmp_path, capsys):
    """THE trap #312's floor-record exists for: after a dry-run update, the operator accepts
    pack.yaml.upstream (new version) via reconcile BEFORE applying migrations. Without the
    floor, the next update sees installed==incoming and silently drops the span."""
    fp = _load("framework_pull")
    src, env = _pack_repo(tmp_path)
    dest = tmp_path / "deploy"
    assert fp.main([str(src), str(dest), "--no-validate",
                    "--catalog", str(tmp_path / "no-catalog.json")]) == 0
    # wipe the fresh-pull baseline to simulate a pre-#312 deployment...
    (dest / ".okengine" / "migrations-state.json").unlink()
    _bump_upstream(src, env)
    assert fp.main([str(src), str(dest), "--update", "--no-validate",
                    "--catalog", str(tmp_path / "no-catalog.json")]) == 0   # dry-run floors it
    assert _state(dest)["pack_versions"]["okpack-mig"] == "0.1.0"
    # ...reconcile: operator accepts the upstream pack.yaml (now says 0.2.0)
    up = dest / "pack.yaml.upstream"
    assert up.is_file()
    up.replace(dest / "pack.yaml")
    capsys.readouterr()
    assert fp.main([str(src), str(dest), "--update", "--apply-migrations", "--no-validate",
                    "--catalog", str(tmp_path / "no-catalog.json")]) == 0
    assert (dest / "MIGRATED-e2e.txt").read_text() == "done", \
        "migration span must survive reconcile rewriting pack.yaml"
    assert _state(dest)["pack_versions"]["okpack-mig"] == "0.2.0"


# --- install-domain over an existing member -----------------------------------

def _load_install_domain():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "framework_install_domain", REPO / "scripts" / "framework_install_domain.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_install_domain"] = m
    spec.loader.exec_module(m)
    return m


def _id_host(tmp_path) -> Path:
    h = tmp_path / "host"
    (h / "wiki").mkdir(parents=True)
    (h / "crons").mkdir()
    (h / "feeds").mkdir()
    (h / "schema.yaml").write_text(yaml.safe_dump(
        {"name": "okpack-host", "types": {"vendor": {"required": ["type", "id", "name"]}}}))
    (h / "pack.yaml").write_text("name: okpack-host\nowns:\n  types: [vendor]\n")
    (h / "crons" / "domain-crons.json").write_text(json.dumps(
        [{"id": "aa", "name": "okpack-host-feed-fetch"}]))
    (h / "crons" / "engine-template-prompts.json").write_text(json.dumps(
        {"daily-brief": "HOST BRIEF PROMPT"}))
    (h / "feeds" / "feeds.opml").write_text('<?xml version="1.0"?><opml><body></body></opml>')
    (h / "CLAUDE.md").write_text("# host persona\n")
    return h


def _id_guest(tmp_path, version="0.1.0") -> Path:
    p = tmp_path / "okpack-tax"
    (p / "subdomain").mkdir(parents=True, exist_ok=True)
    (p / "schema.yaml").write_text(yaml.safe_dump(
        {"name": "okpack-tax", "types": {"intrusion-set": {"required": ["type", "id"]}},
         "partitioning": {"namespaces": {"tax-events": {"strategy": "flat"}}}}))
    (p / "pack.yaml").write_text(
        f"name: okpack-tax\nversion: {version}\nowns:\n  types: [intrusion-set]\n"
        f"  namespaces: [tax-events]\n")
    (p / "subdomain" / "host-schema-additions.yaml").write_text(yaml.safe_dump(
        {"types": {"intrusion-set": {"required": ["type", "id"]}}}))
    (p / "subdomain" / "PERSONA.md").write_text("curation rules for tax\n")
    return p


def test_install_domain_existing_member_runs_guest_migrations(tmp_path, monkeypatch, capsys):
    mid = _load_install_domain()
    orig_load = mid._load_mod

    def _patched_load(filename):
        m2 = orig_load(filename)
        if filename == "framework_upgrade.py":
            m2.VALIDATOR = lambda p: (True, "ok (test)")
        return m2
    monkeypatch.setattr(mid, "_load_mod", _patched_load)

    host, guest = _id_host(tmp_path), _id_guest(tmp_path, "0.1.0")
    assert mid.main([str(host), str(guest), "--apply"]) == 0
    # new member: version baselined in the host's migrations state
    assert _state(host)["pack_versions"]["okpack-tax"] == "0.1.0"

    # upstream ships 0.2.0 with a pack-version migration against the composed vault
    guest = _id_guest(tmp_path, "0.2.0")
    (guest / "migrations").mkdir()
    (guest / "migrations" / "m_0_1_0_0_2_0_tax.py").write_text(
        'ID = "tax-0-2-0"\nFROM = "0.1.0"\nTO = "0.2.0"\nDESCRIPTION = "stamp host"\n'
        'def apply(pack, dry_run):\n'
        '    if not dry_run: (pack / "MIGRATED-tax.txt").write_text("done")\n'
        '    return ["would write MIGRATED-tax.txt"]\n', encoding="utf-8")
    (guest / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.2.0 — 2026-07-19\n- Migration impact: stamps MIGRATED-tax.txt.\n",
        encoding="utf-8")

    # plan mode: preview only, fully write-free
    capsys.readouterr()
    assert mid.main([str(host), str(guest)]) == 0
    out = capsys.readouterr().out
    assert "pack version: 0.1.0 → 0.2.0" in out and "would write MIGRATED-tax.txt" in out
    assert not (host / "MIGRATED-tax.txt").exists()
    assert _state(host)["pack_versions"]["okpack-tax"] == "0.1.0"

    # --apply: migration runs against the HOST vault through the runner
    assert mid.main([str(host), str(guest), "--apply"]) == 0
    assert (host / "MIGRATED-tax.txt").read_text() == "done"
    st = _state(host)
    assert st["pack_versions"]["okpack-tax"] == "0.2.0" and "tax-0-2-0" in st["applied"]
