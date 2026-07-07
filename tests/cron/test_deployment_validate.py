"""deployment-validate pin check — the stamp keys must MATCH what ensure-runtime writes.

Born from a live near-miss: the check read `engine`/`hermes` while ensure-runtime.sh
writes `engine_release`/`hermes_pin`, so both comparisons were vacuous and a stale
v0.6.1 deployment pin sailed through weekly validation. These tests pin the real key
contract from BOTH sides: the parser against a stamp in ensure-runtime's exact format,
and drift in either direction FAILing.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "deployment_validate.py"


def _run_check(tmp_path, monkeypatch, pin: dict, stamp: str | None):
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    (vault / "wiki").mkdir(parents=True)
    data.mkdir()
    (vault / "engine.version").write_text(yaml.safe_dump(pin))
    if stamp is not None:
        (data / "engine-runtime.yaml").write_text(stamp)
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_DATA_DIR", str(data))
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA = vault, data
    m.check_pins()
    return list(m.F)


def _stamp(release="v0.9.0", hermes="v2026.7.1"):
    # EXACTLY the format ensure-runtime.sh emits (keys are the contract under test)
    return (f"engine_release: {release}\nhermes_pin: {hermes}\n"
            f"hermes_sha: abc123\nengine_sha: def456\n")


def test_matching_pins_pass(tmp_path, monkeypatch):
    f = _run_check(tmp_path, monkeypatch,
                   {"engine": "okengine", "version": "v0.9.0", "hermes_pin": "v2026.7.1"},
                   _stamp())
    assert f == [], f


def test_engine_series_drift_fails(tmp_path, monkeypatch):
    f = _run_check(tmp_path, monkeypatch,
                   {"version": "v0.6.1", "hermes_pin": "v2026.7.1"}, _stamp())
    assert any(lvl == "FAIL" and "v0.6.1" in msg for lvl, _, msg in f), f


def test_hermes_pin_drift_fails(tmp_path, monkeypatch):
    f = _run_check(tmp_path, monkeypatch,
                   {"version": "v0.9.0", "hermes_pin": "v2026.6.19"}, _stamp())
    assert any(lvl == "FAIL" and "v2026.6.19" in msg for lvl, _, msg in f), f


def test_keyless_stamp_warns_not_silently_passes(tmp_path, monkeypatch):
    """A stamp without the expected keys must WARN 'undetectable', never pass clean —
    the vacuous-comparison regression."""
    f = _run_check(tmp_path, monkeypatch, {"version": "v0.9.0"},
                   "some_other_key: v9.9.9\n")
    assert any(lvl == "WARN" and "undetectable" in msg for lvl, _, msg in f), f


def test_stamp_format_matches_ensure_runtime():
    """Cross-file contract: the keys this validator reads must be the keys
    ensure-runtime.sh actually prints."""
    src = (REPO / "scripts" / "ensure-runtime.sh").read_text()
    for key in ("engine_release", "hermes_pin"):
        assert re.search(rf"printf '{key}: %s", src), \
            f"ensure-runtime.sh no longer writes '{key}:' — update deployment_validate.check_pins"


def _run_crons_check(tmp_path, monkeypatch, jobs):
    import json as _json
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    (data / "cron-plus").mkdir(parents=True, exist_ok=True)
    (data / "scripts").mkdir(exist_ok=True)
    for j in jobs:
        s = j.get("script", "")
        if s and not s.startswith("/"):
            (data / "scripts" / s).write_text("# stub\n")
    (data / "cron-plus" / "jobs.json").write_text(_json.dumps({"jobs": jobs}))
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA = vault, data
    m.check_crons()
    return list(m.F)


def test_backlinks_refresh_no_longer_requires_iwe(tmp_path, monkeypatch):
    """okengine#179: backlinks-refresh builds the graph in-process (no iwe), so an enabled
    backlinks-refresh with no iwe binary must NOT fail (the old #168 dependency is gone)."""
    jobs = [{"id": "007c0b16b658", "name": "backlinks-refresh", "enabled": True,
             "script": "backlinks_refresh.py"}]
    monkeypatch.delenv("IWE_BIN", raising=False)
    f = _run_crons_check(tmp_path, monkeypatch, jobs)
    assert not [x for x in f if x[0] == "FAIL" and "iwe" in x[2]], f


def _run_tz_check(tmp_path, monkeypatch, jobs, tz, plugin_tz_aware=None):
    """Drive check_timezone with a jobs.json, a TZ env (None = unset), and optionally a
    staged cron-plus plugin jobs.py (plugin_tz_aware True/False stages a TZ-aware/UTC-naive
    stub; None stages no plugin)."""
    import json as _json
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    (data / "cron-plus").mkdir(parents=True, exist_ok=True)
    (data / "cron-plus" / "jobs.json").write_text(_json.dumps({"jobs": jobs}))
    if plugin_tz_aware is not None:
        pdir = data / "plugins" / "cron-plus"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "jobs.py").write_text(
            "name = os.environ.get('CRON_TZ') or os.environ.get('TZ')\n"
            if plugin_tz_aware else
            "cron_base = cron_base.replace(tzinfo=timezone.utc)\n")
    monkeypatch.delenv("TZ", raising=False)
    if tz is not None:
        monkeypatch.setenv("TZ", tz)
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA = vault, data
    m.check_timezone()
    return list(m.F)


