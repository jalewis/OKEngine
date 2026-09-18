"""framework backup — create/verify/restore/prune + integrity (okengine#65)."""
import importlib.util
import io
import json
import tarfile
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location("framework_backup", REPO / "scripts" / "framework_backup.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _pack(tmp_path):
    p = tmp_path / "vault"
    (p / "wiki" / "entities" / "a").mkdir(parents=True)
    (p / "wiki" / "entities" / "a" / "acme.md").write_text("# Acme\n")
    (p / "pack.yaml").write_text("name: testpack\n")
    (p / "engine.version").write_text("version: v0.5.0\n")
    (p / ".hermes-data" / "cron-plus").mkdir(parents=True)
    (p / ".hermes-data" / "config.yaml").write_text("model: x\n")
    (p / ".hermes-data" / "cron-plus" / "jobs.json").write_text("[]\n")
    (p / ".hermes-data" / "logs").mkdir()
    (p / ".hermes-data" / "logs" / "big.log").write_text("noise\n" * 500)
    (p / ".git").mkdir(); (p / ".git" / "HEAD").write_text("ref\n")
    (p / ".env").write_text("OPENROUTER_API_KEY=secret\n")
    (p / ".okengine" / "backups").mkdir(parents=True)
    (p / ".okengine" / "backups" / "old.tar.gz").write_text("x")
    return p


def _corrupt_archive(tmp_path):
    """An archive whose MANIFEST claims a sha that doesn't match the file."""
    arc = tmp_path / "bad.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        content = b"hello"
        info = tarfile.TarInfo("wiki/x.md"); info.size = len(content)
        t.addfile(info, io.BytesIO(content))
        man = {"files": {"wiki/x.md": "de" * 32}, "digest": "z", "count": 1, "created": "t"}
        d = json.dumps(man).encode(); mi = tarfile.TarInfo("MANIFEST.json"); mi.size = len(d)
        t.addfile(mi, io.BytesIO(d))
    return arc


# --- scope -------------------------------------------------------------------

def test_iter_files_excludes_runtime_secrets_vcs(tmp_path):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data/cron-plus/jobs.json.bak.lacuna-daily").write_text("old\n")
    (pack / ".hermes-data/cron-plus/jobs.json.pre-lint-crons").write_text("old\n")
    files = {r.as_posix() for r in m.iter_files(pack, include_secrets=False)}
    assert "wiki/entities/a/acme.md" in files            # vault: kept
    assert ".hermes-data/config.yaml" in files           # runtime config: kept
    assert ".hermes-data/cron-plus/jobs.json" in files   # cron state: kept
    assert ".hermes-data/cron-plus/jobs.json.bak.lacuna-daily" not in files
    assert ".hermes-data/cron-plus/jobs.json.pre-lint-crons" not in files
    assert ".git/HEAD" not in files                      # VCS: skipped
    assert ".hermes-data/logs/big.log" not in files      # heavy logs: skipped
    assert ".env" not in files                           # secret: skipped
    assert ".okengine/backups/old.tar.gz" not in files   # backups dir: skipped (no recursion)
    sidecar = pack / "wiki" / "transient.sqlite-journal"
    sidecar.write_bytes(b"transient")
    assert m._excluded(sidecar.relative_to(pack), include_secrets=False)


def test_iter_files_excludes_cron_plus_transient_runtime(tmp_path):  # okengine#326 [26]
    """cron-plus pidfiles + the tick lock are transient scheduler PROCESS state — restoring them
    verbatim permanently orphans a lane (a stale pidfile blocks its re-trigger). They must never enter
    a backup, while the live jobs.json (real schedule state) still does."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data/cron-plus/pids").mkdir(parents=True, exist_ok=True)
    (pack / ".hermes-data/cron-plus/pids/lacuna-daily.pid").write_text("12345\n")
    (pack / ".hermes-data/cron-plus/.tick.lock").write_text("locked\n")
    files = {r.as_posix() for r in m.iter_files(pack, include_secrets=False)}
    assert ".hermes-data/cron-plus/jobs.json" in files                  # real schedule state: kept
    assert ".hermes-data/cron-plus/pids/lacuna-daily.pid" not in files  # transient pidfile: excluded
    assert ".hermes-data/cron-plus/.tick.lock" not in files             # tick lock: excluded


def test_include_secrets_captures_env(tmp_path):
    m = _mod()
    files = {r.as_posix() for r in m.iter_files(_pack(tmp_path), include_secrets=True)}
    assert ".env" in files


def test_default_backup_excludes_generated_extension_credentials(tmp_path):
    """The default archive must not contain either plaintext extension-token surface."""
    m = _mod()
    pack = _pack(tmp_path)
    secrets = pack / ".okengine" / "extension-secrets.json"
    compose = pack / ".okengine" / "generated" / "sidecars.compose.yml"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    compose.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text('{"my-ext":"PLAINTEXT_SECRET_TOKEN_ABC123"}\n')
    compose.write_text("environment:\n  OKENGINE_MY_EXT_TOKEN: PLAINTEXT_SECRET_TOKEN_ABC123\n")

    excluded = {r.as_posix() for r in m.iter_files(pack, include_secrets=False)}
    assert secrets.relative_to(pack).as_posix() not in excluded
    assert compose.relative_to(pack).as_posix() not in excluded

    included = {r.as_posix() for r in m.iter_files(pack, include_secrets=True)}
    assert secrets.relative_to(pack).as_posix() in included
    assert compose.relative_to(pack).as_posix() in included

    archive, _ = m.create(pack, tmp_path / "out", include_secrets=False, stamp="20260803-000000")
    with tarfile.open(archive) as tar:
        assert secrets.relative_to(pack).as_posix() not in tar.getnames()
        assert compose.relative_to(pack).as_posix() not in tar.getnames()


def test_manifest_is_deterministic(tmp_path):
    m = _mod(); p = _pack(tmp_path); f = m.iter_files(p, False)
    assert m.build_manifest(p, f)["digest"] == m.build_manifest(p, f)["digest"]
    assert m.build_manifest(p, f)["count"] == len(f)
    assert m.default_dest(p) == p.parent / "vault-backups"


def test_invalid_sqlite_falls_back_to_plain_bytes(tmp_path):
    m = _mod(); pack = _pack(tmp_path)
    rel = Path("wiki/not-really.sqlite")
    (pack / rel).write_bytes(b"plain bytes")
    assert m._file_bytes(pack, rel, include_secrets=True) == b"plain bytes"


# --- create / verify ---------------------------------------------------------

def test_create_then_verify_ok(tmp_path):
    m = _mod()
    arc, man = m.create(_pack(tmp_path), tmp_path / "bk", False, "20260101T000000Z")
    assert arc.exists()
    ok, man2, probs = m.verify(arc)
    assert ok and not probs and man2["digest"] == man["digest"]
    with tarfile.open(arc) as t:
        names = t.getnames()
    assert "MANIFEST.json" in names and ".env" not in names
    assert man["corpus_epoch"] == 0
    assert man["consistency"]["canonical_markdown_schema_config"] == "corpus-fenced"


def test_create_holds_one_stable_corpus_state_across_all_markdown(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    first = pack / "wiki/a.md"
    second = pack / "wiki/b.md"
    first.write_text("version-1", encoding="utf-8")
    second.write_text("version-1", encoding="utf-8")
    first_read = threading.Event()
    mutation_done = threading.Event()
    original = m._file_bytes

    def mutate_both():
        first_read.wait(2)
        from okengine.corpus_transaction import mutation
        with mutation(pack, writer="test", operation="replace both"):
            first.write_text("version-2", encoding="utf-8")
            second.write_text("version-2", encoding="utf-8")
        mutation_done.set()

    def interleave(pack_arg, rel, include_secrets):
        data = original(pack_arg, rel, include_secrets)
        if rel.as_posix() == "wiki/a.md":
            first_read.set()
            assert not mutation_done.wait(0.1), "writer crossed the backup's stable corpus fence"
        return data

    monkeypatch.setattr(m, "_file_bytes", interleave)
    writer = threading.Thread(target=mutate_both)
    writer.start()
    archive, manifest = m.create(pack, tmp_path / "bk", False, "coherent")
    writer.join(2)
    assert mutation_done.is_set()
    with tarfile.open(archive) as tar:
        assert tar.extractfile("wiki/a.md").read() == b"version-1"
        assert tar.extractfile("wiki/b.md").read() == b"version-1"
    assert manifest["corpus_epoch"] == 0
    assert first.read_text() == second.read_text() == "version-2"


def test_create_lock_timeout_and_interruption_publish_no_archive(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def holder():
        from okengine.corpus_transaction import mutation
        with mutation(pack, writer="test", operation="hold"):
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1)
    with pytest.raises(m.CorpusLockTimeout):
        m.create(pack, tmp_path / "blocked", False, "timeout", lock_timeout_seconds=0.01)
    assert not list((tmp_path / "blocked").iterdir())
    release.set()
    thread.join(2)

    original = m._file_bytes
    monkeypatch.setattr(
        m, "_file_bytes",
        lambda pack_arg, rel, include: (_ for _ in ()).throw(KeyboardInterrupt())
        if rel.as_posix().endswith("acme.md") else original(pack_arg, rel, include),
    )
    with pytest.raises(KeyboardInterrupt):
        m.create(pack, tmp_path / "interrupted", False, "interrupted")
    assert not list((tmp_path / "interrupted").iterdir())


def test_corpus_runtime_lock_markers_are_never_archived(tmp_path):
    m = _mod()
    pack = _pack(tmp_path)
    state = pack / ".okengine/corpus"
    state.mkdir(parents=True, exist_ok=True)
    for name in ("lock", "lock-owner.json", "active.json"):
        (state / name).write_text("transient", encoding="utf-8")
    (state / "epoch").write_text("7\n", encoding="ascii")
    names = {item.as_posix() for item in m.iter_files(pack, False)}
    assert ".okengine/corpus/epoch" in names
    assert not names.intersection({f".okengine/corpus/{name}" for name in (
        "lock", "lock-owner.json", "active.json"
    )})


def test_verify_detects_corruption(tmp_path):
    m = _mod()
    ok, man, probs = m.verify(_corrupt_archive(tmp_path))
    assert not ok and any("checksum" in why for _, why in probs)


@pytest.mark.parametrize(
    ("manifest", "members", "problem"),
    [
        (None, [("MANIFEST.json", None)], "manifest is not a regular file"),
        ({"files": []}, [], "files must be an object"),
        ({"files": {"wiki/x.md": "00"}}, [("wiki/x.md", b"x"), ("wiki/x.md", b"x")],
         "duplicate archive member"),
        ({"files": {"wiki/x.md": "00"}}, [("wiki/x.md", None)], "not a regular file"),
    ],
)
def test_verify_rejects_malformed_and_ambiguous_archive_shapes(
        tmp_path, manifest, members, problem):
    m = _mod()
    archive = tmp_path / f"{problem.replace(' ', '-')}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        if manifest is not None:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("MANIFEST.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    ok, _, problems = m.verify(archive)
    assert not ok
    assert any(problem in detail for _, detail in problems)


def test_create_preserves_mtime(tmp_path):  # invariant-audit MEDIUM (#39)
    """Restore was dating every file to 1970-01-01 (TarInfo.mtime unset), deranging mtime-keyed
    engine lanes. Archived entries must carry the source file's real mtime."""
    import os
    m = _mod()
    p = _pack(tmp_path)
    os.utime(p / "wiki" / "entities" / "a" / "acme.md", (1_780_000_000, 1_780_000_000))
    arc, _ = m.create(p, tmp_path / "bk", False, "20260101T000000Z")
    with tarfile.open(arc) as t:
        info = t.getmember("wiki/entities/a/acme.md")
    assert info.mtime == 1_780_000_000, info.mtime


# --- restore -----------------------------------------------------------------

def test_restore_roundtrip(tmp_path):
    m = _mod()
    arc, _ = m.create(_pack(tmp_path), tmp_path / "bk", False, "20260101T000000Z")
    tgt = tmp_path / "restored"
    ok, man, probs = m.restore(arc, tgt, force=False)
    assert ok
    assert (tgt / "wiki" / "entities" / "a" / "acme.md").read_text() == "# Acme\n"
    assert (tgt / ".hermes-data" / "config.yaml").exists()
    restored_env = (tgt / ".env").read_text()
    assert "OPENROUTER_API_KEY" not in restored_env      # secrets weren't in the backup
    assert "HERMES_UID=" in restored_env                 # non-secret runtime identity was preserved


def test_restore_rearms_archived_cron_schedule_without_erasing_history(tmp_path):
    """An aged backup must not burst-fire every overdue job on the first scheduler tick."""
    m = _mod()
    pack = _pack(tmp_path)
    jobs = [{
        "name": "daily-brief",
        "next_run_at": "2026-01-01T07:00:00-05:00",
        "last_run_at": "2025-12-31T07:00:00-05:00",
        "last_run_success": True,
    }]
    (pack / ".hermes-data/cron-plus/jobs.json").write_text(json.dumps(jobs))
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    target = tmp_path / "restored"

    assert m.restore(archive, target, force=False)[0]
    restored = json.loads((target / ".hermes-data/cron-plus/jobs.json").read_text())
    assert restored[0]["next_run_at"] is None
    assert restored[0]["last_run_at"] == jobs[0]["last_run_at"]
    assert restored[0]["last_run_success"] is True


def test_restore_rebinds_gateway_identity_to_target_owner_without_copying_secrets(tmp_path):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".env").write_text(
        "OPENROUTER_API_KEY=do-not-archive\nHERMES_UID=12001\nHERMES_GID=12002\n"
    )
    archive, manifest = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert manifest["hermes_uid"] == 12001
    assert manifest["hermes_gid"] == 12002
    target = tmp_path / "restored"
    assert m.restore(archive, target, force=False)[0]
    restored_env = (target / ".env").read_text()
    owner = (target / ".hermes-data").stat()
    assert f"HERMES_UID={owner.st_uid}" in restored_env
    assert f"HERMES_GID={owner.st_gid}" in restored_env
    assert "OPENROUTER" not in restored_env
    assert (target / ".env").stat().st_mode & 0o777 == 0o600


