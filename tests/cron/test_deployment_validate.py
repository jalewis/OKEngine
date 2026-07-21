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


def test_hermes_pin_one_sided_warns_not_silently_passes(tmp_path, monkeypatch):
    """M22 one-sided-drift rule on the hermes_pin leg (sibling of the engine_release leg): an
    older/partial ensure-runtime stamp can carry engine_release but NO hermes_pin (the key postdates
    okengine#119). engine.version still pins a Hermes, so `hp and hr and hp != hr` short-circuits to
    a silent PASS — a stale Hermes pin sails through the pin-drift gate. It must WARN 'undetectable',
    never pass clean (invariant-audit completeness sweep)."""
    f = _run_check(tmp_path, monkeypatch,
                   {"version": "v0.9.0", "hermes_pin": "v2026.7.1"},   # engine.version pins a Hermes
                   "engine_release: v0.9.0\n")                          # stamp has NO hermes_pin key
    assert any(lvl == "WARN" and "hermes_pin" in msg and "undetectable" in msg
               for lvl, _, msg in f), f
    assert not any(lvl == "FAIL" for lvl, _, msg in f), f               # not a FAIL — it's undetectable


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


def _run_runtime_ownership(tmp_path, monkeypatch, dirs, euid, plant_jobs_json=False):
    """Drive check_runtime_ownership: create the given /opt/data subdirs (owned by the real test
    uid) and monkeypatch os.geteuid so the check sees `euid` as the lane uid. euid != real uid
    simulates the muddle (runtime tree owned by someone the lane isn't). plant_jobs_json also lays
    down cron-plus/jobs.json (okengine#193 — the critical file the dir-level check misses)."""
    data = tmp_path / "data"
    for rel in dirs:
        (data / rel).mkdir(parents=True, exist_ok=True)
    if plant_jobs_json:
        (data / "cron-plus").mkdir(parents=True, exist_ok=True)
        (data / "cron-plus" / "jobs.json").write_text('{"jobs": []}')
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


def test_runtime_ownership_covers_qmd_and_state(tmp_path, monkeypatch):  # invariant-audit M-B4.3
    """qmd (search index bind-source) and state/ join the runtime tree — a root-recreated one is
    unwritable by the lane uid and silently kills the index rebuild / stateful lanes, exactly like
    jobs.json. The ownership check must cover them, not just cron-plus/scripts/config."""
    import os
    f = _run_runtime_ownership(tmp_path, monkeypatch, ["qmd", "state"], euid=os.getuid() + 1)
    assert any(lvl == "FAIL" and "qmd" in msg for lvl, _, msg in f), f
    assert any("state" in msg for _, _, msg in f), f


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


def test_stale_composed_schema_artifact_fails(tmp_path):  # invariant-audit #12
    """A governing artifact that differs from fresh source composition blocks validation."""
    schema = "types:\n  entity: {required: [type, name]}\npartitioning: {namespaces: {entities: {}}}\n"
    stale = "types:\n  STALE_ONLY_TYPE: {}\npartitioning: {namespaces: {}}\n"
    f = _run_schema_check(tmp_path, schema, stale)
    assert any(lvl == "FAIL" and "DIVERGES" in msg and "composed-schema" in msg
               for lvl, _, msg in f), f


def test_schema_document_comparison_is_exact_and_ignores_generated_metadata(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    fresh = {"types": {"source": {}}, "field_enums": {"status": {"enum": "status"}}}
    same = {**fresh, "_generated": "test", "_fragments": [["ext:x", {}]]}
    assert m._schema_documents_equal(fresh, same)
    changed = {**same, "field_enums": {"status": {"enum": "other"}}}
    assert not m._schema_documents_equal(fresh, changed)
    removed = {"types": {"source": {}}, "_generated": "test"}
    assert not m._schema_documents_equal(fresh, removed)


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


def test_partition_tombstoned_copy_not_a_dup(tmp_path, monkeypatch):
    """A tombstoned page (e.g. a same-story dedup loser left at its old shard path with
    superseded_by) is intentionally superseded — it inflates no count and must NOT be flagged
    as a #54 live duplicate. Only >=2 LIVE copies of a slug are a dup."""
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "schema.yaml").write_text(_CVE_SCHEMA)
    # same slug at two shard paths, but one is tombstoned -> exactly one LIVE -> not a dup
    (vault / "wiki" / "cves" / "2026" / "07").mkdir(parents=True)
    (vault / "wiki" / "cves" / "2026" / "07" / "CVE-2026-45659.md").write_text(
        "---\ntype: cve\n---\n# live\n")
    (vault / "wiki" / "cves" / "2026").mkdir(parents=True, exist_ok=True)
    (vault / "wiki" / "cves" / "2026" / "CVE-2026-45659.md").write_text(
        "---\ntype: cve\nstatus: tombstoned\nsuperseded_by: cves/2026/07/CVE-2026-45659\n---\n# dead\n")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT = vault
    m.check_partition_dups()
    assert [x for x in m.F if x[1] == "partition-dups"] == []
    # sanity: two LIVE copies of the same slug still FAIL
    (vault / "wiki" / "cves" / "2026" / "CVE-2026-45659.md").write_text("---\ntype: cve\n---\n# live2\n")
    m.F.clear()
    m.check_partition_dups()
    assert [x for x in m.F if x[1] == "partition-dups"], "two LIVE copies must still FAIL"


