"""okengine#1032 [12] and [17]: who may hold an extension identity-edit grant, and the token store
never outlives the manifest.

[12] The grant was copied verbatim from the extension's own manifest into its token, whatever tier
the extension came from, so an operator drop-in could grant itself review-bypassing identity edits.
Now a pack- or operator-tier grant reaches the token only with a recorded operator acceptance bound
to a digest of the write policy; a first-party (engine-tier) extension holds it by declaration.

[17] The token store is a copy of the manifest that only `extensions enable` refreshed. Deploy now
re-derives it (`extensions reconcile-tokens`), and `--check` fails on drift."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "scripts" / "framework_extensions.py"
DISC = REPO / "scripts" / "extension_discovery.py"
TOK = REPO / "scripts" / "extension_tokens.py"

GRANT = {"fields": ["title", "aliases"]}


@pytest.fixture(autouse=True)
def _engine_root(monkeypatch, tmp_path):
    root = tmp_path / "engine"
    (root / "extensions").mkdir(parents=True)
    monkeypatch.setenv("OKENGINE_ENGINE_ROOT", str(root))
    return root


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _cli():
    m = _load("framework_extensions", CLI)
    # Policy materialization is not under test, and its tier-1 discovery is import-time cached
    # (extension_discovery.ENGINE_ROOT), so it cannot see this test's synthetic engine root.
    m._materialize_policy = lambda pack: []
    return m


def _tok():
    return _load("extension_tokens", TOK)


def _manifest(ext_id, grant=GRANT, paths=("entities/**",)):
    policy = {"rule_id": "engine-authenticated-writer", "operations": ["update"],
              "paths": list(paths), "update_fields": ["*"]}
    if grant is not None:
        policy["identity_edit"] = grant
    return {"id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "sidecar",
            "requires": {"engine": ">=0.3.0"},
            "capabilities": {"read": ["wiki/**"], "write": ["entities/**"], "write_policy": policy},
            "operation": {"schedule": {"kind": "cron", "expr": "@jitter:daily@4"},
                          "entrypoint": {"image": {"registry": f"r/{ext_id}",
                                                   "digest": "sha256:abc"}}}}


def _ext(root: Path, ext_id, **kw) -> Path:
    d = root / ext_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_manifest(ext_id, **kw)), encoding="utf-8")
    return d / "extension.yaml"


def _operator(pack, ext_id, **kw):
    return _ext(pack / ".okengine" / "extensions", ext_id, **kw)


def _rewrite(manifest_path: Path, **kw):
    ext_id = yaml.safe_load(manifest_path.read_text())["id"]
    manifest_path.write_text(yaml.safe_dump(_manifest(ext_id, **kw)), encoding="utf-8")


def _record(pack, ext_id):
    store = json.loads((pack / ".okengine" / "extension-tokens.json").read_text())
    return next(r for r in store["tokens"] if r["ext_id"] == ext_id)


# --- [12] tier and acceptance -------------------------------------------------------------------


@pytest.mark.parametrize("tier_root", ["operator", "pack"])
def test_an_unaccepted_non_first_party_grant_does_not_reach_the_token(tmp_path, capsys, tier_root):
    pack = tmp_path / "pack"
    root = pack / ".okengine" / "extensions" if tier_root == "operator" else pack / "extensions"
    _ext(root, "acme.cleanup")
    assert _cli().main(["enable", str(pack), "acme.cleanup"]) == 0
    capability = _record(pack, "acme.cleanup")["write_capability"]
    assert "identity_edit" not in capability
    assert capability["paths"] == ["entities/**"]           # the rest of the policy still applies
    out = capsys.readouterr().out
    assert "identity changes are held for review" in out
    assert f"framework extensions enable {pack} acme.cleanup --accept-identity-edit" in out


def test_an_accepted_grant_reaches_the_token_and_is_recorded_against_its_digest(tmp_path, capsys):
    pack = tmp_path / "pack"
    _operator(pack, "acme.cleanup")
    assert _cli().main(["enable", str(pack), "acme.cleanup", "--accept-identity-edit"]) == 0
    assert "identity-edit grant accepted: acme.cleanup (sha256:" in capsys.readouterr().out
    assert _record(pack, "acme.cleanup")["write_capability"]["identity_edit"] == GRANT
    disc = _load("extension_discovery", DISC)
    enabled, _ = disc.load_enabled_state(pack)
    accepted = enabled["acme.cleanup"]["identity_edit_accepted"]
    assert accepted["digest"].startswith("sha256:") and accepted["fields"] == ["title", "aliases"]
    resolved, _ = disc.resolve_for_pack(pack)
    assert _tok().is_identity_edit_accepted(enabled["acme.cleanup"], resolved["acme.cleanup"])


@pytest.mark.parametrize("widen", [{"grant": {"fields": ["title", "aliases", "type"]}},
                                   {"paths": ("entities/**", "cves/**")}])
def test_widening_an_accepted_grant_needs_a_new_acceptance(tmp_path, capsys, widen):
    pack = tmp_path / "pack"
    manifest = _operator(pack, "acme.cleanup")
    cli = _cli()
    assert cli.main(["enable", str(pack), "acme.cleanup", "--accept-identity-edit"]) == 0
    _rewrite(manifest, **widen)
    capsys.readouterr()
    assert cli.main(["enable", str(pack), "acme.cleanup"]) == 0      # re-enable: no new acceptance
    assert "identity_edit" not in _record(pack, "acme.cleanup")["write_capability"]
    assert "was accepted for a different write policy" in capsys.readouterr().out
    disc = _load("extension_discovery", DISC)
    first = disc.load_enabled_state(pack)[0]["acme.cleanup"]["identity_edit_accepted"]["digest"]
    assert cli.main(["enable", str(pack), "acme.cleanup", "--accept-identity-edit"]) == 0
    assert "identity_edit" in _record(pack, "acme.cleanup")["write_capability"]
    assert "identity-edit grant accepted: acme.cleanup" in capsys.readouterr().out
    enabled, _ = disc.load_enabled_state(pack)              # still enabled, re-accepted
    assert enabled["acme.cleanup"]["identity_edit_accepted"]["digest"] not in ("", first)


def test_a_first_party_extension_holds_its_reviewed_grant_without_acceptance(tmp_path,
                                                                              _engine_root):
    pack = tmp_path / "pack"
    _ext(_engine_root / "extensions", "okengine.curate")
    assert _cli().main(["enable", str(pack), "okengine.curate"]) == 0
    assert _record(pack, "okengine.curate")["write_capability"]["identity_edit"] == GRANT


def test_an_extension_without_a_grant_needs_no_acceptance(tmp_path, capsys):
    pack = tmp_path / "pack"
    _operator(pack, "acme.plain", grant=None)
    assert _cli().main(["enable", str(pack), "acme.plain", "--accept-identity-edit"]) == 0
    assert "identity_edit" not in _record(pack, "acme.plain")["write_capability"]
    disc = _load("extension_discovery", DISC)
    enabled, _ = disc.load_enabled_state(pack)
    assert "identity_edit_accepted" not in enabled["acme.plain"]
    assert "identity_edit" not in capsys.readouterr().out


def test_the_digest_covers_the_extension_and_its_whole_write_policy():
    tok = _tok()
    policy = _manifest("acme.x")["capabilities"]["write_policy"]
    base = tok.write_policy_digest("acme.x", policy)
    assert base == tok.write_policy_digest("acme.x", dict(reversed(list(policy.items()))))
    assert base != tok.write_policy_digest("acme.y", policy)
    assert base != tok.write_policy_digest("acme.x", {**policy, "types": ["actor"]})


@pytest.mark.parametrize("settings", [None, "yes", {}, {"identity_edit_accepted": "yes"},
                                      {"identity_edit_accepted": {"digest": "sha256:0"}},
                                      {"identity_edit_accepted": {"digest": "sha256:" + "f" * 64}}])
def test_a_missing_or_malformed_acceptance_accepts_nothing(settings):
    tok = _tok()
    record = {"id": "acme.x", "tier": "operator", "manifest": _manifest("acme.x")}
    capability, warnings = tok.effective_write_capability(record, settings)
    assert "identity_edit" not in capability and len(warnings) == 1


# --- [17] deploy reconciles the store against the manifest --------------------------------------


def _enabled_first_party(tmp_path, _engine_root):
    pack = tmp_path / "pack"
    manifest = _ext(_engine_root / "extensions", "okengine.curate")
    assert _cli().main(["enable", str(pack), "okengine.curate"]) == 0
    return pack, manifest


def test_a_grant_the_manifest_dropped_is_removed_from_the_store_on_deploy(tmp_path, capsys,
                                                                         _engine_root):
    pack, manifest = _enabled_first_party(tmp_path, _engine_root)
    before = _record(pack, "okengine.curate")
    _rewrite(manifest, grant=None)
    cli = _cli()
    capsys.readouterr()
    assert cli.main(["reconcile-tokens", str(pack), "--check"]) == 1
    err = capsys.readouterr().err
    assert "FAIL: token record for 'okengine.curate' was stale" in err and "write_capability" in err
    assert _record(pack, "okengine.curate") == before                  # --check writes nothing
    assert cli.main(["reconcile-tokens", str(pack)]) == 0
    assert "rewritten from the manifest" in capsys.readouterr().out
    after = _record(pack, "okengine.curate")
    assert "identity_edit" not in after["write_capability"]
    assert after["token_sha256"] == before["token_sha256"] and after["status"] == "active"
    assert cli.main(["reconcile-tokens", str(pack), "--check"]) == 0     # converged


def test_scope_drift_is_reconciled_too(tmp_path, _engine_root):
    pack, manifest = _enabled_first_party(tmp_path, _engine_root)
    data = yaml.safe_load(manifest.read_text())
    data["capabilities"]["write"] = ["entities/**", "briefings/**"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert _cli().main(["reconcile-tokens", str(pack)]) == 0
    assert _record(pack, "okengine.curate")["write_scopes"] == ["entities/**", "briefings/**"]


def test_deploy_drops_a_grant_whose_acceptance_no_longer_matches(tmp_path):
    pack = tmp_path / "pack"
    manifest = _operator(pack, "acme.cleanup")
    assert _cli().main(["enable", str(pack), "acme.cleanup", "--accept-identity-edit"]) == 0
    _rewrite(manifest, grant={"fields": ["title", "aliases", "remove"]})   # widened in place
    assert _cli().main(["reconcile-tokens", str(pack)]) == 0
    assert "identity_edit" not in _record(pack, "acme.cleanup")["write_capability"]


def test_a_legacy_record_without_a_write_capability_key_is_not_drift(tmp_path, _engine_root):
    pack = tmp_path / "pack"
    _ext(_engine_root / "extensions", "okengine.plain", grant=None)
    data = yaml.safe_load((_engine_root / "extensions" / "okengine.plain" /
                           "extension.yaml").read_text())
    data["capabilities"].pop("write_policy")
    (_engine_root / "extensions" / "okengine.plain" / "extension.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8")
    assert _cli().main(["enable", str(pack), "okengine.plain"]) == 0
    store_path = pack / ".okengine" / "extension-tokens.json"
    store = json.loads(store_path.read_text())
    for record in store["tokens"]:
        record.pop("write_capability")                    # the pre-write_capability store shape
    store_path.write_text(json.dumps(store))
    assert _cli().main(["reconcile-tokens", str(pack), "--check"]) == 0


def test_a_record_for_an_extension_that_is_not_enabled_is_revoked_on_deploy(tmp_path, capsys,
                                                                            _engine_root):
    """okengine#1042 [20]: it used to be reported and kept, so the credential (with its scopes and
    any identity_edit grant) stayed usable until someone acted on a WARN."""
    pack, _ = _enabled_first_party(tmp_path, _engine_root)
    store_path = pack / ".okengine" / "extension-tokens.json"
    secrets_path = pack / ".okengine" / "extension-secrets.json"
    store = json.loads(store_path.read_text())
    store["tokens"].append({"ext_id": "gone.ext", "token_sha256": "0" * 64, "status": "active",
                            "read_scopes": [], "write_scopes": ["**"]})
    store["tokens"].append("not-a-record")
    store_path.write_text(json.dumps(store))
    secrets_path.write_text(json.dumps({**json.loads(secrets_path.read_text()), "gone.ext": "x"}))
    capsys.readouterr()
    assert _cli().main(["reconcile-tokens", str(pack), "--check"]) == 1
    err = capsys.readouterr().err
    assert "FAIL: token record for 'gone.ext', which is not an enabled extension, is active" in err
    assert "gone.ext" in json.loads(secrets_path.read_text()), "--check writes nothing"
    assert _cli().main(["reconcile-tokens", str(pack)]) == 0
    assert "'gone.ext', which is not an enabled extension, is active; revoked" in \
        capsys.readouterr().out
    tokens = json.loads(store_path.read_text())["tokens"]
    assert not any(isinstance(r, dict) and r.get("ext_id") == "gone.ext" for r in tokens)
    assert "not-a-record" in tokens and _record(pack, "okengine.curate")["status"] == "active"
    assert set(json.loads(secrets_path.read_text())) == {"okengine.curate"}
    assert _cli().main(["reconcile-tokens", str(pack), "--check"]) == 0     # converged


def test_no_store_is_nothing_to_reconcile(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    assert _cli().main(["reconcile-tokens", str(pack), "--check"]) == 0
    assert not (pack / ".okengine" / "extension-tokens.json").exists()


def test_a_discovery_failure_aborts_the_reconcile(tmp_path, capsys):
    pack = tmp_path / "pack"
    _operator(pack, "okengine.squatter")                 # okengine.* is reserved to the engine tier
    state = pack / ".okengine" / "extensions.yaml"
    state.write_text("enabled: [not, a, mapping]\n", encoding="utf-8")   # and an unreadable state
    assert _cli().main(["reconcile-tokens", str(pack)]) == 1
    lines = capsys.readouterr().err.strip().splitlines()
    assert lines[-1] == "token store NOT reconciled (extension discovery failed)"
    assert len(lines) >= 2 and all(line.startswith("FAIL: ") and not line.startswith("FAIL: FAIL")
                                   for line in lines[:-1]), lines


def test_deploy_runs_the_reconcile_and_aborts_when_it_fails():
    deploy = (REPO / "scripts" / "deploy.sh").read_text()
    step = deploy.index('extensions reconcile-tokens "$PACK"')
    assert deploy.index("policy_plane.py\" materialize") < step < deploy.index(
        "extensions sidecar-generate")
    assert "exit 1" in deploy[step:step + 200]


def test_the_digest_format_is_pinned():
    """A recorded acceptance stays valid only while the digest is computed the same way; changing
    the canonical form silently revokes every accepted grant on the next deploy."""
    policy = {"rule_id": "r", "paths": ["entités/**"], "identity_edit": {"fields": ["title"]}}
    assert _tok().write_policy_digest("acme.x", policy) == (
        "sha256:118c8d61d64195e9b97747ed8a24f1cc750b8dadba9a4fed737b978da15cd0a4")


@pytest.mark.parametrize(("tier", "needs"), [("".join(["eng", "ine"]), False), ("pack", True),
                                             ("operator", True), (None, True)])
def test_only_the_engine_tier_holds_a_grant_by_declaration(tier, needs):
    record = {"id": "acme.x", "tier": tier, "manifest": _manifest("acme.x")}
    assert _tok().identity_edit_needs_acceptance(record) is needs


@pytest.mark.parametrize("status", ["revoked", "abandoned"])
def test_an_inactive_orphan_record_is_not_reported(tmp_path, capsys, _engine_root, status):
    pack, _ = _enabled_first_party(tmp_path, _engine_root)
    store_path = pack / ".okengine" / "extension-tokens.json"
    store = json.loads(store_path.read_text())
    store["tokens"].append({"ext_id": "gone.ext", "token_sha256": "0" * 64, "status": status})
    store_path.write_text(json.dumps(store))
    capsys.readouterr()
    assert _cli().main(["reconcile-tokens", str(pack)]) == 0
    assert "gone.ext" not in capsys.readouterr().out


def test_an_orphan_record_does_not_stop_the_reconcile_of_later_records(tmp_path, _engine_root):
    pack, manifest = _enabled_first_party(tmp_path, _engine_root)
    _rewrite(manifest, grant=None)
    store_path = pack / ".okengine" / "extension-tokens.json"
    store = json.loads(store_path.read_text())
    store["tokens"].insert(0, {"ext_id": "aaa.gone", "token_sha256": "0" * 64, "status": "active"})
    store_path.write_text(json.dumps(store))
    disc = _load("extension_discovery", DISC)
    resolved, _ = disc.resolve_for_pack(pack)
    enabled, _ = disc.load_enabled_state(pack)
    drift, _ = _tok().reconcile_store(pack, resolved, enabled)        # writes by default
    assert drift and "identity_edit" not in _record(pack, "okengine.curate")["write_capability"]


def test_an_orphan_record_without_a_status_is_live_and_revoked(tmp_path, capsys, _engine_root):
    """okengine.mcp.scope.resolve treats a record with no `status` as active."""
    pack, _ = _enabled_first_party(tmp_path, _engine_root)
    store_path = pack / ".okengine" / "extension-tokens.json"
    store = json.loads(store_path.read_text())
    store["tokens"].append({"ext_id": "gone.ext", "token_sha256": "0" * 64})
    store["tokens"].append({"token_sha256": "1" * 64})                  # no ext_id at all
    store_path.write_text(json.dumps(store))
    capsys.readouterr()
    assert _cli().main(["reconcile-tokens", str(pack)]) == 0
    out = capsys.readouterr().out
    assert "'gone.ext', which is not an enabled extension, is active; revoked" in out
    assert "'None', which is not an enabled extension, is active; revoked" in out
    assert [r["ext_id"] for r in json.loads(store_path.read_text())["tokens"]] == [
        "okengine.curate"]


# --- okengine#1042 [9]: `extensions disable` revokes a token its extension left behind -------------
# The WARN told the operator to run `disable`, which returned "not enabled ... (nothing to do)" before
# it ever reached the revoke, so the operator believed a still-active credential was gone.

def _orphaned_sidecar(tmp_path):
    """The finding's reproduction: enable a sidecar (token minted), delete its directory, and reset
    extensions.yaml to `enabled: {}` -- the state a pull --update or a hand edit leaves."""
    pack = tmp_path / "pack"
    _operator(pack, "acme.side", grant=None)
    assert _cli().main(["enable", str(pack), "acme.side"]) == 0
    import shutil
    shutil.rmtree(pack / ".okengine" / "extensions" / "acme.side")
    (pack / ".okengine" / "extensions.yaml").write_text("enabled: {}\n", encoding="utf-8")
    return pack


def test_disable_revokes_the_leftover_token_of_an_extension_that_is_not_enabled(tmp_path, capsys):
    pack = _orphaned_sidecar(tmp_path)
    assert _record(pack, "acme.side")["status"] == "active", "fixture: the token is live"
    capsys.readouterr()
    assert _cli().main(["disable", str(pack), "acme.side"]) == 0
    out = capsys.readouterr().out
    assert "not enabled: acme.side; revoked its leftover token" in out, out
    store = json.loads((pack / ".okengine" / "extension-tokens.json").read_text())
    assert not any(r.get("ext_id") == "acme.side" for r in store["tokens"])
    assert "acme.side" not in json.loads(
        (pack / ".okengine" / "extension-secrets.json").read_text())


def test_disable_revokes_a_leftover_plaintext_without_a_store_record(tmp_path, capsys):
    pack = _orphaned_sidecar(tmp_path)
    (pack / ".okengine" / "extension-tokens.json").write_text('{"tokens": []}')
    assert _cli().main(["disable", str(pack), "acme.side"]) == 0
    assert "revoked its leftover token" in capsys.readouterr().out
    assert "acme.side" not in json.loads(
        (pack / ".okengine" / "extension-secrets.json").read_text())


def test_disable_of_an_id_with_no_token_still_has_nothing_to_do(tmp_path, capsys):
    pack = _orphaned_sidecar(tmp_path)
    capsys.readouterr()
    assert _cli().main(["disable", str(pack), "never.was"]) == 0
    assert "not enabled: never.was (nothing to do)" in capsys.readouterr().out
    assert _record(pack, "acme.side")["status"] == "active", "another id's token is untouched"


def test_the_deploy_reconcile_revokes_the_reproduced_orphan(tmp_path, capsys):
    pack = _orphaned_sidecar(tmp_path)
    capsys.readouterr()
    assert _cli().main(["reconcile-tokens", str(pack)]) == 0
    assert "'acme.side', which is not an enabled extension, is active; revoked" in \
        capsys.readouterr().out
    assert json.loads((pack / ".okengine" / "extension-tokens.json").read_text())["tokens"] == []
    assert json.loads((pack / ".okengine" / "extension-secrets.json").read_text()) == {}


def test_reconcile_tokens_runs_as_deploy_sh_runs_it(tmp_path):
    """Real invocation: framework.py from the engine root, no test PYTHONPATH."""
    import os
    import subprocess
    pack = _orphaned_sidecar(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "framework.py"), "extensions",
                        "reconcile-tokens", str(pack)], cwd=REPO, capture_output=True, text=True,
                       timeout=120, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "'acme.side', which is not an enabled extension, is active; revoked" in r.stdout
    assert json.loads((pack / ".okengine" / "extension-tokens.json").read_text())["tokens"] == []


def test_holds_token_matches_the_id_exactly(tmp_path):
    """A record of another id, sorting above or below, is not this id's token; a store record
    alone (no plaintext) is."""
    tok = _tok()
    pack = tmp_path / "pack"
    (pack / ".okengine").mkdir(parents=True)
    store = pack / ".okengine" / "extension-tokens.json"
    store.write_text(json.dumps({"tokens": [{"ext_id": "aaa.other"}, {"ext_id": "zzz.other"},
                                            "not-a-record"]}))
    assert tok.holds_token(pack, "acme.side") is False
    store.write_text(json.dumps({"tokens": [{"ext_id": "acme.side"}]}))
    assert tok.holds_token(pack, "".join(["acme", ".side"])) is True