def test_deployment_identity_and_restore_manifest_branch_edges(tmp_path):
    m = _mod()
    pack = tmp_path / "pack"
    pack.mkdir()
    uid, gid = m._deployment_identity(pack)
    assert uid == (pack.stat().st_uid or None) and gid == (pack.stat().st_gid or None)

    target = tmp_path / "target"
    target.mkdir()
    m._restore_deployment_identity(target, {})
    assert f"HERMES_UID={target.stat().st_uid}" in (target / ".env").read_text()
    (target / ".env").write_text("EXISTING=yes\n")
    m._restore_deployment_identity(target, {"hermes_uid": 12001, "hermes_gid": "bad"})
    restored = (target / ".env").read_text()
    assert "EXISTING=yes" in restored and f"HERMES_UID={target.stat().st_uid}" in restored
    (target / ".env").unlink()
    m._restore_deployment_identity(target, {"hermes_uid": None, "hermes_gid": 12002})
    assert (target / ".env").read_text().endswith(f"HERMES_GID={target.stat().st_gid}\n")


def test_restore_identity_replaces_duplicate_existing_keys_once(tmp_path):
    m = _mod()
    target = tmp_path / "target"
    runtime = target / ".hermes-data"
    runtime.mkdir(parents=True)
    env_path = target / ".env"
    env_path.write_text(
        "HERMES_UID=old\nKEEP=yes\nHERMES_UID=duplicate\nHERMES_GID=old\n",
        encoding="utf-8",
    )

    m._restore_deployment_identity(target, {})

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines.count(f"HERMES_UID={runtime.stat().st_uid}") == 1
    assert lines.count(f"HERMES_GID={runtime.stat().st_gid}") == 1
    assert "KEEP=yes" in lines
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_restore_rearm_handles_missing_state_and_rejects_invalid_shape(tmp_path):
    m = _mod()
    m._rearm_restored_schedule(tmp_path)
    jobs_path = tmp_path / ".hermes-data/cron-plus/jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text("{}")
    try:
        m._rearm_restored_schedule(tmp_path)
        assert False, "invalid cron state must not be silently restored"
    except ValueError as exc:
        assert "not a job list" in str(exc)
    jobs_path.write_text('["legacy", {"name": "live", "next_run_at": "old"}]')
    m._rearm_restored_schedule(tmp_path)
    jobs = json.loads(jobs_path.read_text())
    assert jobs == ["legacy", {"name": "live", "next_run_at": None}]
    jobs_path.write_text('{"jobs": [{"name": "current", "next_run_at": "old"}]}')
    m._rearm_restored_schedule(tmp_path)
    state = json.loads(jobs_path.read_text())
    assert state == {"jobs": [{"name": "current", "next_run_at": None}]}
    jobs_path.write_text('{"jobs": [], "unexpected": true}')
    with pytest.raises(ValueError, match="not a job list"):
        m._rearm_restored_schedule(tmp_path)