def test_stale_stamp_selfheals_against_running_engine(tmp_path, monkeypatch):
    """okengine#192: an image roll without a re-stamp leaves the runtime stamp behind the RUNNING
    engine, so About reports the wrong version. check_pins compares the stamp to the baked
    $HERMES/.okengine_release, refreshes the stamp (About corrects on next read), and WARNs so the
    missing re-stamp in the roll is visible."""
    vault = tmp_path / "vault"; data = tmp_path / "data"; hermes = tmp_path / "hermes"
    (vault / "wiki").mkdir(parents=True); data.mkdir(); hermes.mkdir()
    (vault / "engine.version").write_text("engine: okengine\nversion: v0.10.3\nhermes_pin: v2026.7.1\n")
    (data / "engine-runtime.yaml").write_text(              # STALE stamp (rolled without re-stamp)
        "engine_release: v0.9.1\nhermes_pin: v2026.7.1\nhermes_sha: abc\nengine_sha: def\n")
    (hermes / ".okengine_release").write_text("v0.10.3\n")  # the RUNNING engine, baked in the image

    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec); sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear(); m.VAULT, m.DATA, m.HERMES = vault, data, hermes
    m.check_pins()

    # stamp self-healed to the running engine (so the About panel now reads v0.10.3)
    assert "engine_release: v0.10.3" in (data / "engine-runtime.yaml").read_text()
    # the desync is surfaced (a roll skipped the re-stamp), and it's a WARN not a FAIL (it fixed it)
    warns = [f for f in m.F if f[0] == "WARN" and "auto-refreshed" in f[2]]
    assert len(warns) == 1, m.F
    # ...and no engine.version-vs-stamp FAIL, because after the heal the stamp matches the pin's series
    assert not [f for f in m.F if f[0] == "FAIL" and f[1] == "pins"], m.F


def test_stale_hermes_pin_selfheals_against_baked_marker(tmp_path, monkeypatch):
    """The HERMES half of okengine#192, found live on the v0.18.2 canary: a Hermes-bump image roll
    without a re-stamp left About claiming the OLD Hermes and NOTHING validated it (the engine check
    only covers engine_release). check_pins must compare the stamp's hermes_pin to the baked
    $HERMES/.hermes_pin, self-heal, and WARN — exactly like the engine half."""
    vault = tmp_path / "vault"; data = tmp_path / "data"; hermes = tmp_path / "hermes"
    (vault / "wiki").mkdir(parents=True); data.mkdir(); hermes.mkdir()
    (vault / "engine.version").write_text("engine: okengine\nversion: v0.10.9\nhermes_pin: v2026.7.1\n")
    (data / "engine-runtime.yaml").write_text(              # STALE hermes_pin (canary roll, no re-stamp)
        "engine_release: v0.10.9\nhermes_pin: v2026.7.1\nhermes_sha: abc\nengine_sha: def\n")
    (hermes / ".okengine_release").write_text("v0.10.9\n")
    (hermes / ".hermes_pin").write_text("v2026.7.7.2\n")    # the RUNNING Hermes, baked in the image

    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec); sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear(); m.VAULT, m.DATA, m.HERMES = vault, data, hermes
    m.check_pins()

    # stamp self-healed (About now reports the Hermes actually running)
    assert "hermes_pin: v2026.7.7.2" in (data / "engine-runtime.yaml").read_text()
    warns = [f for f in m.F if f[0] == "WARN" and "auto-refreshed" in f[2] and "Hermes" in f[2]]
    assert len(warns) == 1, m.F
    # NOTE: the pack's engine.version still pins the OLD hermes — that residual hp!=hr FAIL is the
    # separate, pre-existing pack-pin check doing its job (the pack must re-validate + bump), so we
    # only assert the STAMP was healed here.


def test_no_hermes_pin_marker_is_silent(tmp_path, monkeypatch):
    """A pre-marker image (no baked .hermes_pin) must not warn/heal — undetectable is skip, and the
    stamp is left alone."""
    vault = tmp_path / "vault"; data = tmp_path / "data"; hermes = tmp_path / "hermes"
    (vault / "wiki").mkdir(parents=True); data.mkdir(); hermes.mkdir()
    (vault / "engine.version").write_text("engine: okengine\nversion: v0.10.9\nhermes_pin: v2026.7.1\n")
    (data / "engine-runtime.yaml").write_text("engine_release: v0.10.9\nhermes_pin: v2026.7.1\n")
    (hermes / ".okengine_release").write_text("v0.10.9\n")   # no .hermes_pin baked
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec); sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear(); m.VAULT, m.DATA, m.HERMES = vault, data, hermes
    m.check_pins()
    assert "hermes_pin: v2026.7.1" in (data / "engine-runtime.yaml").read_text()
    assert not [f for f in m.F if "Hermes" in f[2] and "auto-refreshed" in f[2]], m.F


