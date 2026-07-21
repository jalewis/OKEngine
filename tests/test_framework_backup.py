"""framework backup — create/verify/restore/prune + integrity (okengine#65)."""
import importlib.util
import io
import json
import tarfile
from pathlib import Path

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


def test_include_secrets_captures_env(tmp_path):
    m = _mod()
    files = {r.as_posix() for r in m.iter_files(_pack(tmp_path), include_secrets=True)}
    assert ".env" in files


def test_manifest_is_deterministic(tmp_path):
    m = _mod(); p = _pack(tmp_path); f = m.iter_files(p, False)
    assert m.build_manifest(p, f)["digest"] == m.build_manifest(p, f)["digest"]
    assert m.build_manifest(p, f)["count"] == len(f)


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


def test_verify_detects_corruption(tmp_path):
    m = _mod()
    ok, man, probs = m.verify(_corrupt_archive(tmp_path))
    assert not ok and any("checksum" in why for _, why in probs)


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
    assert not (tgt / ".env").exists()                   # secrets weren't in the backup


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
    rc = m.main(["create", str(p), "--dest", str(tmp_path / "bk")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "symlink" in err.lower() and "wiki/shared.md" in err