def test_restore_refuses_corrupt_archive(tmp_path):
    m = _mod()
    ok, man, probs = m.restore(_corrupt_archive(tmp_path), tmp_path / "out", force=False)
    assert not ok and probs                              # integrity gate before extract


def test_restore_refuses_nonempty_target(tmp_path):
    m = _mod()
    arc, _ = m.create(_pack(tmp_path), tmp_path / "bk", False, "20260101T000000Z")
    tgt = tmp_path / "t"; tgt.mkdir(); (tgt / "existing").write_text("x")
    try:
        m.restore(arc, tgt, force=False)
        assert False, "expected FileExistsError"
    except FileExistsError:
        pass


# --- prune -------------------------------------------------------------------

def test_prune_keeps_newest_n(tmp_path):
    import os, time
    m = _mod()
    d = tmp_path / "bk"; d.mkdir()
    for i, ts in enumerate(["20260101", "20260102", "20260103", "20260104"]):
        p = d / f"p-{ts}T000000Z.tar.gz"; p.write_text("x")
        os.utime(p, (time.time() + i, time.time() + i))   # mtime order == intended age order
    assert m.prune_backups(d, keep=2, pack_name="p") == 2
    assert sorted(f.name for f in d.glob("*.tar.gz")) == \
        ["p-20260103T000000Z.tar.gz", "p-20260104T000000Z.tar.gz"]