def test_stamp_matching_running_engine_is_silent(tmp_path, monkeypatch):
    """No desync -> no refresh, no WARN (a correctly re-stamped deploy is quiet)."""
    vault = tmp_path / "vault"; data = tmp_path / "data"; hermes = tmp_path / "hermes"
    (vault / "wiki").mkdir(parents=True); data.mkdir(); hermes.mkdir()
    (vault / "engine.version").write_text("engine: okengine\nversion: v0.10.3\nhermes_pin: v2026.7.1\n")
    (data / "engine-runtime.yaml").write_text("engine_release: v0.10.3\nhermes_pin: v2026.7.1\n")
    (hermes / ".okengine_release").write_text("v0.10.3\n")
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec); sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear(); m.VAULT, m.DATA, m.HERMES = vault, data, hermes
    m.check_pins()
    assert not [f for f in m.F if "auto-refreshed" in f[2]], m.F


# --- okengine#193 shift-left: catch a mis-owned cron-plus/jobs.json (the fleet-stall poison) as a
#     first-class deployment-validate invariant, and run the whole self-check DAILY not weekly. -----

def test_runtime_ownership_flags_misowned_jobs_json(tmp_path, monkeypatch):
    """A cron-plus/jobs.json whose owner != the lane uid (root:0600 from a deploy without the pack's
    HERMES_UID — the fleet-stall poison) is FAILed with the #193 diagnostic. The dir-level check alone
    missed this: a mis-owned FILE inside a correctly-owned dir went dark silently."""
    import os
    f = _run_runtime_ownership(tmp_path, monkeypatch, [], euid=os.getuid() + 1, plant_jobs_json=True)
    assert [x for x in f if x[0] == "FAIL" and "jobs.json" in x[2] and "193" in x[2]], f


def test_deployment_validate_runs_daily_not_weekly():
    """Shift-left: a weekly cadence let a contract violation (version desync, mis-owned jobs.json)
    sit stale in fleet health for up to a week. deployment-validate must run every day."""
    import json as _json
    crons = _json.loads((REPO / "config" / "engine-crons.json").read_text())
    jobs = crons["jobs"] if isinstance(crons, dict) else crons
    dv = next(j for j in jobs if j.get("name") == "deployment-validate")
    dow = dv["schedule"]["expr"].split()[4]
    assert dow == "*", f"deployment-validate must run daily (day-of-week '*'), got {dv['schedule']['expr']!r}"


# --- invariant-audit #17: the type_alias SHADOW/target branch — the validator's headline drift --

def test_type_alias_shadow_fails(tmp_path):
    """A root type_alias whose KEY is also a composed canonical type is the drift this module was
    built to catch: normalization drains silently retype those pages. Must FAIL and name the type.
    (No test covered this branch before — a refactor that mis-sourced the shadow check would have
    gone green.)"""
    schema = ("types:\n  company: {required: [type, name]}\n"
              "type_aliases: {company: vendor}\n"
              "partitioning: {namespaces: {}}\n")
    f = _run_schema_check(tmp_path, schema, schema)
    assert any(lvl == "FAIL" and "SHADOWS" in msg and "company" in msg
               for lvl, _, msg in f), f


def test_type_alias_target_not_a_type_warns(tmp_path):
    """An alias pointing at a target that is NOT a composed type is a dangling remap — WARN."""
    schema = ("types:\n  entity: {required: [type, name]}\n"
              "type_aliases: {org: no_such_type}\n"
              "partitioning: {namespaces: {}}\n")
    f = _run_schema_check(tmp_path, schema, schema)
    assert any(lvl == "WARN" and "no_such_type" in msg and "not a composed type" in msg
               for lvl, _, msg in f), f


def test_type_alias_pointing_at_real_type_is_clean(tmp_path):
    """A well-formed alias (key not a type, target IS a composed type) is the intended use — no
    shadow FAIL, no dangling-target WARN."""
    schema = ("types:\n  entity: {required: [type, name]}\n  vendor: {required: [type, name]}\n"
              "type_aliases: {company: vendor}\n"
              "partitioning: {namespaces: {}}\n")
    f = _run_schema_check(tmp_path, schema, schema)
    assert not any(lvl in ("FAIL", "WARN") and ("SHADOWS" in msg or "not a composed type" in msg)
                   for lvl, _, msg in f), f


# --- invariant-audit #8: check_extensions must not FALSE-FAIL a no-lane (sidecar/panels/schema)
#     enabled extension that legitimately stages no /opt/data/scripts/<id>/ dir. ----------------

def _run_extensions_check(tmp_path, enabled, jobs, staged_dirs):
    """Drive check_extensions: enable `enabled` ids in .okengine/extensions.yaml, lay down a
    jobs.json (`jobs`), and materialize the given staged /opt/data/scripts/<id>/ dirs."""
    import json as _json
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    (vault / ".okengine").mkdir(parents=True)
    (data / "scripts").mkdir(parents=True)
    (data / "cron-plus").mkdir(parents=True)
    (vault / ".okengine" / "extensions.yaml").write_text(
        yaml.safe_dump({"enabled": {e: {} for e in enabled}}))
    (data / "cron-plus" / "jobs.json").write_text(_json.dumps({"jobs": jobs}))
    for sd in staged_dirs:
        (data / "scripts" / sd).mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA = vault, data
    m.check_extensions()
    return list(m.F)


def _ext_job(ext_id, fname="run.py"):
    """A synthesized extension cron job — script under <SCRIPTS_ROOT>/<id>/ (extension_compose)."""
    return {"id": ext_id, "name": ext_id, "enabled": True,
            "script": f"/opt/data/scripts/{ext_id}/{fname}"}