def _daily(name, expr="30 7 * * *"):
    return {"id": name, "name": name, "enabled": True,
            "schedule": {"expr": expr}}


def test_tz_unset_with_daily_brief_warns(tmp_path, monkeypatch):
    """okengine#177 structural catch: no TZ + a fixed-hour daily lane = briefs fire in
    UTC, not the operator's local morning. Must WARN (UTC is legit, so not a FAIL)."""
    f = _run_tz_check(tmp_path, monkeypatch, [_daily("daily-brief")], tz=None)
    assert any(lvl == "WARN" and "UTC" in msg and "daily-brief" in msg
               for lvl, _, msg in f), f


def test_tz_utc_literal_with_daily_brief_warns(tmp_path, monkeypatch):
    f = _run_tz_check(tmp_path, monkeypatch, [_daily("daily-brief")], tz="UTC")
    assert any(lvl == "WARN" for lvl, _, msg in f), f


def test_real_tz_with_daily_brief_is_clean(tmp_path, monkeypatch):
    """A real local zone + a TZ-aware plugin honors the local hour — nothing to flag."""
    f = _run_tz_check(tmp_path, monkeypatch, [_daily("daily-brief")],
                      tz="America/New_York", plugin_tz_aware=True)
    assert f == [], f


def test_real_tz_but_stale_utc_naive_plugin_fails(tmp_path, monkeypatch):
    """The stale-pin regression: TZ is set but the installed cron-plus is UTC-naive, so it
    silently ignores TZ and briefs run in UTC. Explicit intent violated -> FAIL, not WARN."""
    f = _run_tz_check(tmp_path, monkeypatch, [_daily("daily-brief")],
                      tz="America/New_York", plugin_tz_aware=False)
    assert any(lvl == "FAIL" and "UTC-naive" in msg and "daily-brief" in msg
               for lvl, _, msg in f), f


def test_real_tz_unknown_plugin_does_not_false_fail(tmp_path, monkeypatch):
    """If the plugin isn't readable we can't tell it's stale — don't manufacture a FAIL
    (check_crons/post-deploy already cover a missing plugin)."""
    f = _run_tz_check(tmp_path, monkeypatch, [_daily("daily-brief")],
                      tz="America/New_York", plugin_tz_aware=None)
    assert not [x for x in f if x[0] == "FAIL"], f


def test_tz_unset_but_no_daily_lane_is_clean(tmp_path, monkeypatch):
    """Interval/hourly lanes are TZ-agnostic in effect — don't warn on a UTC deployment
    that has no fixed-hour daily brief."""
    hourly = {"id": "x", "name": "raw-drain", "enabled": True,
              "schedule": {"expr": "17 */2 * * *"}}
    f = _run_tz_check(tmp_path, monkeypatch, [hourly], tz=None)
    assert f == [], f


def test_disabled_daily_lane_does_not_warn(tmp_path, monkeypatch):
    j = _daily("daily-brief")
    j["enabled"] = False
    f = _run_tz_check(tmp_path, monkeypatch, [j], tz=None)
    assert f == [], f


def _run_runtime_ownership(tmp_path, monkeypatch, dirs, euid):
    """Drive check_runtime_ownership: create the given /opt/data subdirs (owned by the real test
    uid) and monkeypatch os.geteuid so the check sees `euid` as the lane uid. euid != real uid
    simulates the muddle (runtime tree owned by someone the lane isn't)."""
    data = tmp_path / "data"
    for rel in dirs:
        (data / rel).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("os.geteuid", lambda: euid)
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA = tmp_path / "vault", data
    m.check_runtime_ownership()
    return list(m.F)


def test_runtime_dir_wrong_owner_fails(tmp_path, monkeypatch):
    """The uid muddle: /opt/data owned by a uid the lane isn't -> the ticker can't write
    .tick.lock and the scheduler dies. Must FAIL and name the mis-owned dir."""
    import os
    f = _run_runtime_ownership(tmp_path, monkeypatch,
                               ["cron-plus", "plugins/cron-plus"], euid=os.getuid() + 1)
    assert any(lvl == "FAIL" and "cron-plus" in msg and "not owned by the lane uid" in msg
               for lvl, _, msg in f), f


def test_runtime_dir_correct_owner_is_clean(tmp_path, monkeypatch):
    import os
    f = _run_runtime_ownership(tmp_path, monkeypatch,
                               ["cron-plus", "plugins/cron-plus", "scripts"], euid=os.getuid())
    assert f == [], f