def test_prune_is_pack_scoped_on_a_shared_dest(tmp_path):
    """invariant-audit HIGH #3: two packs sharing one dest — pruning one must NEVER touch the
    other's archives (name-sort put every zeta-* above every alpha-* and deleted alpha's history)."""
    import os, time
    m = _mod()
    d = tmp_path / "shared"; d.mkdir()
    for i in range(4):
        for name in ("alpha", "zeta"):
            p = d / f"{name}-2026010{i}T000000Z.tar.gz"; p.write_text("x")
            os.utime(p, (time.time() + i, time.time() + i))
    removed = m.prune_backups(d, keep=2, pack_name="zeta")
    assert removed == 2
    assert len(list(d.glob("alpha-*.tar.gz"))) == 4, "alpha's DR history must be untouched"
    assert len(list(d.glob("zeta-*.tar.gz"))) == 2


def test_prune_refuses_keep_zero(tmp_path):
    """`--keep 0` used to silently unlink every archive in the dest — a delete-all footgun."""
    m = _mod()
    d = tmp_path / "bk"; d.mkdir()
    (d / "p-20260101T000000Z.tar.gz").write_text("x")
    import pytest
    with pytest.raises(ValueError):
        m.prune_backups(d, keep=0, pack_name="p")
    assert m.main(["prune", str(_pack(tmp_path)), "--dest", str(d), "--keep", "0"]) == 2  # CLI -> exit 2
    assert len(list(d.glob("*.tar.gz"))) == 1                                             # nothing deleted