def test_panels_only_extension_no_scripts_dir_is_clean(tmp_path):
    """A schema-fragment-only / panels-only / sidecar extension stages NO *.py, so
    deploy-cron-scripts creates no dir and synthesizes no cron lane. It must NOT be FAILed —
    the false positive would ERROR the whole lane and bury real findings."""
    f = _run_extensions_check(tmp_path, enabled=["okengine.embeddings"], jobs=[], staged_dirs=[])
    assert not any(lvl == "FAIL" for lvl, _, _ in f), f


def test_extension_with_lane_but_no_staged_dir_fails(tmp_path):
    """A genuinely dead lane: the extension DID synthesize a cron job pointing at its scripts dir,
    but the dir was never staged. That is a real FAIL."""
    f = _run_extensions_check(tmp_path, enabled=["okengine.glossary"],
                              jobs=[_ext_job("okengine.glossary", "glossary_refresh.py")],
                              staged_dirs=[])
    assert any(lvl == "FAIL" and "okengine.glossary" in msg for lvl, _, msg in f), f


def test_extension_with_lane_and_staged_dir_is_clean(tmp_path):
    """Lane synthesized AND its scripts dir staged — the healthy in-gateway extension. No finding."""
    f = _run_extensions_check(tmp_path, enabled=["okengine.glossary"],
                              jobs=[_ext_job("okengine.glossary", "glossary_refresh.py")],
                              staged_dirs=["okengine.glossary"])
    assert not any(lvl == "FAIL" for lvl, _, _ in f), f


def _load(tmp_path, monkeypatch):
    """Import deployment_validate to exercise its pure helpers (env set so module import succeeds)."""
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    return m


def test_staleness_subset_ignores_extension_additions(tmp_path, monkeypatch):
    """The composed artifact (base⊕pack⊕extensions) must NOT read as STALE just because it carries
    extension-owned types/namespaces/enum-values the base⊕pack recompose lacks — the bug that
    false-flagged every lacuna/frontier deployment. Only MISSING/DISAGREEING pack governance is stale.
    """
    m = _load(tmp_path, monkeypatch)
    f = m._artifact_missing_pack_governance
    live = {"types": {"actor": {"required": ["type"]}}, "enums": {"tlp": ["clear", "amber"]}}
    # artifact is a SUPERSET: an extension added `lacuna` + extended the tlp enum
    disk = {"types": {"actor": {"required": ["type"]}, "lacuna": {"required": ["type"]}},
            "enums": {"tlp": ["clear", "amber", "red"]}}
    assert f(live.get("types"), disk.get("types")) is False      # extra ext type -> not stale
    assert f(live.get("enums"), disk.get("enums")) is False       # extended enum -> not stale