def test_runtime_ownership_no_dirs_does_not_crash(tmp_path, monkeypatch):
    import os
    f = _run_runtime_ownership(tmp_path, monkeypatch, [], euid=os.getuid() + 1)
    assert f == [], f


def test_runtime_ownership_root_lane_is_exempt(tmp_path, monkeypatch):
    """A lane running as root (euid 0) can write any-owner files — no muddle. Don't false-FAIL on
    a tree it doesn't 'own' (the failure mode is a NON-root lane uid that mismatches the tree)."""
    f = _run_runtime_ownership(tmp_path, monkeypatch,
                               ["cron-plus", "plugins/cron-plus"], euid=0)
    assert f == [], f


def _run_schema_check(tmp_path, vault_schema, artifact):
    """Drive check_schema with a vault schema.yaml and an on-disk composed-schema.yaml artifact."""
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    (vault / "wiki").mkdir(parents=True)
    (vault / ".okengine").mkdir()
    (data / "scripts").mkdir(parents=True)
    (vault / "schema.yaml").write_text(vault_schema, encoding="utf-8")
    (vault / ".okengine" / "composed-schema.yaml").write_text(artifact, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA = vault, data
    m.check_schema()
    return list(m.F)


def test_stale_composed_schema_artifact_warns(tmp_path):  # invariant-audit #12
    """The write path prefers the on-disk composed-schema.yaml unconditionally, but only extension
    enable/disable regenerated it — so a schema.yaml edit was silently ignored. check_schema must
    WARN when the artifact drifts from a live recompose."""
    schema = "types:\n  entity: {required: [type, name]}\npartitioning: {namespaces: {entities: {}}}\n"
    stale = "types:\n  STALE_ONLY_TYPE: {}\npartitioning: {namespaces: {}}\n"
    f = _run_schema_check(tmp_path, schema, stale)
    assert any(lvl == "WARN" and "STALE" in msg and "composed-schema" in msg
               for lvl, _, msg in f), f


def test_in_sync_composed_schema_artifact_is_clean(tmp_path):
    """When the artifact matches the live recompose (the fresh state every enable/disable and deploy
    leaves), no drift WARN fires."""
    import importlib.util as _il
    # recompose the same schema live so the artifact is byte-for-byte in sync
    sl_spec = _il.spec_from_file_location("schema_lib", MOD.parent / "schema_lib.py")
    sl = _il.module_from_spec(sl_spec)
    sys.modules["schema_lib"] = sl
    sl_spec.loader.exec_module(sl)
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / ".okengine").mkdir()
    schema = "types:\n  entity: {required: [type, name]}\npartitioning: {namespaces: {entities: {}}}\n"
    (vault / "schema.yaml").write_text(schema, encoding="utf-8")
    live = sl.compose_schema(vault)[0]
    (vault / ".okengine" / "composed-schema.yaml").write_text(yaml.safe_dump(live), encoding="utf-8")
    (tmp_path / "data" / "scripts").mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA = vault, tmp_path / "data"
    m.check_schema()
    assert not any("STALE" in msg for _, _, msg in m.F), list(m.F)


def _run_partition_check(tmp_path, monkeypatch, schema: str, pages: dict):
    """Load the module and run ONLY check_partition_dups against a hand-built vault.
    pages: {wiki-relative-path-without-.md: frontmatter-type}."""
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "schema.yaml").write_text(schema)
    for rel, typ in pages.items():
        p = vault / "wiki" / (rel + ".md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntype: {typ}\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT = vault
    m.check_partition_dups()
    return list(m.F)


_CVE_SCHEMA = ("partitioning:\n  namespaces:\n"
               "    cves: {strategy: by-date, date_field: date_added, reshard_by: year}\n"
               "types:\n  cve: {}\n")


def test_partition_dup_fails(tmp_path, monkeypatch):
    """okengine#54: the same slug at the flat root AND a shard is the double-count bug — a FAIL,
    caught at the earliest gate before it inflates every downstream count."""
    f = _run_partition_check(tmp_path, monkeypatch, _CVE_SCHEMA, {
        "cves/CVE-2026-45659": "cve",            # stale flat copy
        "cves/2026/07/CVE-2026-45659": "cve",    # canonical shard
        "cves/2026/06/CVE-2026-48558": "cve",    # a clean, unique CVE
    })
    fails = [x for x in f if x[0] == "FAIL" and x[1] == "partition-dups"]
    assert len(fails) == 1, f
    assert "CVE-2026-45659" in fails[0][2] and "CVE-2026-48558" not in fails[0][2]


def test_partition_no_dups_passes(tmp_path, monkeypatch):
    """Each slug once (across shards) -> clean, no finding. A flat namespace is exempt entirely."""
    f = _run_partition_check(tmp_path, monkeypatch, _CVE_SCHEMA, {
        "cves/2026/07/CVE-2026-45659": "cve",
        "cves/2026/06/CVE-2026-48558": "cve",
    })
    assert [x for x in f if x[1] == "partition-dups"] == []