# --- CLI integration ---------------------------------------------------------

def test_main_create_list_verify(tmp_path, capsys):
    m = _mod(); p = _pack(tmp_path)
    assert m.main(["create", str(p), "--dest", str(tmp_path / "bk")]) == 0
    assert m.main(["list", str(p), "--dest", str(tmp_path / "bk")]) == 0
    out = capsys.readouterr().out
    import re as _re; assert _re.search(r"1 .*backup\(s\) in", out), out
    arc = next((tmp_path / "bk").glob("*.tar.gz"))
    assert m.main(["verify", str(arc)]) == 0


def test_main_create_reports_corpus_lock_timeout(tmp_path, monkeypatch, capsys):
    m = _mod()
    monkeypatch.setattr(
        m, "create", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            m.CorpusLockTimeout("held by writer")
        ),
    )
    assert m.main(["create", str(_pack(tmp_path)), "--dest", str(tmp_path / "bk")]) == 1
    assert "stable corpus state" in capsys.readouterr().err


def test_main_restore_validate_gate_fails(tmp_path, monkeypatch):
    m = _mod()
    arc, _ = m.create(_pack(tmp_path), tmp_path / "bk", False, "20260101T000000Z")
    monkeypatch.setattr(m, "VALIDATOR", lambda t: (False, "stub: invalid"))
    assert m.main(["restore", str(arc), str(tmp_path / "r")]) == 1   # post-restore gate
    monkeypatch.setattr(m, "VALIDATOR", lambda t: (True, "stub: ok"))
    assert m.main(["restore", str(arc), str(tmp_path / "r2")]) == 0