def test_staleness_subset_catches_real_drift(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    f = m._artifact_missing_pack_governance
    live = {"actor": {"required": ["type", "aliases"]}}
    assert f(live, {"actor": {"required": ["type"]}}) is True     # artifact DISAGREES on a pack type
    assert f(live, {}) is True                                    # artifact MISSING a pack type
    assert f({"tlp": ["clear", "amber"]}, {"tlp": ["clear"]}) is True  # pack enum value dropped in artifact
    assert f({"x": 1}, {"x": 1, "y": 2}) is False                 # pure superset scalar -> not stale


def _load_dv(monkeypatch, vault, data, hermes):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA, m.HERMES = vault, data, hermes
    return m


def _write_path_dirs(tmp_path):
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    hermes = tmp_path / "h"; (hermes / "scripts" / "cron").mkdir(parents=True)
    (hermes / "config").mkdir(parents=True)
    data = tmp_path / "d"; (data / "scripts").mkdir(parents=True)
    (data / "config").mkdir(parents=True)
    for name in ("schema_lib.py", "id_lib.py", "id_index.py", "converge.py"):
        (hermes / "scripts" / "cron" / name).write_text("v1")
        (data / "scripts" / name).write_text("v1")
    (hermes / "config" / "base-schema.yaml").write_text("shape: v1\n")   # baked base-schema
    (data / "config" / "base-schema.yaml").write_text("shape: v1\n")     # staged, in sync
    return vault, hermes, data


def test_write_path_libs_drift_fails(tmp_path, monkeypatch):
    """The write path imports schema_lib/id_lib/id_index/converge from the BAKED /opt/hermes/scripts/
    cron; a stage-only deploy leaves it stale. check_write_path_libs must FAIL on baked!=staged."""
    vault, hermes, data = _write_path_dirs(tmp_path)
    (data / "scripts" / "schema_lib.py").write_text("STAGED-NEWER")   # staged diverged from baked
    m = _load_dv(monkeypatch, vault, data, hermes)
    m.check_write_path_libs()
    assert any(l == "FAIL" and a == "write-path" and "schema_lib.py" in msg for l, a, msg in m.F), m.F


def test_write_path_base_schema_drift_fails(tmp_path, monkeypatch):  # invariant-audit M5
    """The write server also validates against the engine base-schema, loaded from the BAKED
    HERMES/config — same stale-vs-staged trap as the libs. A field-shape change staged but not
    rebuilt must FAIL, not ship green."""
    vault, hermes, data = _write_path_dirs(tmp_path)
    (data / "config" / "base-schema.yaml").write_text("shape: v2  # staged newer\n")   # diverged from baked
    m = _load_dv(monkeypatch, vault, data, hermes)
    m.check_write_path_libs()
    assert any(l == "FAIL" and a == "write-path" and "base-schema.yaml" in msg
               for l, a, msg in m.F), m.F


def test_write_path_libs_match_pass(tmp_path, monkeypatch):
    vault, hermes, data = _write_path_dirs(tmp_path)
    m = _load_dv(monkeypatch, vault, data, hermes)
    m.check_write_path_libs()
    assert not any(l == "FAIL" for l, _, _ in m.F), m.F


def test_write_path_libs_missing_dir_warns_not_silent_pass(tmp_path, monkeypatch):
    vault, hermes, data = _write_path_dirs(tmp_path)
    m = _load_dv(monkeypatch, vault, data, tmp_path / "no-such-hermes")   # baked tree absent
    m.check_write_path_libs()
    assert any(l == "WARN" and a == "write-path" for l, a, _ in m.F), m.F   # undetectable, not a pass
    assert not any(l == "FAIL" for l, _, _ in m.F)


def test_write_path_lib_present_one_side_warns_not_silent(tmp_path, monkeypatch):  # invariant-audit M22
    """A lib present baked-only or staged-only (e.g. a new _WRITE_PATH_LIBS entry that a rebuild or a
    stage didn't carry across) is REAL drift — the write path may be running a lib the fleet can't
    see. The old `if not (baked and staged): continue` silently skipped it (a per-file vacuous pass).
    It must WARN (undetectable), never nothing."""
    vault, hermes, data = _write_path_dirs(tmp_path)
    (data / "scripts" / "id_index.py").unlink()                # staged copy gone, baked present
    m = _load_dv(monkeypatch, vault, data, hermes)
    m.check_write_path_libs()
    assert any(l == "WARN" and a == "write-path" and "id_index.py" in msg and "MISSING" in msg
               for l, a, msg in m.F), m.F
    assert not any(l == "FAIL" for l, _, _ in m.F)             # one-sided is undetectable, not a spurious FAIL


def test_write_path_base_schema_present_one_side_warns(tmp_path, monkeypatch):  # invariant-audit M22
    """Same one-sided-drift hole for the baked base-schema: present on only one side is undetectable,
    not a pass."""
    vault, hermes, data = _write_path_dirs(tmp_path)
    (data / "config" / "base-schema.yaml").unlink()            # staged gone, baked present
    m = _load_dv(monkeypatch, vault, data, hermes)
    m.check_write_path_libs()
    assert any(l == "WARN" and a == "write-path" and "base-schema.yaml" in msg and "MISSING" in msg
               for l, a, msg in m.F), m.F


def test_write_path_libs_pins_write_server_imports():  # invariant-audit M1/M23
    """_WRITE_PATH_LIBS is a hand-copy of the libs write_server imports from the BAKED tree; when it
    drifts, check_write_path_libs silently stops guarding a lib (id_index was ADDED to write_server's
    imports and nearly slipped past this list — the cross-surface contract that already broke once).
    Bind them: every LOCAL module write_server imports must be either a tracked write-path lib OR an
    explicitly MCP-image-only module (baked into the image, never staged to the cron tree, so it has
    no baked-vs-staged drift surface). And no dead entries the server no longer imports."""
    import ast
    ws_src = (REPO / "okengine-mcp" / "write_server.py").read_text()
    local_dirs = [REPO / "scripts" / "cron", REPO / "okengine-mcp"]
    imported = set()
    for n in ast.walk(ast.parse(ws_src)):
        # cover both `import X` and `from X import ...` (a 5th lib pulled in via from-import must not
        # slip the pin — the round-2 re-verify flagged the Import-only scan as bypassable)
        mods = []
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods = [n.module]
        for mod in mods:
            if "." in mod or mod == "write_server":
                continue
            if any((d / f"{mod}.py").is_file() for d in local_dirs):
                imported.add(f"{mod}.py")
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    dv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dv)
    tracked = set(dv._WRITE_PATH_LIBS)
    # Image-only: baked beside write_server.py (okengine-mcp/) and imported from its OWN dir, NEVER
    # staged to scripts/cron — so no baked-vs-staged drift pair exists to compare. converge.py is here
    # (not scripts/cron; no cron script imports it) and so is scope.py. A change to either needs an
    # image rebuild (caught by the version stamp), but it must NOT be in _WRITE_PATH_LIBS or the drift
    # check hits the both-absent branch and silently never compares it (invariant-audit M23).
    IMAGE_ONLY = {"scope.py", "converge.py"}
    unclassified = imported - tracked - IMAGE_ONLY
    assert not unclassified, (
        f"write_server imports {sorted(unclassified)} but they are neither in _WRITE_PATH_LIBS "
        f"(baked-vs-staged drift-checked) nor the image-only allowlist — classify each (is it staged "
        f"to scripts/cron, or baked-only in okengine-mcp?) or the drift check silently skips it.")
    dead = tracked - imported
    assert not dead, f"_WRITE_PATH_LIBS lists {sorted(dead)} which write_server no longer imports"
    # every tracked lib must actually live in scripts/cron (the staged tree) — an image-only lib parked
    # here (the converge misclassification) has no staged copy and would be a permanent both-absent skip
    for lib in tracked:
        assert (REPO / "scripts" / "cron" / lib).is_file(), (
            f"_WRITE_PATH_LIBS entry {lib} is not in scripts/cron — it has no staged copy to drift "
            f"against, so it belongs in the image-only set, not the drift check")