def test_config_yaml_bearer_token_redacted_in_no_secrets_backup(tmp_path):
    """okengine invariant-audit: config.yaml carries a live MCP `Bearer <token>` (ensure-runtime
    writes it). A no-secrets backup must REDACT it (config stays restorable, secret does not leak);
    --include-secrets keeps it verbatim."""
    m = _mod()
    p = _pack(tmp_path)
    (p / ".hermes-data" / "config.yaml").write_text(
        'model: qwen\nokengine:\n  authorization: "Bearer abc123DEADBEEFtoken"\n')
    # no-secrets archive: config.yaml present but token redacted
    arch, _ = m.create(p, tmp_path / "out", include_secrets=False, stamp="20260101-000000")
    with tarfile.open(arch) as t:
        cfg = t.extractfile(".hermes-data/config.yaml").read().decode()
    assert ".hermes-data/config.yaml" in {i.name for i in tarfile.open(arch).getmembers()}  # kept
    assert "abc123DEADBEEFtoken" not in cfg and "<redacted>" in cfg          # token gone
    assert "model: qwen" in cfg                                              # config preserved
    # --include-secrets keeps the real token
    arch2, _ = m.create(p, tmp_path / "out2", include_secrets=True, stamp="20260101-000001")
    with tarfile.open(arch2) as t:
        assert "abc123DEADBEEFtoken" in t.extractfile(".hermes-data/config.yaml").read().decode()


def test_config_yaml_base64_bearer_token_fully_redacted(tmp_path):
    """okengine invariant-audit #11: OKENGINE_MCP_TOKEN is operator-settable, so a STANDARD-base64
    token (`openssl rand -base64`) carries `+` `/` `=`. The redaction char class must cover those in
    ONE match — otherwise only the leading run is redacted and the token tail leaks verbatim into a
    "(secrets excluded)" archive. Worst case: a token whose first run is <6 chars before a `+`/`/`/`=`
    isn't matched at all and the WHOLE secret leaks."""
    m = _mod()
    p = _pack(tmp_path)
    for token in ("Zm9vYmFy+ab/cd==", "ab+cdefghij"):  # padded base64; and <6-char leading run
        (p / ".hermes-data" / "config.yaml").write_text(
            f'model: qwen\nokengine:\n  authorization: "Bearer {token}"\n')
        arch, _ = m.create(p, tmp_path / "out", include_secrets=False, stamp="20260101-000000")
        with tarfile.open(arch) as t:
            cfg = t.extractfile(".hermes-data/config.yaml").read().decode()
        assert token not in cfg, f"token leaked into no-secrets archive: {token!r}"
        # no stray base64 fragment survives after "<redacted>"
        assert "<redacted>" in cfg and "+ab/cd" not in cfg and "cdefghij" not in cfg
        assert "model: qwen" in cfg                                          # config preserved


def test_skeleton_gitignore_excludes_okengine_secrets_keeps_enable_state():
    """okengine invariant-audit: the shipped pack scaffold must gitignore the GENERATED + SECRET
    .okengine/ artifacts (composed-schema, tokens) but keep the committed enable-state/model config."""
    gi = (REPO / "templates" / "pack" / "skeleton" / ".gitignore").read_text()
    # active patterns only — comment lines (which reference the tracked paths for context) don't count
    patterns = {ln.strip() for ln in gi.splitlines() if ln.strip() and not ln.strip().startswith("#")}
    patterns = {p.split("#", 1)[0].strip() for p in patterns}
    for secret in (".okengine/composed-schema.yaml", ".okengine/extension-tokens.json",
                   ".okengine/extension-secrets.json", ".okengine/generated/"):
        assert secret in patterns, f"scaffold .gitignore must exclude {secret}"
    for tracked in (".okengine/extensions.yaml", ".okengine/model-profiles.yaml"):
        assert tracked not in patterns, f"scaffold .gitignore must NOT exclude the committed {tracked}"


# --- invariant-audit v0.11.5 batch-4 ---------------------------------------------------------

def test_sqlite_captured_consistently_and_sidecars_skipped(tmp_path):  # invariant-audit #33/#34
    """A live SQLite index (qmd) must be captured via the online-backup API — a consistent snapshot
    even under concurrent writes — and its transient -wal/-shm sidecars excluded (folded into the
    snapshot). A plain file copy caught torn pages / a db+wal pair that never coexisted, so verify()
    rejected the archive or restore yielded a corrupt index."""
    import sqlite3
    m = _mod()
    p = _pack(tmp_path)
    qdir = p / ".hermes-data" / "qmd" / "cache" / "qmd"
    qdir.mkdir(parents=True)
    db = qdir / "index.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(50)])
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # fold WAL into the main db
    con.close()
    # a leftover transient -wal sidecar must be excluded from the archive (folded into the snapshot)
    (qdir / "index.sqlite-wal").write_bytes(b"")
    files = {r.as_posix() for r in m.iter_files(p, include_secrets=False)}
    assert ".hermes-data/qmd/cache/qmd/index.sqlite" in files
    assert ".hermes-data/qmd/cache/qmd/index.sqlite-wal" not in files      # sidecar excluded
    arc, man = m.create(p, tmp_path / "bk", False, "20260101T000000Z")
    ok, _man, problems = m.verify(arc)
    assert ok, problems                                                     # internally consistent
    # the archived sqlite is a valid, queryable db (WAL folded in)
    out = tmp_path / "restored"
    m.restore(arc, out, force=False)
    rcon = sqlite3.connect(str(out / ".hermes-data" / "qmd" / "cache" / "qmd" / "index.sqlite"))
    assert rcon.execute("SELECT count(*) FROM t").fetchone()[0] == 50
    rcon.close()


def test_qmd_model_cache_excluded_but_index_and_config_kept(tmp_path):
    """Downloaded GGUF/cache payload is rebuildable; the SQLite index and small config are state."""
    m = _mod()
    p = _pack(tmp_path)
    cache = p / ".hermes-data" / "qmd" / "cache" / "qmd"
    cache.mkdir(parents=True)
    (cache / "index.sqlite").write_bytes(b"sqlite placeholder")
    (cache / "models" / "query-expansion.gguf").parent.mkdir()
    (cache / "models" / "query-expansion.gguf").write_bytes(b"large model")
    (cache / "downloads.json").write_text("{}\n")
    config = p / ".hermes-data" / "qmd" / "config" / "collections.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("collections: []\n")

    files = {r.as_posix() for r in m.iter_files(p, include_secrets=False)}

    assert ".hermes-data/qmd/cache/qmd/index.sqlite" in files
    assert ".hermes-data/qmd/config/collections.yaml" in files
    assert ".hermes-data/qmd/cache/qmd/models/query-expansion.gguf" not in files
    assert ".hermes-data/qmd/cache/qmd/downloads.json" not in files