def test_report_write_failure_delivers_diagnosis_not_crash(tmp_path, monkeypatch, capsys):
    """When the report file is foreign-owned/unwritable — the exact condition check_ownership catches —
    main() must not crash on its OWN output; it prints the remedy to stderr and still emits findings."""
    vault = tmp_path / "v"; (vault / "wiki" / "operational").mkdir(parents=True)
    data = tmp_path / "d"; data.mkdir()
    (vault / "wiki" / "operational" / "deployment-validation.md").mkdir()   # a dir -> write_text raises
    m = _load_dv(monkeypatch, vault, data, tmp_path / "h")
    rc = m.main()                                        # must NOT raise
    assert isinstance(rc, int)
    assert "fix-vault-ownership.sh" in capsys.readouterr().err


def test_check_auth_fails_private_exposed_without_password(tmp_path, monkeypatch):
    """L4: the in-gateway security gate must FAIL a private vault bound beyond loopback with no
    password. It had no failing-case test."""
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    m = _load_dv(monkeypatch, vault, tmp_path / "d", tmp_path / "h")
    monkeypatch.setenv("OKENGINE_TRUST", "private")
    monkeypatch.setenv("OKENGINE_BIND", "0.0.0.0")
    monkeypatch.delenv("OKENGINE_READER_PASSWORD", raising=False)
    monkeypatch.delenv("API_SERVER_ENABLED", raising=False); monkeypatch.delenv("API_SERVER_KEY", raising=False)
    m.check_auth()
    assert any(l == "FAIL" and a == "auth" for l, a, _ in m.F), m.F


def test_check_auth_passes_with_password(tmp_path, monkeypatch):
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    m = _load_dv(monkeypatch, vault, tmp_path / "d", tmp_path / "h")
    monkeypatch.setenv("OKENGINE_TRUST", "private")
    monkeypatch.setenv("OKENGINE_BIND", "0.0.0.0")
    monkeypatch.setenv("OKENGINE_READER_PASSWORD", "secret")
    monkeypatch.delenv("API_SERVER_ENABLED", raising=False); monkeypatch.delenv("API_SERVER_KEY", raising=False)
    m.check_auth()
    assert not any(a == "auth" for _, a, _ in m.F), m.F


def _auth_env(monkeypatch):
    monkeypatch.setenv("OKENGINE_TRUST", "public")   # isolate the api_server check from the bind rule
    monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")
    monkeypatch.delenv("API_SERVER_ENABLED", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)


def test_check_auth_fails_chat_enabled_with_unlocked_api_server_toolset(tmp_path, monkeypatch):  # invariant-audit HIGH
    """A config.yaml seeded before the v0.10.7 lockdown that later enables Agent Chat inherits the
    broad default toolset (terminal/code_execution/...). The gate must FAIL it."""
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    data = tmp_path / "d"; data.mkdir()
    (data / "config.yaml").write_text("model: {default: x}\n")   # NO platform_toolsets.api_server
    m = _load_dv(monkeypatch, vault, data, tmp_path / "h")
    _auth_env(monkeypatch); monkeypatch.setenv("API_SERVER_ENABLED", "true")
    m.check_auth()
    assert any(l == "FAIL" and a == "auth" and "api_server" in msg for l, a, msg in m.F), m.F


def test_check_auth_fails_non_allowlisted_api_server_toolset(tmp_path, monkeypatch):
    """Allowlist, not blocklist: a dangerous name AND a composite alias that expands to broad tools
    both fail (the alias would slip a dangerous-name blocklist — re-verify)."""
    for toolset in ("[okengine, terminal]", "[okengine, hermes-api-server]"):
        vault = tmp_path / f"v{hash(toolset)}"; (vault / "wiki").mkdir(parents=True)
        data = tmp_path / f"d{hash(toolset)}"; data.mkdir()
        (data / "config.yaml").write_text(
            f"platforms: {{api_server: {{enabled: true}}}}\nplatform_toolsets: {{api_server: {toolset}}}\n")
        m = _load_dv(monkeypatch, vault, data, tmp_path / "h")
        _auth_env(monkeypatch)
        m.check_auth()
        assert any(l == "FAIL" and a == "auth" for l, a, _ in m.F), (toolset, m.F)


def test_check_auth_fails_scalar_api_server_toolset(tmp_path, monkeypatch):
    """A non-list value (scalar/dict) is treated by Hermes as unset -> broad default; must FAIL."""
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    data = tmp_path / "d"; data.mkdir()
    (data / "config.yaml").write_text("platform_toolsets: {api_server: okengine}\n")   # scalar
    m = _load_dv(monkeypatch, vault, data, tmp_path / "h")
    _auth_env(monkeypatch); monkeypatch.setenv("API_SERVER_ENABLED", "true")
    m.check_auth()
    assert any(l == "FAIL" and a == "auth" and "not a list" in msg for l, a, msg in m.F), m.F


def test_check_auth_chat_enabled_by_key_alone_is_checked(tmp_path, monkeypatch):
    """Hermes enables chat on API_SERVER_KEY alone — the gate must treat that as enabled too."""
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    data = tmp_path / "d"; data.mkdir()
    (data / "config.yaml").write_text("model: {default: x}\n")   # no lockdown
    m = _load_dv(monkeypatch, vault, data, tmp_path / "h")
    _auth_env(monkeypatch); monkeypatch.setenv("API_SERVER_KEY", "sk-something")
    m.check_auth()
    assert any(l == "FAIL" and a == "auth" for l, a, _ in m.F), m.F


def test_check_auth_passes_locked_api_server_toolset(tmp_path, monkeypatch):
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    data = tmp_path / "d"; data.mkdir()
    (data / "config.yaml").write_text(
        "platform_toolsets: {api_server: [okengine, okengine-write, web]}\n")   # web is allowlisted
    m = _load_dv(monkeypatch, vault, data, tmp_path / "h")
    _auth_env(monkeypatch); monkeypatch.setenv("API_SERVER_ENABLED", "true")
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    m.check_auth()
    assert not any(a == "auth" for _, a, _ in m.F), m.F


def test_check_crons_fails_dup_id_dup_name_missing_script(tmp_path, monkeypatch):
    """L5: check_crons's FAIL branches (dup id/name, dangling script) were untested."""
    import json as _json
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    data = tmp_path / "d"; (data / "cron-plus").mkdir(parents=True); (data / "scripts").mkdir()
    (data / "scripts" / "present.py").write_text("# ok")
    (data / "cron-plus" / "jobs.json").write_text(_json.dumps({"jobs": [
        {"id": "x", "name": "a", "script": "present.py"},
        {"id": "x", "name": "b", "script": "gone.py"},      # dup id + dangling script
        {"id": "y", "name": "a", "script": "present.py"},   # dup name
    ]}))
    m = _load_dv(monkeypatch, vault, data, tmp_path / "h")
    m.check_crons()
    msgs = " | ".join(msg for _, _, msg in m.F)
    assert "duplicate job id x" in msgs, m.F
    assert "duplicate job name a" in msgs, m.F
    assert "missing script gone.py" in msgs, m.F


def test_check_crons_missing_jobs_json_fails(tmp_path, monkeypatch):
    vault = tmp_path / "v"; (vault / "wiki").mkdir(parents=True)
    data = tmp_path / "d"; data.mkdir()
    m = _load_dv(monkeypatch, vault, data, tmp_path / "h")
    m.check_crons()
    assert any(l == "FAIL" and "jobs.json missing" in msg for l, _, msg in m.F), m.F


def test_staged_but_not_folded_warns_even_for_a_core_extension(tmp_path):  # invariant-audit M-B4.2 + re-verify
    """A staged extension lane not folded into jobs.json is WARNed — and, critically, driven off the
    STAGED dirs, NOT the extensions.yaml `enabled` map. A CORE default-on extension (okengine.
    contradictions/timeline) is active + staged yet absent from `enabled`; the old enabled-map loop
    missed exactly that worst case. Here `enabled` is EMPTY, yet the staged, unfolded lane still WARNs."""
    import importlib.util as _il
    import json as _json
    import sys as _sys
    import yaml as _yaml
    vault = tmp_path / "vault"; data = tmp_path / "data"
    (vault / ".okengine").mkdir(parents=True); (data / "cron-plus").mkdir(parents=True)
    (vault / ".okengine" / "extensions.yaml").write_text(_yaml.safe_dump({"enabled": {}}))   # NOT in the map
    (data / "cron-plus" / "jobs.json").write_text(_json.dumps({"jobs": []}))                 # nothing folded
    extd = data / "scripts" / "okengine.contradictions"; extd.mkdir(parents=True)
    (extd / "contradictions_scan.py").write_text("# a core lane script\n")                   # staged lane present
    spec = _il.spec_from_file_location("deployment_validate", MOD)
    m = _il.module_from_spec(spec); _sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m); m.F.clear(); m.VAULT, m.DATA = vault, data
    m.check_extensions()
    assert any(lvl == "WARN" and "okengine.contradictions" in msg and "NO lane" in msg
               for lvl, _, msg in m.F), list(m.F)
    assert not any(lvl == "FAIL" for lvl, _, _ in m.F), "staged-but-not-folded is a WARN, not a FAIL"


import json as _json197  # noqa: E402  (this file predates the json need; local alias avoids clashes)

# --- okengine#197: cron store ownership + stall sentinel -------------------------------------

def _run_cron_check(tmp_path, monkeypatch, *, owner_uid=None, sentinel=None):
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    (vault / "wiki").mkdir(parents=True)
    cp = data / "cron-plus"
    cp.mkdir(parents=True)
    (cp / "jobs.json").write_text(_json197.dumps({"jobs": [{"id": "a1", "name": "j1"}]}))
    if sentinel is not None:
        (cp / ".scheduler-stalled").write_text(_json197.dumps(sentinel))
    monkeypatch.setenv("WIKI_PATH", str(vault))
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT, m.DATA = vault, data
    if owner_uid is not None:                 # simulate a foreign-owned store without needing root
        real_stat = type(cp / "jobs.json").stat

        class _St:
            def __init__(self, st): self._st, self.st_uid = st, owner_uid
            def __getattr__(self, k): return getattr(self._st, k)
        monkeypatch.setattr(type(cp / "jobs.json"), "stat",
                            lambda self, **kw: _St(real_stat(self, **kw)))
    m.check_crons()
    return list(m.F)