def test_symlinks_excluded_are_warned(tmp_path, capsys):  # invariant-audit #60
    """Symlinked files/dirs are dropped from the archive; the create CLI must WARN so a restore is
    not silently missing content (verify passes only on what WAS captured)."""
    import os
    m = _mod()
    p = _pack(tmp_path)
    target = tmp_path / "external.md"
    target.write_text("# shared\n")
    os.symlink(target, p / "wiki" / "shared.md")
    syms = {r.as_posix() for r in m.skipped_symlinks(p, include_secrets=False)}
    assert "wiki/shared.md" in syms
    os.symlink(target, p / ".git" / "ignored-link.md")
    assert ".git/ignored-link.md" not in {
        r.as_posix() for r in m.skipped_symlinks(p, include_secrets=False)}
    rc = m.main(["create", str(p), "--dest", str(tmp_path / "bk")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "symlink" in err.lower() and "wiki/shared.md" in err


def test_verify_missing_manifest_and_manifest_file(tmp_path):
    m=_mod()
    empty=tmp_path/"empty.tar.gz"
    with tarfile.open(empty,"w:gz"):
        pass
    ok,manifest,problems=m.verify(empty)
    assert not ok and manifest=={} and problems[0][0]=="MANIFEST.json"
    missing=tmp_path/"missing.tar.gz"
    with tarfile.open(missing,"w:gz") as tar:
        data=json.dumps({"files":{"wiki/missing.md":"x"},"count":1}).encode()
        info=tarfile.TarInfo("MANIFEST.json");info.size=len(data);tar.addfile(info,io.BytesIO(data))
    ok,_,problems=m.verify(missing)
    assert not ok and problems==[("wiki/missing.md","missing from archive")]


def test_verify_rejects_unmanifested_member_before_restore(tmp_path):
    """#695: an injected member must not inherit trust from an otherwise valid archive."""
    m = _mod()
    pack = _pack(tmp_path)
    original, _ = m.create(pack, tmp_path / "backups", False, "clean")
    injected = tmp_path / "injected.tar.gz"
    with tarfile.open(original, "r:gz") as source, tarfile.open(injected, "w:gz") as target:
        for member in source.getmembers():
            target.addfile(member, source.extractfile(member))
        payload = b"untrusted\n"
        member = tarfile.TarInfo("wiki/entities/injected.md")
        member.size = len(payload)
        target.addfile(member, io.BytesIO(payload))

    ok, manifest, problems = m.verify(injected)
    assert not ok and manifest["pack"] == pack.name
    assert ("wiki/entities/injected.md", "not declared in manifest") in problems
    restored = tmp_path / "restored"
    ok, _, restore_problems = m.restore(injected, restored, force=False)
    assert not ok and restore_problems == problems
    assert not (restored / "wiki/entities/injected.md").exists()


def test_verify_invalid_gzip_returns_friendly_problem(tmp_path):
    m = _mod()
    archive = tmp_path / "not-a-backup.tar.gz"
    archive.write_text("not gzip", encoding="utf-8")
    ok, manifest, problems = m.verify(archive)
    assert not ok and manifest == {}
    assert problems[0][0] == archive.name
    assert problems[0][1].startswith("unreadable archive:")


def test_backup_misc_edges_and_cli_failures(tmp_path, monkeypatch, capsys):
    m=_mod()
    assert m.list_backups(tmp_path/"absent")==[]
    assert m._human(1)=="1B" and m._human(1024)=="1.0KB" and m._human(1024**5).endswith("TB")
    assert m.main(["create",str(tmp_path/"missing")])==2
    empty=tmp_path/"bad.tar.gz"
    with tarfile.open(empty,"w:gz"):
        pass
    assert m.main(["verify",str(empty)])==2
    bad=_corrupt_archive(tmp_path)
    assert m.main(["verify",str(bad)])==1
    assert m.main(["restore",str(bad),str(tmp_path/"out")])==1
    pack=_pack(tmp_path);arc,_=m.create(pack,tmp_path/"bk",False,"stamp")
    occupied=tmp_path/"occupied";occupied.mkdir();(occupied/"x").write_text("x")
    assert m.main(["restore",str(arc),str(occupied)])==2
    assert m.main(["restore",str(arc),str(tmp_path/"no-validation"),"--no-validate"])==0
    assert m.main(["prune",str(pack),"--dest",str(tmp_path/"bk"),"--keep","1"])==0
    assert "not a valid backup" in capsys.readouterr().err


def test_validator_import_failure_is_nonfatal(tmp_path, monkeypatch):
    m=_mod()
    monkeypatch.setattr(m.importlib.util,"spec_from_file_location",lambda *_:None)
    ok,summary=m._validator(tmp_path)
    assert ok and "validation skipped" in summary


def test_validator_loads_and_runs_framework_validate(tmp_path, monkeypatch):
    from types import SimpleNamespace
    m = _mod()
    loader = SimpleNamespace(exec_module=lambda module: setattr(
        module, "main", lambda argv: 3 if argv == [str(tmp_path), "--quiet"] else 4))
    spec = SimpleNamespace(loader=loader)
    monkeypatch.setattr(m.importlib.util, "spec_from_file_location", lambda *_: spec)
    monkeypatch.setattr(m.importlib.util, "module_from_spec", lambda _spec: SimpleNamespace())
    assert m._validator(tmp_path) == (False, "framework validate → exit 3")