def test_cron_store_foreign_owner_fails(tmp_path, monkeypatch):
    """okengine#197: jobs.json owned by root (a docker-exec write) silently kills every lane —
    the validator must go RED, not rely on an operator noticing frozen last_run_at."""
    f = _run_cron_check(tmp_path, monkeypatch, owner_uid=0)
    assert any(l == "FAIL" and "owned by uid 0" in msg for l, c, msg in f), f


def test_cron_store_own_uid_passes(tmp_path, monkeypatch):
    f = _run_cron_check(tmp_path, monkeypatch)
    assert not any(l == "FAIL" and "owned by uid" in msg for l, c, msg in f), f


def test_scheduler_stalled_sentinel_fails(tmp_path, monkeypatch):
    """The cron-plus .scheduler-stalled sentinel (dropped when the store is unreadable) must be
    a FAIL with the recorded cause — silence never reads as healthy."""
    f = _run_cron_check(tmp_path, monkeypatch,
                        sentinel={"error": "Permission denied: jobs.json", "at": "2026-07-08T01:00:00Z"})
    assert any(l == "FAIL" and "STALLED" in msg and "Permission denied" in msg for l, c, msg in f), f


def _run_provenance_check(monkeypatch, pack):
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    if pack is None:
        monkeypatch.delenv("OKENGINE_PACK", raising=False)
    else:
        monkeypatch.setenv("OKENGINE_PACK", pack)
    m.check_provenance_env()
    return list(m.F)


def test_provenance_env_unset_warns(monkeypatch):
    """invariant-audit M14/#750: no OKENGINE_PACK in the gateway env -> the write path stamps no
    composition provenance. Undetectable at runtime, so the in-gateway validator must WARN."""
    f = _run_provenance_check(monkeypatch, None)
    assert any(l == "WARN" and c == "provenance" and "OKENGINE_PACK" in msg for l, c, msg in f), f


def test_provenance_env_set_is_clean(monkeypatch):
    f = _run_provenance_check(monkeypatch, "okpack-example")
    assert not any(c == "provenance" for l, c, msg in f), f


# --- invariant-audit v0.11.5 batch-5 (cron scripts) --------------------------------------------

def _load_dv5(tmp_path, vault):
    spec = importlib.util.spec_from_file_location("deployment_validate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_validate"] = m
    spec.loader.exec_module(m)
    m.F.clear()
    m.VAULT = vault
    return m


def test_check_ownership_flags_stray_directory(tmp_path, monkeypatch):  # invariant-audit #8
    """A root-owned DIRECTORY (from a bare root docker exec) is unwritable by the lane uid — the
    atomic-write pattern can't create pages in it — but the old file-only check skipped dirs and went
    green. check_ownership must flag stray dirs too."""
    vault = tmp_path / "vault"
    (vault / "wiki" / "notes" / "sub").mkdir(parents=True)          # a nested dir
    (vault / "wiki" / "notes" / "page.md").write_text("p")         # a file
    m = _load_dv5(tmp_path, vault)
    monkeypatch.setattr(m.os, "geteuid", lambda: m.os.getuid() + 99999)  # every path reads as foreign
    m.check_ownership()
    f = list(m.F)
    assert any(l == "FAIL" and c == "ownership" and "dir" in msg for l, c, msg in f), f


def test_check_rules_flags_duplicate_rule_ids(tmp_path):  # invariant-audit #21 (was untested)
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    (vault / "config" / "rules.yaml").write_text(
        "rules:\n  - {id: r1}\n  - {id: r1}\n  - {id: r2}\n")
    m = _load_dv5(tmp_path, vault)
    m.check_rules()
    assert any(l == "FAIL" and c == "rules" and "r1" in msg for l, c, msg in m.F), list(m.F)


def test_check_subdomains_warns_on_typeless_subdomain(tmp_path):  # invariant-audit #21 (was untested)
    vault = tmp_path / "vault"
    sub = vault / "wiki" / "research"
    sub.mkdir(parents=True)
    (sub / "schema.yaml").write_text("types: {}\n")
    m = _load_dv5(tmp_path, vault)
    m.check_subdomains()
    assert any(l == "WARN" and c == "sub-domains" and "no types" in msg for l, c, msg in m.F), list(m.F)


def test_tz_remediation_mentions_nulling_next_run_at(tmp_path, monkeypatch):  # invariant-audit #53
    """The TZ remediation must tell the operator to null the stale next_run_at — jobs.json persists it
    across the recreate under the OLD tz, and cron-plus only self-heals a NULL next_run_at."""
    f = _run_tz_check(tmp_path, monkeypatch, [_daily("daily-brief")], tz=None)
    assert any(lvl == "WARN" and "next_run_at" in msg for lvl, _, msg in f), f
    f2 = _run_tz_check(tmp_path, monkeypatch, [_daily("daily-brief")], tz="America/New_York",
                       plugin_tz_aware=False)
    assert any(lvl == "FAIL" and "next_run_at" in msg for lvl, _, msg in f2), f2


def test_schema_staleness_compares_base_pack_only_not_recorded_fragments():  # invariant-audit #62
    """The staleness check must recompose base⊕pack (fragments=[]), NOT fragments=None — the latter
    auto-loads the artifact's OWN recorded _fragments, making the comparison circular. Source guard."""
    src = MOD.read_text()
    body = src[src.index("def check_schema"):src.index("def check_subdomains")]
    assert "fragments=[]" in body, "staleness compare must use a base⊕pack recompose (fragments=[])"
