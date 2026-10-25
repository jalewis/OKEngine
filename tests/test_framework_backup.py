"""framework backup — create/verify/restore/prune + integrity (okengine#65)."""
import importlib.util
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import create_autospec

import pytest

REPO = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location("framework_backup", REPO / "scripts" / "framework_backup.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# The mutation lanes run this file as ROOT. The restore contract under test is the non-root one
# (a root restore is refused by design), so under a real root runner the tests impersonate a
# deployment user: geteuid reports a non-root uid and the trees a restore inspects are chowned to
# it, which is what a non-root extraction produces. Nothing is skipped.
_REAL_ROOT = os.geteuid() == 0
_DEPLOY_ID = 12345


def _as_deploy_user(path):
    if _REAL_ROOT:
        path = Path(path)
        for p in (path, *path.rglob("*")):
            os.chown(p, _DEPLOY_ID, _DEPLOY_ID, follow_symlinks=False)
    return path


@pytest.fixture(autouse=True)
def _non_root_restore_contract(monkeypatch):
    if _REAL_ROOT:
        monkeypatch.setattr(os, "geteuid", lambda: _DEPLOY_ID)
        original = tarfile.TarFile.extractall

        def extractall(self, path=".", *args, **kwargs):
            result = original(self, path, *args, **kwargs)
            _as_deploy_user(path)
            return result

        monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)


@pytest.fixture(autouse=True)
def _tmp_tree_stays_removable(tmp_path):
    """A test (or a mutant of the directory-mode restore) can leave a 0o000 or 0o500 directory
    behind; make the tree writable again so the temp dir, and a mutation worktree holding it,
    can be removed by a non-root user."""
    yield
    for root, dirs, _files in os.walk(tmp_path):
        for name in dirs:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, 0o700)


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


def _settle(pack):
    """Age every file past file_fingerprint.SETTLE_NS so the snapshot hardlinks it (a file written
    within the last clock tick is copied instead)."""
    past = time.time() - 7200
    for path in pack.rglob("*"):
        if path.is_file() and not path.is_symlink():
            os.utime(path, (past, past))


def _publish(path, text):
    """Publish like the enforced write path (integrity._atomic_write_text): a new inode by rename."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_during_archive(m, monkeypatch, pack, trigger, write, *, rounds=1):
    """Run `write` under the corpus mutation fence (2 s timeout) the first `rounds` times the
    archive step reads `trigger`; fail if the backup blocks it."""
    original = m._file_bytes
    done = []

    def mutate():
        from okengine.corpus_transaction import mutation
        with mutation(pack, writer="test", operation="write", lock_timeout_seconds=2):
            write(len(done))
        done.append(True)

    def interleave(root, rel, include_secrets):
        if rel.as_posix() == trigger and len(done) < rounds:
            writer = threading.Thread(target=mutate)
            before = len(done)
            writer.start()
            writer.join(5)
            assert len(done) == before + 1, "the writer was blocked by the backup's archive step"
        return original(root, rel, include_secrets)

    monkeypatch.setattr(m, "_file_bytes", interleave)
    return done


def _fence_spy(m, monkeypatch):
    """Record, per fence hold, its duration and how many files were COPIED under it."""
    import contextlib
    holds = []
    real_fence, real_copy = m.stable_corpus, shutil.copy2
    copies = [0]

    def copy2(*args, **kwargs):
        copies[0] += 1
        return real_copy(*args, **kwargs)

    @contextlib.contextmanager
    def fence(*args, **kwargs):
        with real_fence(*args, **kwargs) as epoch:
            start, copies[0] = time.perf_counter(), 0
            yield epoch
            holds.append((time.perf_counter() - start, copies[0]))

    monkeypatch.setattr(m, "stable_corpus", fence)
    monkeypatch.setattr(shutil, "copy2", copy2)
    return holds


def _links_spy(monkeypatch):
    linked = []
    real_link = os.link
    monkeypatch.setattr(os, "link", lambda src, dst, **kw: (linked.append(Path(dst).name),
                                                             real_link(src, dst, **kw))[1])
    return linked


def test_create_archives_one_stable_corpus_state_while_a_writer_proceeds(tmp_path, monkeypatch,
                                                                         capsys):
    """okengine#1032: a writer that publishes two pages while the archive is being built is not
    blocked by the backup, and the archive still holds the one pre-write corpus state. A publish by
    rename changes the old inode's ctime and link count; that must not read as a rewrite."""
    m = _mod()
    pack = _pack(tmp_path)
    first, second = pack / "wiki/a.md", pack / "wiki/b.md"
    first.write_text("version-1", encoding="utf-8")
    second.write_text("version-1", encoding="utf-8")
    _settle(pack)
    linked = _links_spy(monkeypatch)

    def write(_round):
        _publish(first, "version-2")
        _publish(second, "version-2")

    done = _write_during_archive(m, monkeypatch, pack, "wiki/a.md", write)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "coherent")
    assert done == [True]
    assert "a.md" in linked and "b.md" in linked
    with tarfile.open(archive) as tar:
        assert tar.extractfile("wiki/a.md").read() == b"version-1"
        assert tar.extractfile("wiki/b.md").read() == b"version-1"
    assert manifest["corpus_epoch"] == 0
    assert manifest["consistency"]["corpus_snapshot"] == "hardlink"
    assert manifest["consistency"]["rewritten_after_fence"] == []
    assert "rebuilding" not in capsys.readouterr().err
    assert first.read_text() == second.read_text() == "version-2"
    assert m.verify(archive)[0]


def test_an_in_place_rewrite_after_the_fence_rebuilds_from_copies(tmp_path, monkeypatch, capsys):
    """A hardlink shares the inode, so a page rewritten IN PLACE (O_TRUNC) after the fence would
    reach the archive newer than its corpus epoch. The backup detects it and rebuilds with that
    page copied under the fence: the result is one coherent, later corpus state."""
    m = _mod()
    pack = _pack(tmp_path)
    first, second = pack / "wiki/a.md", pack / "wiki/b.md"
    first.write_text("version-1", encoding="utf-8")
    second.write_text("version-1", encoding="utf-8")
    links = _links_spy(monkeypatch)

    def write(_round):
        first.write_text("version-2", encoding="utf-8")   # in place: same inode

    _settle(pack)
    _write_during_archive(m, monkeypatch, pack, "wiki/a.md", write)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "inplace")
    with tarfile.open(archive) as tar:
        assert tar.extractfile("wiki/a.md").read() == b"version-2"
        assert tar.extractfile("wiki/b.md").read() == b"version-1"
    assert manifest["corpus_epoch"] == 1, "the archive must carry the epoch its pages belong to"
    assert manifest["consistency"]["rewritten_after_fence"] == []
    assert links.count("b.md") == 2, links
    assert "rewritten in place" in capsys.readouterr().err
    assert m.verify(archive)[0]


def test_writers_rewriting_different_files_each_attempt_never_copy_the_corpus_under_the_fence(
    tmp_path, monkeypatch, capsys
):
    """QA on !1314: on a busy vault each attempt can meet a DIFFERENT in-place writer. The old
    escalation copied every fenced file under the fence on the last attempt (98.7 s on a 100k-page
    fixture; writers time out at 30 s). Each rebuild copies only the cumulative rewritten set, and
    after the last attempt the archive is kept and names what was still changing."""
    m = _mod()
    pack = _pack(tmp_path)
    for n in range(40):
        (pack / f"wiki/bulk/p{n:02d}.md").parent.mkdir(exist_ok=True)
        (pack / f"wiki/bulk/p{n:02d}.md").write_text(f"bulk {n}", encoding="utf-8")
    pages = [pack / f"wiki/p{n}.md" for n in range(m._SNAPSHOT_ATTEMPTS)]
    for page in pages:
        page.write_text("original", encoding="utf-8")
    _settle(pack)
    holds = _fence_spy(m, monkeypatch)

    def write(round_):
        pages[round_].write_text(f"round {round_}", encoding="utf-8")   # in place

    done = _write_during_archive(m, monkeypatch, pack, "engine.version", write,
                                 rounds=m._SNAPSHOT_ATTEMPTS)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "busy")
    fenced = sum(1 for rel in m.iter_files(pack, False) if m._corpus_fenced(rel))
    assert len(done) == len(holds) == m._SNAPSHOT_ATTEMPTS
    copied = [count for _seconds, count in holds]
    assert max(copied) <= m._SNAPSHOT_ATTEMPTS + 2, (
        f"a rebuild copied {max(copied)} of {fenced} fenced files under the fence: {copied}")
    assert all(seconds < 5 for seconds, _count in holds), holds
    last = m._SNAPSHOT_ATTEMPTS - 1
    with tarfile.open(archive) as tar:
        got = [tar.extractfile(f"wiki/p{n}.md").read().decode() for n in range(len(pages))]
    assert got == [f"round {n}" for n in range(m._SNAPSHOT_ATTEMPTS)]
    assert manifest["corpus_epoch"] == last
    assert manifest["consistency"]["rewritten_after_fence"] == [f"wiki/p{last}.md"]
    assert "kept changing in place" in capsys.readouterr().err
    assert m.verify(archive)[0]


def test_known_in_place_files_are_copied_on_the_first_attempt(tmp_path, monkeypatch, capsys):
    """The write path appends to wiki/log.md and the corpus journal in place on every write; a
    lane rewrites a page it touched minutes ago. None of that may force a rebuild."""
    m = _mod()
    pack = _pack(tmp_path)
    log = pack / "wiki/log.md"
    log.write_text("- first\n", encoding="utf-8")
    state = pack / ".okengine/lane-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"n": 1}', encoding="utf-8")
    hot = pack / "wiki/dashboards/fleet-health.md"
    hot.parent.mkdir(parents=True)
    hot.write_text("old dashboard", encoding="utf-8")
    _settle(pack)
    recent = time.time() - 600
    os.utime(hot, (recent, recent))                  # touched ten minutes ago

    def write(_round):
        with open(log, "a", encoding="utf-8") as stream:
            stream.write("- during backup\n")
        state.write_text('{"n": 2}', encoding="utf-8")
        hot.write_text("new dashboard", encoding="utf-8")

    holds = _fence_spy(m, monkeypatch)
    _write_during_archive(m, monkeypatch, pack, "engine.version", write)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "known")
    assert len(holds) == 1, "an append to log.md or a hot rewrite forced a rebuild"
    with tarfile.open(archive) as tar:
        assert tar.extractfile("wiki/log.md").read() == b"- first\n"
        assert tar.extractfile(".okengine/lane-state.json").read() == b'{"n": 1}'
        assert tar.extractfile("wiki/dashboards/fleet-health.md").read() == b"old dashboard"
    assert manifest["consistency"]["copied_under_fence"] == 3
    assert "rebuilding" not in capsys.readouterr().err


def test_hot_files_beyond_the_copy_budget_are_linked(tmp_path, monkeypatch, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    page = pack / "wiki/hot.md"
    page.write_text("hot v1", encoding="utf-8")
    _settle(pack)
    recent = time.time() - 600
    os.utime(page, (recent, recent))
    monkeypatch.setattr(m, "_COPY_BUDGET_BYTES", 0)
    linked = _links_spy(monkeypatch)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "budget")
    assert "hot.md" in linked and manifest["consistency"]["copied_under_fence"] == 0


def test_the_hot_window_is_thirty_minutes(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    _settle(pack)
    now = time.time()
    inside, outside = pack / "wiki/inside.md", pack / "wiki/outside.md"
    for page, age in ((inside, 1790), (outside, 1810)):
        page.write_text("x", encoding="utf-8")
        os.utime(page, (now - age, now - age))
    staged, refs = m._snapshot_corpus(pack, [Path("wiki/inside.md"), Path("wiki/outside.md")],
                                      tmp_path / "stage", link=True)
    assert staged == {Path("wiki/inside.md"), Path("wiki/outside.md")}
    assert Path("wiki/inside.md") not in refs, "a page modified 29m50s ago must be copied"
    assert refs[Path("wiki/outside.md")][0] == "link", "a page modified 30m10s ago is linked"


def test_files_that_must_be_copied_do_not_spend_the_hot_budget(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    log, hot = pack / "wiki/log.md", pack / "wiki/hot.md"
    log.write_text("L" * 100, encoding="utf-8")
    hot.write_text("H" * 10, encoding="utf-8")
    _settle(pack)
    recent = time.time() - 600
    os.utime(hot, (recent, recent))
    monkeypatch.setattr(m, "_COPY_BUDGET_BYTES", 10)
    staged, refs = m._snapshot_corpus(pack, [Path("wiki/hot.md"), Path("wiki/log.md")],
                                      tmp_path / "stage", link=True)
    assert refs == {} and staged == {Path("wiki/hot.md"), Path("wiki/log.md")}
    staged, refs = m._snapshot_corpus(pack, [Path("wiki/log.md"), Path("wiki/hot.md")],
                                      tmp_path / "stage2", link=True)
    assert refs == {}, "copying log.md (a must-copy) used up the hot-file budget"


def test_snapshot_bounds_are_the_documented_ones():
    """docs/backup-restore.md states three attempts and a 64 MB hot-copy budget."""
    m = _mod()
    assert (m._SNAPSHOT_ATTEMPTS, m._COPY_BUDGET_BYTES) == (3, 64 * 1024 * 1024)


@pytest.mark.parametrize("rel,expected", [
    ("wiki/log.md", True), ("wiki/entities/log.md", True), (".okengine/corpus/journal.jsonl", True),
    ("wiki/feed.jsonl", True), (".okengine/lane-state.json", True),
    ("wiki/data.json", False), ("wiki/log.md.bak", False), (".okengine/composed-schema.yaml", False),
    ("wiki/entities/acme.md", False),
])
def test_known_in_place_writers(rel, expected):
    assert _mod()._written_in_place(Path(rel)) is expected


def _stat(**fields):
    base = dict(st_ino=1, st_nlink=2, st_mtime_ns=10, st_size=5, st_ctime_ns=20,
                st_mode=0o100644, st_uid=7, st_gid=8)
    base.update(fields)
    return type("St", (), base)()


def test_identity_records_what_changed_since_compares():
    m = _mod()
    st = _stat()
    assert m._identity("link", st) == ("link", 2, 10, 5, 20, 0o100644, 7, 8)
    assert m._identity("live", st) == ("live", 1, 10, 5, 20, 0o100644, 7, 8)


@pytest.mark.parametrize("fields,changed", [
    ({}, False),
    ({"st_mtime_ns": 11}, True), ({"st_size": 6}, True),
    ({"st_ctime_ns": 21}, True),                        # same count and mode: a pinned-mtime rewrite
    ({"st_nlink": 1, "st_ctime_ns": 21}, False),        # renamed away: the old inode is untouched
    ({"st_nlink": 3, "st_ctime_ns": 21}, False),        # another backup's staging link
    ({"st_ctime_ns": 21, "st_mode": 0o100666}, False),  # chmod
    ({"st_ctime_ns": 21, "st_uid": 9}, False),          # chown
    ({"st_ctime_ns": 21, "st_gid": 9}, False),          # chgrp
    ({"st_mode": 0o100666}, False),
    ({"st_ino": 9}, False),                             # a link is checked on its own inode
    ({"st_mtime_ns": 11, "st_mode": 0o100666}, True),   # a write and a chmod
])
def test_changed_since_for_a_linked_entry(fields, changed):
    m = _mod()
    ref = ("link", 2, 10, 5, 20, 0o100644, 7, 8)
    assert m._changed_since(ref, _stat(**fields)) is changed


@pytest.mark.parametrize("fields,changed", [
    ({}, False), ({"st_ino": 9}, True), ({"st_mtime_ns": 11}, True), ({"st_size": 6}, True),
    ({"st_ctime_ns": 21}, True), ({"st_nlink": 7}, False),
    ({"st_nlink": 7, "st_ctime_ns": 21}, True),          # a live file has no link of ours
    ({"st_ctime_ns": 21, "st_mode": 0o100600}, False),
    ({"st_ino": 9, "st_mode": 0o100600}, True),
])
def test_changed_since_for_a_file_read_after_the_fence(fields, changed):
    m = _mod()
    ref = ("live", 1, 10, 5, 20, 0o100644, 7, 8)
    assert m._changed_since(ref, _stat(**fields)) is changed


def _staging_dirs(pack, m):
    return [p.name for p in (pack / m._STAGING_DIR).iterdir() if p.name.startswith(".staging-")]


def test_a_recursive_chmod_during_the_archive_does_not_rebuild(tmp_path, monkeypatch, capsys):
    """QA on !1314 (round 2): `chmod -R a+rwX` (ensure-runtime --fix-perms, ensure_readable) moved
    every linked file's ctime; each counted as rewritten and the rebuild copied all 100k files under
    the fence for 134 s. A mode change is metadata: the bytes are the snapshot's."""
    m = _mod()
    pack = _pack(tmp_path)
    for n in range(20):
        (pack / f"wiki/p{n:02d}.md").write_text(f"page {n}", encoding="utf-8")
    _settle(pack)
    holds = _fence_spy(m, monkeypatch)

    def chmod_all(_round):
        for path in (pack / "wiki").rglob("*.md"):
            path.chmod(0o666)

    _write_during_archive(m, monkeypatch, pack, "engine.version", chmod_all)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "chmod")
    assert len(holds) == 1, f"a chmod forced a rebuild: {holds}"
    assert manifest["consistency"]["rewritten_after_fence"] == []
    assert "rebuilding" not in capsys.readouterr().err
    assert m.verify(archive)[0]


def test_a_metadata_event_on_every_file_rebuilds_once_with_capped_copies(tmp_path, monkeypatch,
                                                                         capsys):
    """A ctime-only change that keeps mode, owner and link count (chown to the same owner, a second
    backup linking and unlinking every file) is indistinguishable from QA's N1 rewrite, so it does
    count as changed. The forced copies are capped, the rest relinked at the next fence, so the
    whole corpus is never copied under the fence."""
    m = _mod()
    pack = _pack(tmp_path)
    for n in range(30):
        (pack / f"wiki/p{n:02d}.md").write_text(f"page {n}", encoding="utf-8")
    _settle(pack)
    monkeypatch.setattr(m, "_FORCED_COPY_MAX_FILES", 5, raising=False)
    holds = _fence_spy(m, monkeypatch)
    side = tmp_path / "side"
    side.mkdir()

    def link_and_unlink_all(_round):               # what a concurrent create() does to each inode
        _after_the_ctime_tick_of_all((pack / "wiki").rglob("*.md"))
        for n, path in enumerate(sorted((pack / "wiki").rglob("*.md"))):
            os.link(path, side / f"{n}")
            (side / f"{n}").unlink()

    _write_during_archive(m, monkeypatch, pack, "engine.version", link_and_unlink_all)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "meta")
    copied = [count for _seconds, count in holds]
    assert len(holds) == 2 and max(copied) <= 5, f"fence copies per attempt: {copied}"
    assert manifest["consistency"]["rewritten_after_fence"] == []
    assert "rebuilding" in capsys.readouterr().err
    with tarfile.open(archive) as tar:
        assert tar.extractfile("wiki/p07.md").read() == b"page 7"


def test_a_real_concurrent_create_stays_bounded(tmp_path, monkeypatch):
    """A second create() run during the first one's archive phase (QA adv.py ctime)."""
    m = _mod()
    pack = _pack(tmp_path)
    for n in range(30):
        (pack / f"wiki/p{n:02d}.md").write_text(f"page {n}", encoding="utf-8")
    _settle(pack)
    monkeypatch.setattr(m, "_FORCED_COPY_MAX_FILES", 5, raising=False)
    holds = _fence_spy(m, monkeypatch)
    original = m._write_archive
    nested = []

    def write(*args, **kwargs):
        if not nested:
            nested.append(None)
            nested[0] = m.create(pack, tmp_path / "bk-b", False, "b")
        return original(*args, **kwargs)

    monkeypatch.setattr(m, "_write_archive", write)
    archive, manifest = m.create(pack, tmp_path / "bk-a", False, "a")
    assert max(count for _seconds, count in holds) <= 5, holds
    assert m.verify(archive)[0] and m.verify(nested[0][0])[0]
    assert _staging_dirs(pack, m) == []


def test_forced_copies_stop_at_the_byte_cap(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    big, small = pack / "wiki/log.md", pack / "wiki/feed.jsonl"
    big.write_text("B" * 100, encoding="utf-8")
    small.write_text("S" * 10, encoding="utf-8")
    _settle(pack)
    monkeypatch.setattr(m, "_COPY_BUDGET_BYTES", 0)
    monkeypatch.setattr(m, "_FORCED_COPY_MAX_BYTES", 50)
    staged, refs = m._snapshot_corpus(pack, [Path("wiki/feed.jsonl"), Path("wiki/log.md")],
                                      tmp_path / "stage", link=True)
    assert Path("wiki/feed.jsonl") not in refs, "the small in-place file must be copied"
    assert refs[Path("wiki/log.md")][0] == "link", "past the byte cap the file is linked"
    monkeypatch.setattr(m, "_FORCED_COPY_MAX_BYTES", 10)
    staged, refs = m._snapshot_corpus(pack, [Path("wiki/feed.jsonl"), Path("wiki/log.md")],
                                      tmp_path / "stage2", link=True)
    assert Path("wiki/feed.jsonl") not in refs and Path("wiki/log.md") in refs


def test_restore_names_files_that_kept_changing_after_the_fence(tmp_path, monkeypatch, capsys):
    """QA N5: restore used to exit 0 without a word about rewritten_after_fence."""
    m = _mod()
    pack = _pack(tmp_path)
    pages = [pack / f"wiki/p{n}.md" for n in range(m._SNAPSHOT_ATTEMPTS)]
    for page in pages:
        page.write_text("original", encoding="utf-8")
    _settle(pack)
    _write_during_archive(m, monkeypatch, pack, "engine.version",
                          lambda r: pages[r].write_text(f"round {r}", encoding="utf-8"),
                          rounds=m._SNAPSHOT_ATTEMPTS)
    archive, _ = m.create(pack, tmp_path / "bk", False, "n5")
    capsys.readouterr()
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    out = capsys.readouterr().out
    assert "rewritten_after_fence" in out and f"wiki/p{m._SNAPSHOT_ATTEMPTS - 1}.md" in out


@pytest.mark.parametrize("manifest,expected", [
    ({}, []), ({"consistency": "x"}, []), ({"consistency": {}}, []),
    ({"consistency": {"rewritten_after_fence": "wiki/a.md"}}, []),
    ({"consistency": {"rewritten_after_fence": ["wiki/a.md", 3, None]}}, ["wiki/a.md"]),
])
def test_rewritten_after_fence_from_the_manifest_is_filtered(manifest, expected):
    assert _mod()._manifest_rewritten_after_fence(manifest) == expected


def test_restore_names_at_most_a_bounded_list(tmp_path, monkeypatch, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "bounded")
    names = [f"wiki/x{n}.md" for n in range(m._NOT_CARRIED_PRINT_LIMIT + 3)]
    real_verify = m.verify
    monkeypatch.setattr(m, "verify", lambda arc: (lambda r: (r[0], {
        **r[1], "consistency": {**r[1]["consistency"], "rewritten_after_fence": names}}, r[2]))(
        real_verify(arc)))
    capsys.readouterr()
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    out = capsys.readouterr().out
    assert "(+3 more)" in out and names[-1] not in out


def test_a_page_deleted_after_the_fence_without_hardlinks_is_a_change_not_a_crash(
    tmp_path, monkeypatch, capsys
):
    """QA N6: in read-after-fence mode a page removed (or moved) after the fence crashed create()
    with FileNotFoundError and left no archive."""
    m = _mod()
    pack = _pack(tmp_path)
    page = pack / "wiki/entities/a/acme.md"
    _settle(pack)
    monkeypatch.setattr(os, "link", create_autospec(os.link, side_effect=OSError(18, "EXDEV")))

    def delete(_round):
        page.unlink()

    _write_during_archive(m, monkeypatch, pack, "engine.version", delete)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "deleted")
    assert "wiki/entities/a/acme.md" not in manifest["files"]
    assert manifest["corpus_epoch"] == 1 and m.verify(archive)[0]


def test_a_runtime_file_removed_during_the_archive_is_skipped(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    original = m._file_bytes
    gone = pack / ".hermes-data/config.yaml"

    def vanish(root, rel, include_secrets):
        if rel.as_posix() == ".hermes-data/config.yaml":
            gone.unlink(missing_ok=True)
        return original(root, rel, include_secrets)

    monkeypatch.setattr(m, "_file_bytes", vanish)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "gone")
    assert ".hermes-data/config.yaml" not in manifest["files"]
    assert manifest["consistency"]["rewritten_after_fence"] == [] and m.verify(archive)[0]


def test_a_sweep_cannot_remove_a_staging_dir_before_its_owner_locks_it(tmp_path, monkeypatch):
    """QA N7: a concurrent create()'s sweep landing between mkdtemp and flock removed the fresh
    staging dir. Creating and locking now happen under a parent lock the sweep also takes."""
    m = _mod()
    pack = _pack(tmp_path)
    real_mkdtemp = tempfile.mkdtemp
    survived = []

    def mkdtemp(*args, **kwargs):
        made = real_mkdtemp(*args, **kwargs)
        if "staging" in kwargs.get("prefix", "") and not survived:
            m._sweep_stale_staging(Path(kwargs["dir"]))
            survived.append(Path(made).is_dir())
        return made

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    archive, _ = m.create(pack, tmp_path / "bk", False, "window")
    assert survived == [True] and m.verify(archive)[0]


def _after_the_ctime_tick_of(path, limit_seconds=5.0):
    """Wait until the filesystem's ctime clock has moved past `path`'s ctime (the backup's link).

    The kernel stamps ctime from a coarse clock, so a rewrite in the same tick as the snapshot's
    os.link leaves ctime unchanged. That is the documented N1 residual (a ctime that does not
    advance), and on a fast runner it made the detection test fail intermittently (main pipeline
    12998, job 157909: new bytes archived at corpus epoch 0). The probe is a file on the same
    filesystem, so its ctime comes from the same clock."""
    floor = os.stat(path).st_ctime_ns
    probe = Path(path).with_name(".ctime-probe")
    deadline = time.monotonic() + limit_seconds
    try:
        while True:
            probe.write_bytes(b"")
            if probe.stat().st_ctime_ns > floor:
                return
            assert time.monotonic() < deadline, "the filesystem's ctime clock did not advance"
            time.sleep(0.001)
    finally:
        probe.unlink(missing_ok=True)


def _after_the_ctime_tick_of_all(paths):
    """_after_the_ctime_tick_of for the newest ctime among `paths`, for a test whose change is
    ctime-only on every file (nightly 13316, job 164280: a same-owner chown in the snapshot's tick
    went unseen, so the archive kept its first attempt)."""
    _after_the_ctime_tick_of(max(paths, key=lambda path: os.stat(path).st_ctime_ns))


def test_a_same_size_rewrite_with_its_mtime_set_back_is_still_detected(tmp_path, monkeypatch,
                                                                       capsys):
    """QA P1: a settled page rewritten in place at the same size, mtime restored with os.utime,
    used to reach the archive with new bytes under the old epoch. The link count is unchanged, so
    its ctime gives it away."""
    m = _mod()
    pack = _pack(tmp_path)
    page = pack / "wiki/entities/a/acme.md"
    _settle(pack)
    pinned = page.stat()

    def write(_round):
        _after_the_ctime_tick_of(page)
        with open(page, "r+", encoding="utf-8") as stream:
            stream.write("# ACME")                                    # same size, in place
        os.utime(page, ns=(pinned.st_atime_ns, pinned.st_mtime_ns))   # mtime set back

    _write_during_archive(m, monkeypatch, pack, "engine.version", write)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "pinned")
    with tarfile.open(archive) as tar:
        body = tar.extractfile("wiki/entities/a/acme.md").read()
    assert (body, manifest["corpus_epoch"]) == (b"# ACME\n", 1), (
        "new bytes were archived under the fence epoch")
    assert "rewritten in place" in capsys.readouterr().err


def test_a_page_written_within_the_clock_tick_is_copied_not_linked(tmp_path, monkeypatch, capsys):
    """okengine#1017's trap: a rewrite in the same mtime tick at the same size leaves its stat
    unchanged, so a hardlinked page could change unseen. A page that has not settled at the fence
    is copied, so the archive keeps its fence-time bytes even with the hot-copy budget at zero."""
    m = _mod()
    pack = _pack(tmp_path)
    _settle(pack)
    monkeypatch.setattr(m, "_COPY_BUDGET_BYTES", 0)
    page = pack / "wiki/fresh.md"
    page.write_text("before", encoding="utf-8")
    pinned = page.stat()
    linked = _links_spy(monkeypatch)

    def write(_round):
        page.write_text("after!", encoding="utf-8")                   # same size, in place
        os.utime(page, ns=(pinned.st_atime_ns, pinned.st_mtime_ns))   # same tick

    _write_during_archive(m, monkeypatch, pack, "engine.version", write)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "tick")
    with tarfile.open(archive) as tar:
        assert tar.extractfile("wiki/fresh.md").read() == b"before"
    assert "fresh.md" in linked and "acme.md" in linked
    assert _staging_dirs(pack, m) == []
    assert "rewritten in place" not in capsys.readouterr().err
    assert manifest["consistency"]["corpus_snapshot"] == "hardlink"


def test_without_hardlinks_the_fence_copies_nothing_and_changes_are_rebuilt(tmp_path, monkeypatch,
                                                                            capsys):
    """QA N3: with hardlinks unavailable (EXDEV, protected_hardlinks) the first attempt used to copy
    the whole corpus under the fence. Now unchanged files are read after the fence and re-checked by
    inode, so a rename-publish during the archive is caught too."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / "wiki/entities/a/acme.md").chmod(0o640)
    (pack / "pack.yaml").chmod(0o604)
    archive, _ = m.create(pack, tmp_path / "bk", False, "linked")
    with tarfile.open(archive) as tar:
        assert tar.getmember("wiki/entities/a/acme.md").mode == 0o640
        assert tar.getmember("pack.yaml").mode == 0o604
    _settle(pack)
    link = create_autospec(os.link, side_effect=OSError(18, "EXDEV"))
    monkeypatch.setattr(os, "link", link)
    holds = _fence_spy(m, monkeypatch)
    page = pack / "wiki/entities/a/acme.md"
    _write_during_archive(m, monkeypatch, pack, "engine.version",
                          lambda _round: _publish(page, "# Acme v2\n"))
    archive, manifest = m.create(pack, tmp_path / "bk", False, "nolink")
    assert link.call_count == 1, "links were retried per file after the probe failed"
    assert "cannot hardlink" in capsys.readouterr().err
    assert manifest["consistency"]["corpus_snapshot"] == "read-after-fence"
    assert holds[0][1] <= 1, f"the first attempt copied {holds[0][1]} files under the fence"
    with tarfile.open(archive) as tar:
        member = tar.getmember("wiki/entities/a/acme.md")
        assert tar.extractfile(member).read() == b"# Acme v2\n"
        assert member.mode == page.stat().st_mode & 0o777    # the copy keeps the published mode
    assert manifest["corpus_epoch"] == 1 and len(holds) == 2


def test_one_file_that_cannot_be_linked_is_read_after_the_fence(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    _settle(pack)
    real_link = os.link

    def link(src, dst, **kwargs):
        if str(dst).endswith("acme.md"):
            raise PermissionError(1, "protected_hardlinks")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", link)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "onelink")
    assert manifest["consistency"]["corpus_snapshot"] == "hardlink"
    with tarfile.open(archive) as tar:
        member = tar.getmember("wiki/entities/a/acme.md")
        assert tar.extractfile(member).read() == b"# Acme\n"
        assert member.mtime == int((pack / "wiki/entities/a/acme.md").stat().st_mtime)


def test_a_stale_staging_dir_is_swept_but_a_live_one_is_kept(tmp_path):
    """QA N2: a killed create() left its staging dir behind, pinning old inodes and a copy of
    pack.yaml. The next create() removes staging dirs no live create() holds locked."""
    import fcntl
    m = _mod()
    pack = _pack(tmp_path)
    parent = pack / m._STAGING_DIR
    stale = parent / ".staging-killed-run"
    (stale / "wiki").mkdir(parents=True)
    os.link(pack / "pack.yaml", stale / "pack.yaml")
    live = parent / ".staging-running"
    live.mkdir()
    (parent / "unrelated").mkdir()
    (parent / ".staging-a-file").write_text("not a dir")
    holder = os.open(live, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        m.create(pack, tmp_path / "bk", False, "sweep")
    finally:
        os.close(holder)
    assert not stale.exists(), "a stale staging dir survived the next create()"
    assert live.is_dir() and (parent / "unrelated").is_dir() and (parent / ".staging-a-file").exists()


def test_a_write_during_a_slow_archive_succeeds_within_its_lock_timeout(tmp_path, monkeypatch):
    """okengine#1032: the archive step (gzip, hashing, the qmd online backup) can take minutes on a
    large vault. A write tool waits at most its lock timeout for the corpus fence, so the fence must
    not be held while archiving: here the archive is slower than the writer's timeout."""
    m = _mod()
    pack = _pack(tmp_path)
    archiving = threading.Event()
    original = m._file_bytes

    def slow(root, rel, include_secrets):
        archiving.set()
        time.sleep(0.3)
        return original(root, rel, include_secrets)

    monkeypatch.setattr(m, "_file_bytes", slow)
    outcome = {}

    def write_during_archive():
        archiving.wait(5)
        from okengine.corpus_transaction import mutation
        try:
            with mutation(pack, writer="test", operation="write", lock_timeout_seconds=0.5):
                _publish(pack / "wiki/entities/a/acme.md", "# Acme v2\n")
                outcome["owner"] = json.loads(
                    (pack / ".okengine/corpus/lock-owner.json").read_text())["writer"]
            outcome["ok"] = True
        except m.CorpusLockTimeout as exc:
            outcome["error"] = exc

    writer = threading.Thread(target=write_during_archive)
    writer.start()
    archive, manifest = m.create(pack, tmp_path / "bk", False, "slow")
    writer.join(5)
    assert outcome.get("ok"), f"a write timed out behind the backup: {outcome.get('error')}"
    assert outcome["owner"] == "test"
    with tarfile.open(archive) as tar:
        assert tar.extractfile("wiki/entities/a/acme.md").read() == b"# Acme\n"
    assert manifest["count"] >= 4 and m.verify(archive)[0]


def test_backup_staging_never_outlives_create(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    m.create(pack, tmp_path / "bk", False, "clean")
    staging = pack / m._STAGING_DIR
    assert [p.name for p in (tmp_path / "bk").iterdir()] == ["vault-clean.tar.gz"]
    assert _staging_dirs(pack, m) == []
    monkeypatch.setattr(m, "_snapshot_corpus", create_autospec(
        m._snapshot_corpus, side_effect=OSError("full")))
    with pytest.raises(OSError, match="full"):
        m.create(pack, tmp_path / "bk", False, "failed")
    assert [p.name for p in (tmp_path / "bk").iterdir()] == ["vault-clean.tar.gz"]
    assert _staging_dirs(pack, m) == []
    assert not (pack / ".okengine/corpus/lock-owner.json").exists()
    assert m._STAGING_DIR.startswith(".okengine/backups/")    # never walked, never archived


def test_runtime_and_sqlite_are_archived_outside_the_corpus_fence(tmp_path):
    m = _mod()
    fenced = {".okengine/corpus/epoch", "wiki/entities/a/acme.md", "pack.yaml", "schema.yaml"}
    unfenced = {".hermes-data/config.yaml", ".hermes-data/cron-plus/jobs.json",
                ".okengine/state.sqlite", "wiki/index.db"}
    assert {rel for rel in fenced | unfenced if m._corpus_fenced(Path(rel))} == fenced


def test_iter_files_never_descends_into_excluded_trees(tmp_path, monkeypatch):
    """The walk runs inside the corpus fence; a vault's .okengine/snapshots held 518k files."""
    m = _mod()
    pack = _pack(tmp_path)
    for skipped in (".okengine/snapshots/s1", ".git/objects", "wiki/__pycache__"):
        (pack / skipped).mkdir(parents=True)
        (pack / skipped / "x.md").write_text("x")
    visited = []
    real_scandir = os.scandir

    def recording_scandir(path):
        visited.append(Path(path).relative_to(pack).as_posix())
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", recording_scandir)
    files = {r.as_posix() for r in m.iter_files(pack, False)}
    assert "wiki/entities/a/acme.md" in files
    assert "wiki/entities" in visited, "the walk must prune while listing, not list then filter"
    assert not [v for v in visited if v.startswith((".okengine/snapshots", ".git"))
                or "__pycache__" in v]
    monkeypatch.undo()
    (pack / "wiki/link.md").symlink_to(pack / "pack.yaml")
    (pack / "wiki/dirlink").symlink_to(pack / "wiki/entities", target_is_directory=True)
    files = {r.as_posix() for r in m.iter_files(pack, False)}
    assert "wiki/link.md" not in files and not any(f.startswith("wiki/dirlink") for f in files)
    (pack / "tmp").write_text("a FILE named like a skipped prefix")
    (pack / "wiki/.git").write_text("gitdir: elsewhere\n")
    files = {r.as_posix() for r in m.iter_files(pack, False)}
    assert "tmp" not in files and "wiki/.git" not in files


def test_iter_files_skips_an_unreadable_directory_like_the_old_walk(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path)
    real_scandir = os.scandir

    def scandir(path):
        if Path(path).name == "entities":
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    files = {r.as_posix() for r in m.iter_files(pack, False)}
    assert "pack.yaml" in files and not any(f.startswith("wiki/entities") for f in files)


def test_iter_files_parts_split_at_the_runtime_dir_and_cover_the_whole_tree(tmp_path):
    """The corpus part is walked inside the fence; the runtime part (.hermes-data, whose cron-plus
    run output can hold 200k files) is walked after it. Together they are exactly the full walk."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / "wiki/.hermes-data").mkdir()
    (pack / "wiki/.hermes-data/nested.md").write_text("not the runtime dir")
    corpus = m.iter_files(pack, False, part="corpus")
    runtime = m.iter_files(pack, False, part="runtime")
    assert all(r.parts[0] != ".hermes-data" for r in corpus)
    assert Path("wiki/.hermes-data/nested.md") in corpus
    assert runtime and all(r.parts[0] == ".hermes-data" for r in runtime)
    assert sorted(corpus + runtime, key=lambda r: r.as_posix()) == m.iter_files(pack, False)
    real_runtime = pack / ".hermes-data"
    real_runtime.rename(tmp_path / "elsewhere")
    assert m.iter_files(pack, False, part="runtime") == []
    real_runtime.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert m.iter_files(pack, False, part="runtime") == []


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
    _as_deploy_user(target)
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
    _as_deploy_user(target)
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


def _jobs_store(tmp_path, mode=0o600):
    jobs_path = tmp_path / ".hermes-data/cron-plus/jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text('{"jobs": [{"name": "lane", "next_run_at": "old"}]}')
    jobs_path.chmod(mode)
    return jobs_path


def test_restore_rearm_keeps_the_job_store_private(tmp_path):
    """okengine#1032: jobs.json carries per-job env and prompts; every writer keeps it 0600. The
    re-arm rewrite used write_text and left it at the umask (0644, world-readable)."""
    m = _mod()
    jobs_path = _jobs_store(tmp_path)
    previous = os.umask(0o022)
    try:
        m._rearm_restored_schedule(tmp_path)
    finally:
        os.umask(previous)
    assert oct(jobs_path.stat().st_mode & 0o777) == oct(0o600)
    assert json.loads(jobs_path.read_text()) == {"jobs": [{"name": "lane", "next_run_at": None}]}
    assert sorted(p.name for p in jobs_path.parent.iterdir()) == ["jobs.json", "jobs.json.lock"]


def test_restore_rearm_waits_for_the_cron_plus_store_lock(tmp_path):
    """The re-arm is a read-modify-write of the scheduler's store, so it takes the store's sidecar
    lock (okengine#850) instead of racing a running gateway's scheduler."""
    import fcntl
    m = _mod()
    jobs_path = _jobs_store(tmp_path)
    spec = importlib.util.spec_from_file_location("cps", REPO / "scripts/cron_plus_store.py")
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    lock_name = store.LOCK_NAME
    holder = os.open(jobs_path.parent / lock_name, os.O_RDONLY | os.O_CREAT, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX)
    done = threading.Event()
    rearm = threading.Thread(target=lambda: (m._rearm_restored_schedule(tmp_path), done.set()))
    try:
        rearm.start()
        assert not done.wait(0.3), "the re-arm rewrote jobs.json without the cron-plus store lock"
        assert json.loads(jobs_path.read_text())["jobs"][0]["next_run_at"] == "old"
    finally:
        os.close(holder)
    rearm.join(5)
    assert done.is_set()
    assert json.loads(jobs_path.read_text())["jobs"][0]["next_run_at"] is None


def test_restore_rearm_lock_name_matches_the_scheduler_and_the_deploy():
    spec = importlib.util.spec_from_file_location("cps", REPO / "scripts/cron_plus_store.py")
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    assert _mod()._CRON_PLUS_LOCK_NAME == store.LOCK_NAME
    install = (REPO / "scripts/install-cron-plus.sh").read_text()
    assert "scripts/cron_plus_store.py" in install and "store.LOCK_NAME" in install


def test_restore_rearm_keeps_the_job_store_owner(tmp_path, monkeypatch):
    m = _mod()
    jobs_path = _jobs_store(tmp_path, mode=0o640)
    before = jobs_path.stat()
    monkeypatch.setattr(os, "geteuid", lambda: before.st_uid + 1)
    chown = create_autospec(os.fchown, side_effect=os.fchown)
    monkeypatch.setattr(os, "fchown", chown)
    m._rearm_restored_schedule(tmp_path)
    assert chown.call_args.args[1:] == (before.st_uid, before.st_gid)
    after = jobs_path.stat()
    assert (after.st_uid, after.st_gid, after.st_mode & 0o777) == (
        before.st_uid, before.st_gid, 0o640)


def test_restore_rearm_skips_chown_when_already_the_owner(tmp_path, monkeypatch):
    m = _mod()
    jobs_path = _jobs_store(tmp_path)
    st = jobs_path.stat()
    monkeypatch.setattr(os, "geteuid", lambda: st.st_uid)
    monkeypatch.setattr(os, "getegid", lambda: st.st_gid)
    chown = create_autospec(os.fchown)
    monkeypatch.setattr(os, "fchown", chown)
    m._rearm_restored_schedule(tmp_path)
    chown.assert_not_called()


def test_restore_rearm_failure_leaves_the_store_and_no_staged_file(tmp_path, monkeypatch):
    m = _mod()
    jobs_path = _jobs_store(tmp_path)
    original = jobs_path.read_text()
    monkeypatch.setattr(os, "replace", create_autospec(os.replace, side_effect=OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        m._rearm_restored_schedule(tmp_path)
    assert jobs_path.read_text() == original
    assert sorted(p.name for p in jobs_path.parent.iterdir()) == ["jobs.json", "jobs.json.lock"]


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


def test_validator_crash_fails_closed(tmp_path, monkeypatch):
    """okengine#991: a validator that crashed proved nothing about the restored pack, so it must not
    certify it (it used to return ok with "validation skipped"). --no-validate is the explicit
    override; the same contract as framework_upgrade._default_validator."""
    m=_mod()
    monkeypatch.setattr(m.importlib.util,"spec_from_file_location",lambda *_:None)
    ok,summary=m._validator(tmp_path)
    assert not ok and "crashed" in summary and "unvalidated" in summary


def test_main_restore_fails_when_the_validator_crashes(tmp_path, monkeypatch, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    monkeypatch.setattr(m.importlib.util, "spec_from_file_location", lambda *_: None)
    assert m.main(["restore", str(archive), str(tmp_path / "t")]) == 1
    assert "does not validate" in capsys.readouterr().out
    assert m.main(["restore", str(archive), str(tmp_path / "t2"), "--no-validate"]) == 0


def test_validator_loads_and_runs_framework_validate(tmp_path, monkeypatch):
    from types import SimpleNamespace
    m = _mod()
    loader = SimpleNamespace(exec_module=lambda module: setattr(
        module, "main", lambda argv: 3 if argv == [str(tmp_path), "--quiet"] else 4))
    spec = SimpleNamespace(loader=loader)
    monkeypatch.setattr(m.importlib.util, "spec_from_file_location", lambda *_: spec)
    monkeypatch.setattr(m.importlib.util, "module_from_spec", lambda _spec: SimpleNamespace())
    assert m._validator(tmp_path) == (False, "framework validate → exit 3")


# --- okengine#991: restore identity must never be root; non-secret .env config survives ------------

def test_restore_as_root_is_refused_before_anything_is_extracted(tmp_path, monkeypatch):
    """A root restore extracts root-owned files (the tar `data` filter never chowns), so the
    target-owner rebind would pin HERMES_UID=0/HERMES_GID=0 and the runtime-ownership check exempts
    uid 0. Refuse up front, naming the archived identity, and leave the target untouched."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".env").write_text("HERMES_UID=12001\nHERMES_GID=12002\n")
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    target = tmp_path / "restored"
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    with pytest.raises(m.RestoreOwnershipError, match="12001:12002"):
        m.restore(archive, target, force=False)
    assert not target.exists() or not any(target.iterdir())


@pytest.mark.parametrize("uid, gid", [(0, 1000), (1000, 0)])
def test_restore_identity_never_pins_root(tmp_path, monkeypatch, uid, gid):
    """The chokepoint itself: a root-owned restored runtime (e.g. a --force restore over a tree root
    created earlier) must raise instead of writing a 0 identity into .env."""
    m = _mod()
    target = tmp_path / "target"
    (target / ".hermes-data").mkdir(parents=True)
    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == target / ".hermes-data":
            return type("S", (), {"st_uid": uid, "st_gid": gid})()
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(m.RestoreOwnershipError, match="root"):
        m._restore_deployment_identity(target, {"hermes_uid": 12001, "hermes_gid": 12002})
    assert not (target / ".env").exists()


def test_main_restore_reports_ownership_refusal(tmp_path, monkeypatch, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    assert m.main(["restore", str(archive), str(tmp_path / "t")]) == 2
    assert "as root" in capsys.readouterr().err


def test_no_secrets_backup_carries_schedule_config_into_the_restored_env(tmp_path):
    """A default (secrets-excluded) backup dropped the WHOLE .env, so TZ / CRON_TZ /
    OKENGINE_BRIEF_HOUR / CRON_DEFER_UTC_HOURS were lost and the restored deployment silently
    re-armed every lane in UTC. Non-secret keys now ride in the manifest; secrets still never do."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".env").write_text(
        "OPENROUTER_API_KEY=do-not-archive\n"
        "export TZ=America/New_York\n"
        "CRON_TZ=America/New_York\n"
        "OKENGINE_BRIEF_HOUR=6   # local morning\n"
        "CRON_DEFER_UTC_HOURS=1-4,6-10\n"
        "OKENGINE_BRIEF_HOUR=9\n"
        "OKENGINE_MCP_TOKEN=sekrit-token\n"
        "SOME_UNCLASSIFIED_PACK_KEY=maybe-secret\n"
        "ENGINE_DIR=/host/local/path\n"
        "HERMES_UID=12001\n"
    )
    archive, manifest = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    raw = archive.read_bytes()
    import gzip
    assert b"do-not-archive" not in gzip.decompress(raw)
    assert b"sekrit-token" not in gzip.decompress(raw)
    assert b"maybe-secret" not in gzip.decompress(raw)
    assert manifest["deployment_env"] == {
        "TZ": "America/New_York", "CRON_TZ": "America/New_York",
        # A duplicate resolves LAST-wins, as docker compose's env_file does: the value the
        # containers actually ran with (v0.15.4 release audit).
        "OKENGINE_BRIEF_HOUR": "9", "CRON_DEFER_UTC_HOURS": "1-4,6-10"}

    target = tmp_path / "restored"
    assert m.restore(archive, target, force=False)[0]
    lines = (target / ".env").read_text().splitlines()
    for line in ("TZ=America/New_York", "CRON_TZ=America/New_York",
                 "OKENGINE_BRIEF_HOUR=9", "CRON_DEFER_UTC_HOURS=1-4,6-10"):
        assert line in lines, lines
    assert not any(k in "\n".join(lines) for k in
                   ("OPENROUTER", "OKENGINE_MCP_TOKEN", "SOME_UNCLASSIFIED", "ENGINE_DIR"))
    assert manifest["deployment_env_not_carried"] == [
        "ENGINE_DIR", "OKENGINE_MCP_TOKEN", "OPENROUTER_API_KEY", "SOME_UNCLASSIFIED_PACK_KEY"]


def test_main_restore_names_env_keys_it_did_not_carry(tmp_path, capsys):
    """The operator is told which .env keys must be re-provisioned (names only, never values)."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".env").write_text("TZ=America/New_York\nOPENROUTER_API_KEY=do-not-print\n")
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    out = capsys.readouterr().out
    assert "restored non-secret settings TZ" in out
    assert "NOT carried" in out and "OPENROUTER_API_KEY" in out and "do-not-print" not in out


def test_unprintable_portable_value_is_not_carried_at_create(tmp_path):
    """The create side applies the same one-line rule restore enforces."""
    m = _mod()
    (tmp_path / ".env").write_text("TZ=UTC\tEtc\nCRON_TZ=UTC\n", encoding="utf-8")
    carried, not_carried = m._deployment_env(tmp_path)
    assert carried == {"CRON_TZ": "UTC"}
    assert not_carried == ["TZ"]


@pytest.mark.parametrize("names, expected", [
    (["OPENROUTER_API_KEY", "bad name", 7, "X=1"], ["OPENROUTER_API_KEY"]),
    ("OPENROUTER_API_KEY", []),
])
def test_not_carried_names_from_manifest_are_filtered(names, expected):
    m = _mod()
    assert m._manifest_env_not_carried({"deployment_env_not_carried": names}) == expected


def test_restore_keeps_target_env_values_over_archived_config(tmp_path):
    """An existing target .env (a --force restore, or an --include-secrets archive's own .env) keeps
    its values; archived portable keys only fill what is missing."""
    m = _mod()
    target = tmp_path / "target"
    (target / ".hermes-data").mkdir(parents=True)
    _as_deploy_user(target)
    (target / ".env").write_text("TZ=Europe/Paris\nKEEP=yes\n")
    m._restore_deployment_identity(target, {"deployment_env": {
        "TZ": "America/New_York", "OKENGINE_BRIEF_HOUR": "6"}})
    lines = (target / ".env").read_text().splitlines()
    assert "TZ=Europe/Paris" in lines and "TZ=America/New_York" not in lines
    assert "OKENGINE_BRIEF_HOUR=6" in lines and "KEEP=yes" in lines


@pytest.mark.parametrize("deployment_env", [
    {"LD_PRELOAD": "/tmp/x.so"},                  # not a portable key: never written from a manifest
    {"TZ": "UTC\nOKENGINE_MCP_TOKEN=injected"},    # a newline would smuggle a second assignment
    # every other separator str.splitlines() honors does the same to a splitlines() reader
    # (ensure_projection_config.py re-joins with \n, turning it into a real key)
    {"TZ": "UTC\x85OKENGINE_PROJECTION_WRITER_PASSWORD=injected"},
    {"TZ": "UTC\u2028OKENGINE_MCP_TOKEN=injected"},
    {"TZ": "UTC\u2029OKENGINE_MCP_TOKEN=injected"},
    {"TZ": "UTC\x0bOKENGINE_MCP_TOKEN=injected"},
    {"TZ": "UTC\x00injected"},
    {"TZ": 7},                                    # non-string value
    ["TZ=UTC"],                                   # wrong shape
])
def test_restore_ignores_untrusted_manifest_env(tmp_path, deployment_env):
    """MANIFEST.json is not covered by its own digest, so its env block is filtered to the portable
    allowlist and single-line string values before it can reach .env."""
    m = _mod()
    target = tmp_path / "target"
    (target / ".hermes-data").mkdir(parents=True)
    _as_deploy_user(target)
    m._restore_deployment_identity(target, {"deployment_env": deployment_env})
    text = (target / ".env").read_text()
    assert "LD_PRELOAD" not in text and "injected" not in text and "TZ=" not in text


def _documented_env_keys():
    """Every key the engine documents for a deployment .env, plus every key deploy scripts read."""
    import re as _re
    keys = set()
    example = REPO / "templates" / "pack" / "skeleton" / ".env.example"
    for line in example.read_text(encoding="utf-8").splitlines():
        match = _re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", line)
        if match:
            keys.add(match.group(1))
    compose = REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml"
    keys.update(_re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", compose.read_text(encoding="utf-8")))
    for script in (REPO / "scripts").rglob("*.sh"):
        keys.update(_re.findall(r'_okengine_env_file_val\s+"?\$\{?\w+\}?"?\s+([A-Z][A-Z0-9_]*)',
                                script.read_text(encoding="utf-8", errors="replace")))
        # okengine#994: shell readers that call the dotenv_lib CLI directly (ensure-runtime)
        keys.update(_re.findall(r'dotenv_lib\.py"?\s+get\s+"[^"]+"\s+([A-Z][A-Z0-9_]*)',
                                script.read_text(encoding="utf-8", errors="replace")))
    return keys


def test_every_documented_env_key_is_classified_for_backup():
    """The backup carries .env keys by an explicit classification and drops anything unlisted (fail
    closed). A new documented key must be classified — portable, host-local, or secret — or a
    restore silently loses it (the TZ regression) or, worse, someone widens the rule to a denylist."""
    m = _mod()
    documented = _documented_env_keys()
    assert {"TZ", "CRON_TZ", "OKENGINE_BRIEF_HOUR", "CRON_DEFER_UTC_HOURS", "OKENGINE_MCP_TOKEN",
            "ANTHROPIC_API_KEY", "OKENGINE_TRUST", "API_SERVER_KEY"} <= documented, documented
    classified = m._PORTABLE_ENV_KEYS | m._HOST_LOCAL_ENV_KEYS | m._SECRET_ENV_KEYS
    assert sorted(documented - classified) == []
    overlaps = (m._PORTABLE_ENV_KEYS & m._HOST_LOCAL_ENV_KEYS, m._PORTABLE_ENV_KEYS & m._SECRET_ENV_KEYS,
                m._HOST_LOCAL_ENV_KEYS & m._SECRET_ENV_KEYS)
    assert all(not o for o in overlaps), overlaps


def test_portable_env_is_empty_without_a_dotenv(tmp_path):
    m = _mod()
    assert m._deployment_env(tmp_path) == ({}, [])


def test_main_restore_is_quiet_about_env_when_everything_was_carried(tmp_path, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".env").write_text("# a comment line\nTZ=America/New_York\nHERMES_UID=12001\n")
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    assert "NOT carried" not in capsys.readouterr().out


@pytest.mark.parametrize("value", ["https://user:pass@git.example/x", "https://token@git.example/x",
                                   "http://u@h"])
def test_a_url_with_userinfo_is_not_carried(tmp_path, value):
    """A credential in URL userinfo stays out of the archive even under a non-secret key; the key
    is reported as not carried instead."""
    m = _mod()
    (tmp_path / ".env").write_text(f"OKENGINE_PROJECT_URL={value}\n", encoding="utf-8")
    assert m._deployment_env(tmp_path) == ({}, ["OKENGINE_PROJECT_URL"])
    assert m._manifest_deployment_env({"deployment_env": {"OKENGINE_PROJECT_URL": value}}) == {}


def test_a_plain_project_url_is_carried(tmp_path):
    m = _mod()
    (tmp_path / ".env").write_text("OKENGINE_PROJECT_URL=https://git.example/x@v1\n", encoding="utf-8")
    assert m._deployment_env(tmp_path) == ({"OKENGINE_PROJECT_URL": "https://git.example/x@v1"}, [])


def test_not_carried_names_are_length_capped_and_the_printed_list_is_bounded(tmp_path, capsys):
    m = _mod()
    assert m._manifest_env_not_carried({"deployment_env_not_carried": ["A" * 64, "B" * 65]}) == ["A" * 64]
    pack = _pack(tmp_path)
    (pack / ".env").write_text("".join(f"K{i:02d}=v\n" for i in range(45)))
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    out = capsys.readouterr().out
    assert "K39" in out and "K40" not in out and "(+5 more)" in out


def test_a_restore_keeps_a_read_only_deployment_read_only(tmp_path):
    """OKENGINE_EDITING unset means editing ON, so dropping `OKENGINE_EDITING=0` on restore turned a
    read-only deployment writable with no error (v0.15.4 release audit)."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".env").write_text("OKENGINE_EDITING=0\nAPI_SERVER_ENABLED=true\nCOMPOSE_PROFILES=x\n")
    carried, not_carried = m._deployment_env(pack)
    assert carried == {"OKENGINE_EDITING": "0", "API_SERVER_ENABLED": "true",
                       "COMPOSE_PROFILES": "x"}
    assert not_carried == []


@pytest.mark.parametrize(("lines", "carried", "not_carried"), [
    ("TZ=UTC\nTZ=America/New_York\n", {"TZ": "America/New_York"}, []),
    ("OKENGINE_PROJECT_URL=https://git.example/x\nOKENGINE_PROJECT_URL=https://t@h/x\n",
     {}, ["OKENGINE_PROJECT_URL"]),
    ("OKENGINE_PROJECT_URL=https://t@h/x\nOKENGINE_PROJECT_URL=https://git.example/x\n",
     {"OKENGINE_PROJECT_URL": "https://git.example/x"}, []),
])
def test_a_duplicated_env_key_resolves_last_wins_like_compose(tmp_path, lines, carried, not_carried):
    m = _mod()
    (tmp_path / ".env").write_text(lines, encoding="utf-8")
    assert m._deployment_env(tmp_path) == (carried, not_carried)


def test_a_no_secrets_backup_excludes_the_hermes_home_env(tmp_path):
    """Hermes loads .hermes-data/.env and `hermes setup` writes credentials there; a
    secrets-excluded archive carried it verbatim (v0.15.4 release audit)."""
    import gzip
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data").mkdir(exist_ok=True)
    (pack / ".hermes-data" / ".env").write_text("TELEGRAM_BOT_TOKEN=hermes-home-secret\n")
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert b"hermes-home-secret" not in gzip.decompress(archive.read_bytes())
    archive, _ = m.create(pack, tmp_path / "bk2", True, "20260101T080001Z")
    assert b"hermes-home-secret" in gzip.decompress(archive.read_bytes())


# --- okengine#994 B: a no-secrets restore names the .hermes-data/.env keys it does not bring back ---
_HERMES_ENV = ("BROWSER_HEADLESS=hermes-value-one\n"
               "export TERMINAL_TIMEOUT='hermes-value-two' # seconds\n"
               "TELEGRAM_BOT_TOKEN=hermes-secret-three\n"
               "OKPACK_SEC_OBSERVATIONS=hermes-value-four\n")
_HERMES_VALUES = ("hermes-value-one", "hermes-value-two", "hermes-secret-three", "hermes-value-four")
_HERMES_KEYS = ["BROWSER_HEADLESS", "OKPACK_SEC_OBSERVATIONS", "TELEGRAM_BOT_TOKEN", "TERMINAL_TIMEOUT"]


def test_a_no_secrets_restore_names_every_hermes_home_env_key_and_no_value(tmp_path, capsys):
    import gzip
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data" / ".env").write_text(_HERMES_ENV)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert manifest["hermes_env_not_carried"] == _HERMES_KEYS
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.lstrip().startswith(".hermes-data/.env"))
    assert "NOT carried" in line and ", ".join(_HERMES_KEYS) in line
    restored_env = (tmp_path / "t" / ".env").read_text()
    raw = gzip.decompress(archive.read_bytes()).decode("utf-8", "replace")
    for value in _HERMES_VALUES:
        assert value not in out, f"a .hermes-data/.env value reached the restore output: {value}"
        assert value not in raw, f"a .hermes-data/.env value reached the archive: {value}"
        assert value not in restored_env
    assert not (tmp_path / "t" / ".hermes-data" / ".env").exists()


def test_an_include_secrets_backup_carries_the_hermes_env_and_names_nothing(tmp_path, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data" / ".env").write_text(_HERMES_ENV)
    archive, manifest = m.create(pack, tmp_path / "bk", True, "20260101T080000Z")
    assert manifest["hermes_env_not_carried"] == []
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    assert ".hermes-data/.env" not in capsys.readouterr().out
    assert (tmp_path / "t" / ".hermes-data" / ".env").read_text() == _HERMES_ENV


def test_no_hermes_env_records_nothing_and_restore_stays_quiet(tmp_path, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert manifest["hermes_env_not_carried"] == []
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    assert ".hermes-data/.env" not in capsys.readouterr().out


def test_an_unreadable_hermes_env_is_reported_unknown_not_empty(tmp_path, capsys):
    """An empty parse is `unknown`, never `nothing was there`: a directory stands in for an
    unreadable file (root reads any mode, and the mutation lanes run as root)."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data" / ".env").mkdir()
    assert m._hermes_env_not_carried(pack, False) is None
    archive, manifest = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert manifest["hermes_env_not_carried"] is None
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    out = capsys.readouterr().out
    assert ".hermes-data/.env: NOT carried, and which keys it held is unknown (it was unreadable " \
        "at backup time)" in out


@pytest.mark.parametrize("names, expected", [
    (["BROWSER_HEADLESS", "bad name", 7, "X=1"], ["BROWSER_HEADLESS"]),
    ("BROWSER_HEADLESS", []),
    (None, []),
])
def test_hermes_env_names_from_the_manifest_are_filtered(names, expected):
    m = _mod()
    assert m._manifest_env_not_carried({"hermes_env_not_carried": names},
                                       "hermes_env_not_carried") == expected


def test_the_hermes_env_name_list_is_bounded_in_the_output(tmp_path, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    # 85 names: 85 - 40 = 45 tells subtraction from `%` (5) and `^` (125), which agree below 80.
    (pack / ".hermes-data" / ".env").write_text("".join(f"H{i:02d}=v\n" for i in range(85)))
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    assert m.main(["restore", str(archive), str(tmp_path / "t"), "--no-validate"]) == 0
    out = capsys.readouterr().out
    assert "H39" in out and "H40" not in out and "(+45 more)" in out


@pytest.mark.parametrize(("manifest", "reason"), [
    ({"include_secrets": False}, "archive predates this record"),
    ({}, "archive predates this record"),
    ({"include_secrets": False, "hermes_env_not_carried": None}, "it was unreadable at backup time"),
    ({"include_secrets": True}, ""),
    ({"include_secrets": True, "hermes_env_not_carried": None}, ""),
    ({"include_secrets": 1}, "archive predates this record"),   # untrusted JSON: only true is True
    ({"include_secrets": False, "hermes_env_not_carried": []}, ""),
    ({"include_secrets": False, "hermes_env_not_carried": ["A"]}, ""),
])
def test_hermes_env_unknown_reasons(manifest, reason):
    assert _mod()._hermes_env_unknown(manifest) == reason


def test_restoring_an_archive_from_before_the_record_says_unknown(tmp_path, capsys):
    """An archive written before okengine#994 B has no hermes_env_not_carried field. Silence would
    read as "nothing was dropped"; restore must say it does not know."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data" / ".env").write_text("BROWSER_HEADLESS=1\n")
    archive, manifest = m.create(pack, tmp_path / "bk", False, "20260101T080000Z")
    import tarfile as _tar
    old = tmp_path / "old.tar.gz"
    with _tar.open(archive, "r:gz") as src, _tar.open(old, "w:gz") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read()
            if member.name == m._MANIFEST:
                legacy = json.loads(data)
                del legacy["hermes_env_not_carried"]
                data = (json.dumps(legacy, indent=2) + "\n").encode()
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data))
    assert m.main(["restore", str(old), str(tmp_path / "t"), "--no-validate"]) == 0
    out = capsys.readouterr().out
    assert "which keys it held is unknown (archive predates this record)" in out


# --- okengine#1041 / #1042 backup follow-ups ---------------------------------

def _archives(d, names_and_ages):
    for name, age in names_and_ages:
        p = d / name
        p.write_text("x")
        os.utime(p, (time.time() - age, time.time() - age))


def test_prune_never_deletes_a_sibling_pack_whose_name_extends_this_one(tmp_path):
    """okengine#1042 [0]: `prune okcti` on a dest that also holds `okcti-test-*` archives globbed
    `okcti-*.tar.gz`, kept the newest N across both packs and deleted the sibling's DR history."""
    m = _mod()
    d = tmp_path / "shared"
    d.mkdir()
    _archives(d, [("okcti-20260101T000000Z.tar.gz", 50), ("okcti-20260102T000000Z.tar.gz", 40),
                  ("okcti-test-20260103T000000Z.tar.gz", 30),
                  ("okcti-test-20260104T000000Z.tar.gz", 20)])
    assert m.prune_backups(d, keep=1, pack_name="okcti") == 1
    assert sorted(f.name for f in d.iterdir()) == [
        "okcti-20260102T000000Z.tar.gz", "okcti-test-20260103T000000Z.tar.gz",
        "okcti-test-20260104T000000Z.tar.gz"], "the sibling pack's archives were pruned"
    assert m.prune_backups(d, keep=1, pack_name="okcti-test") == 1
    assert sorted(f.name for f in d.iterdir()) == [
        "okcti-20260102T000000Z.tar.gz", "okcti-test-20260104T000000Z.tar.gz"]


def test_list_counts_only_this_packs_archives_on_a_prefix_sharing_dest(tmp_path, capsys):
    """okengine#1042 [0]: `list` had the same prefix overcount as prune."""
    m = _mod()
    d = tmp_path / "shared"
    d.mkdir()
    _archives(d, [("okcti-20260101T000000Z.tar.gz", 50),
                  ("okcti-test-20260103T000000Z.tar.gz", 30),
                  ("okcti-20260101T000000Z.tar.gz.partial", 10),
                  ("okcti-notastamp.tar.gz", 5)])
    pack = tmp_path / "okcti"
    pack.mkdir()
    assert [f.name for f in m.list_backups(d, "okcti")] == ["okcti-20260101T000000Z.tar.gz"]
    assert m.main(["list", str(pack), "--dest", str(d)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 'okcti' backup(s)") and "okcti-test" not in out, out
    assert len(m.list_backups(d)) == 3, "an unscoped listing still shows every archive"


def test_a_pack_name_with_regex_metacharacters_matches_only_itself(tmp_path):
    m = _mod()
    d = tmp_path / "shared"
    d.mkdir()
    _archives(d, [("a.b-20260101T000000Z.tar.gz", 20), ("axb-20260101T000000Z.tar.gz", 10)])
    assert [f.name for f in m.list_backups(d, "a.b")] == ["a.b-20260101T000000Z.tar.gz"]


def _wal_only_db(path, rows=300):
    """A WAL-mode database whose committed rows live only in its -wal (no checkpoint yet). The
    connection is returned open: closing the last connection would checkpoint the WAL."""
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(rows)])
    con.commit()
    return con


def test_a_failed_online_backup_of_a_real_database_fails_the_backup(tmp_path, monkeypatch):
    """okengine#1042 [6]: an ENOSPC or memory error in the online backup fell back to a raw read of
    the main file without its -wal, so the archive held a torn or stale database labeled
    `online-backup-per-database`, and verify passed. A real database must fail the backup loudly."""
    import sqlite3
    m = _mod()
    pack = _pack(tmp_path)
    db = pack / ".hermes-data/state.db"
    con = _wal_only_db(db)
    try:
        assert (pack / ".hermes-data/state.db-wal").stat().st_size > 0
        monkeypatch.setattr(m, "_sqlite_snapshot_bytes", create_autospec(
            m._sqlite_snapshot_bytes, side_effect=sqlite3.OperationalError("database or disk is full")))
        with pytest.raises(m.SQLiteSnapshotError, match="state.db.*disk is full"):
            m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
        assert list((tmp_path / "bk").glob("*.tar.gz")) == [], "no archive may be published"
    finally:
        con.close()


def test_main_create_reports_a_failed_database_snapshot(tmp_path, monkeypatch, capsys):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data/state.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)
    monkeypatch.setattr(m, "_sqlite_snapshot_bytes", create_autospec(
        m._sqlite_snapshot_bytes, side_effect=MemoryError()))
    assert m.main(["create", str(pack), "--dest", str(tmp_path / "bk")]) == 1
    assert "state.db" in capsys.readouterr().err


def test_a_file_that_is_not_a_database_still_falls_back_to_its_bytes(tmp_path, monkeypatch):
    """The fallback exists for a `.db`/`.sqlite` name that is not a SQLite database at all; it must
    not be reached through a snapshot failure of a real one."""
    m = _mod()
    pack = _pack(tmp_path)
    rel = Path("wiki/data.db")
    (pack / rel).write_bytes(b"plain text, not sqlite")
    snapshot = create_autospec(m._sqlite_snapshot_bytes)
    monkeypatch.setattr(m, "_sqlite_snapshot_bytes", snapshot)
    assert m._file_bytes(pack, rel, include_secrets=True) == b"plain text, not sqlite"
    snapshot.assert_not_called()
    (pack / rel).write_bytes(b"")
    assert m._file_bytes(pack, rel, include_secrets=True) == b""


def test_a_wal_database_is_archived_with_its_wal_rows(tmp_path):
    import sqlite3
    m = _mod()
    pack = _pack(tmp_path)
    con = _wal_only_db(pack / ".hermes-data/state.db")
    try:
        data = m._file_bytes(pack, Path(".hermes-data/state.db"), include_secrets=True)
    finally:
        con.close()
    out = tmp_path / "copy.db"
    out.write_bytes(data)
    rcon = sqlite3.connect(str(out))
    try:
        assert rcon.execute("SELECT count(*) FROM t").fetchone()[0] == 300
    finally:
        rcon.close()


def _scheduler_holds_the_store_lock(jobs_dir):
    import fcntl
    jobs_dir.mkdir(parents=True, exist_ok=True)
    holder = os.open(jobs_dir / "jobs.json.lock", os.O_RDONLY | os.O_CREAT, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX)
    return holder


def test_restore_force_never_writes_the_job_store_outside_the_scheduler_lock(tmp_path):
    """okengine#1042 [7]: `restore --force` over a running gateway extracted jobs.json in place,
    with no lock, and re-armed it only after the whole extraction. A tick in between claimed every
    overdue lane from the archived next_run_at, or read a truncated file and saved an empty store.
    The restored store must appear only under the scheduler's lock, already re-armed."""
    m = _mod()
    pack = _pack(tmp_path)
    stale = {"jobs": [{"name": "daily", "next_run_at": "2026-01-01T07:00:00+00:00"}]}
    (pack / ".hermes-data/cron-plus/jobs.json").write_text(json.dumps(stale))
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    target = tmp_path / "live"
    live_jobs = target / ".hermes-data/cron-plus/jobs.json"
    holder = _scheduler_holds_the_store_lock(live_jobs.parent)
    live_jobs.write_text('{"jobs": [{"name": "live", "next_run_at": "2099-01-01T00:00:00+00:00"}]}')
    done = threading.Event()
    worker = threading.Thread(target=lambda: (m.restore(archive, target, force=True), done.set()))
    try:
        worker.start()
        deadline = time.time() + 5
        while not (target / "wiki/entities/a/acme.md").exists() and time.time() < deadline:
            time.sleep(0.02)
        assert not done.wait(0.3), "restore finished without the scheduler's store lock"
        assert json.loads(live_jobs.read_text())["jobs"][0]["name"] == "live", (
            "the archived job store was written while the scheduler held its lock")
    finally:
        os.close(holder)
    worker.join(10)
    assert done.is_set()
    assert json.loads(live_jobs.read_text()) == {"jobs": [{"name": "daily", "next_run_at": None}]}
    leftovers = [p.name for p in live_jobs.parent.iterdir()
                 if p.name not in ("jobs.json", "jobs.json.lock")]
    assert leftovers == [], f"staged job-store files left behind: {leftovers}"


def test_restore_keeps_the_archived_job_store_mode(tmp_path):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data/cron-plus/jobs.json").chmod(0o600)
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    target = tmp_path / "restored"
    previous = os.umask(0o002)
    try:
        assert m.restore(archive, target, force=False)[0]
    finally:
        os.umask(previous)
    assert oct((target / ".hermes-data/cron-plus/jobs.json").stat().st_mode & 0o777) == oct(0o600)


def test_restore_rejects_an_invalid_archived_job_store_without_publishing_it(tmp_path):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data/cron-plus/jobs.json").write_text('{"jobs": [], "extra": 1}')
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    target = tmp_path / "restored"
    with pytest.raises(ValueError, match="not a job list"):
        m.restore(archive, target, force=False)
    assert sorted(p.name for p in (target / ".hermes-data/cron-plus").iterdir()) == [
        "jobs.json.lock"]


def test_restore_preserves_directory_modes_and_empty_directories(tmp_path):
    """okengine#1042 [29]: the archive held no directory entries, so a restore created
    `.hermes-data` (0700) with the umask (0775 under 002) and dropped empty directories such as
    `cron-plus/model-slots` (0700)."""
    m = _mod()
    pack = _pack(tmp_path)
    slots = pack / ".hermes-data/cron-plus/model-slots"
    slots.mkdir()
    slots.chmod(0o700)
    (pack / ".hermes-data").chmod(0o700)
    (pack / "wiki/entities").chmod(0o750)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    assert manifest["dir_modes"][".hermes-data"] == 0o700
    assert ".hermes-data/logs" not in manifest["dir_modes"], "excluded trees are not recorded"
    assert "." not in manifest["dir_modes"] and "" not in manifest["dir_modes"]
    assert ".git" not in manifest["dir_modes"]
    target = tmp_path / "restored"
    previous = os.umask(0o002)
    try:
        assert m.restore(archive, target, force=False)[0]
    finally:
        os.umask(previous)
    modes = {rel: oct((target / rel).stat().st_mode & 0o777)
             for rel in (".hermes-data", ".hermes-data/cron-plus/model-slots", "wiki/entities")}
    assert modes == {".hermes-data": oct(0o700), ".hermes-data/cron-plus/model-slots": oct(0o700),
                     "wiki/entities": oct(0o750)}


def test_restore_dir_modes_never_follow_a_symlink_or_leave_the_target(tmp_path):
    m = _mod()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    target = tmp_path / "t"
    target.mkdir()
    (target / "wiki").symlink_to(outside)
    m._restore_dir_modes(target, {"dir_modes": {
        "wiki": 0o700, "../escape": 0o700, "/abs": 0o700, "ok": 0o750, "ok/sub": "bad",
        "x\x00y": 0o700, "": 0o700, ".": 0o700, "ok/../ok2": 0o700, "wiki/sub": 0o700,
        "deep/er/dir": 0o751}})
    assert oct(outside.stat().st_mode & 0o777) == oct(0o755), "chmod followed a symlink"
    assert not (outside / "sub").exists(), "a directory was created through a symlink"
    assert oct((target / "deep/er/dir").stat().st_mode & 0o777) == oct(0o751)
    assert not (tmp_path / "escape").exists() and not Path("/abs").exists()
    assert oct((target / "ok").stat().st_mode & 0o777) == oct(0o750)
    assert not (target / "ok2").exists()
    m._restore_dir_modes(target, {"dir_modes": "not a mapping"})
    m._restore_dir_modes(target, {})


def test_restore_dir_modes_strip_special_bits(tmp_path):
    m = _mod()
    target = tmp_path / "t"
    target.mkdir()
    m._restore_dir_modes(target, {"dir_modes": {"d": 0o4777 | 0o2000}})
    assert oct((target / "d").stat().st_mode & 0o7777) == oct(0o777)


def test_restore_dir_modes_apply_after_the_children_exist(tmp_path):
    """A read-only parent (0500) still gets its empty child created first."""
    m = _mod()
    target = tmp_path / "t"
    target.mkdir()
    try:
        m._restore_dir_modes(target, {"dir_modes": {"ro": 0o500, "ro/child": 0o700}})
        assert (target / "ro/child").is_dir()
        assert oct((target / "ro").stat().st_mode & 0o777) == oct(0o500)
    finally:
        (target / "ro").chmod(0o700)


def test_in_place_writers_are_copied_before_the_carried_over_set_takes_the_cap(tmp_path,
                                                                               monkeypatch):
    """okengine#1041 item 1: the carried-over `copy` set and the must-copy files shared one cap in
    walk order, so after a mass metadata event a known in-place writer such as log.md fell past
    the cap on every attempt."""
    m = _mod()
    pack = _pack(tmp_path)
    pages = []
    for n in range(8):
        rel = Path(f"wiki/aa/p{n}.md")
        (pack / rel).parent.mkdir(parents=True, exist_ok=True)
        (pack / rel).write_text(f"page {n}", encoding="utf-8")
        pages.append(rel)
    (pack / "wiki/log.md").write_text("- first\n", encoding="utf-8")
    _settle(pack)
    monkeypatch.setattr(m, "_FORCED_COPY_MAX_FILES", 3)
    monkeypatch.setattr(m, "_COPY_BUDGET_BYTES", 0)
    files = sorted(pages + [Path("wiki/log.md")], key=lambda r: r.as_posix())
    staged, refs = m._snapshot_corpus(pack, files, tmp_path / "stage", link=True,
                                      copy=frozenset(pages))
    assert Path("wiki/log.md") in staged and Path("wiki/log.md") not in refs, (
        "the in-place writer lost its forced copy to the carried-over set")
    copied = [rel for rel in pages if rel in staged and rel not in refs]
    assert len(copied) == 2, copied


def test_a_mass_same_owner_chown_never_starves_an_appended_log(tmp_path, monkeypatch, capsys):
    """okengine#1041 item 1, end to end: `chown -R` to the same owner moves every file's ctime (a
    rewrite by _changed_since), while log.md keeps being appended. log.md must still be archived at
    its fence-time bytes on the attempt that is kept."""
    m = _mod()
    pack = _pack(tmp_path)
    for n in range(12):
        (pack / f"wiki/aa/p{n:02d}.md").parent.mkdir(parents=True, exist_ok=True)
        (pack / f"wiki/aa/p{n:02d}.md").write_text(f"page {n}", encoding="utf-8")
    log = pack / "wiki/log.md"
    log.write_text("- 0\n", encoding="utf-8")
    _settle(pack)
    monkeypatch.setattr(m, "_FORCED_COPY_MAX_FILES", 4)
    monkeypatch.setattr(m, "_COPY_BUDGET_BYTES", 0)
    uid, gid = os.getuid(), os.getgid()

    def chown_and_append(round_):
        _after_the_ctime_tick_of_all((pack / "wiki").rglob("*.md"))
        for path in (pack / "wiki").rglob("*.md"):
            os.chown(path, uid, gid)
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(f"- {round_ + 1}\n")

    rounds = _write_during_archive(m, monkeypatch, pack, "engine.version", chown_and_append,
                                   rounds=3)
    archive, manifest = m.create(pack, tmp_path / "bk", False, "chown")
    assert len(rounds) == 3
    assert "wiki/log.md" not in manifest["consistency"]["rewritten_after_fence"]
    with tarfile.open(archive) as tar:
        assert tar.extractfile("wiki/log.md").read() == b"- 0\n- 1\n- 2\n"


def _clean_env():
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    return env


@pytest.mark.parametrize("entry", ["wrapper", "direct"])
def test_backup_cli_runs_as_production_invokes_it(tmp_path, entry):
    """okengine#1041 item 3 (testing.real-invocation): `bin/framework backup …` and a direct
    `python scripts/framework_backup.py …`, with no test PYTHONPATH and the cwd outside the repo.
    Run directly, the module could not import `okengine` (it lives under src/)."""
    import subprocess
    import sys
    pack = _pack(tmp_path)
    if entry == "wrapper":
        argv = [sys.executable, str(REPO / "bin/framework"), "backup"]
    else:
        argv = [sys.executable, str(REPO / "scripts/framework_backup.py")]

    def run(*args):
        return subprocess.run([*argv, *args], cwd=tmp_path, env=_clean_env(), capture_output=True,
                              text=True, timeout=120)

    created = run("create", str(pack), "--dest", str(tmp_path / "bk"))
    assert created.returncode == 0, created.stderr
    archive = next((tmp_path / "bk").glob("vault-*.tar.gz"))
    verified = run("verify", str(archive))
    assert verified.returncode == 0, verified.stderr
    restored = run("restore", str(archive), str(tmp_path / "out"), "--no-validate")
    if _REAL_ROOT:                        # the mutation lanes run as root: refused by design
        assert restored.returncode == 2 and "refusing to restore as root" in restored.stderr
    else:
        assert restored.returncode == 0, restored.stderr
        assert (tmp_path / "out/wiki/entities/a/acme.md").read_text() == "# Acme\n"


def test_the_archive_name_list_and_prune_match_is_the_one_create_writes():
    """The pack-scoped match is exact, so it must agree with main()'s stamp format."""
    import re
    from datetime import datetime, timezone
    m = _mod()
    name = f"okcti-{datetime.now(timezone.utc).strftime(m._STAMP_FORMAT)}.tar.gz"
    assert re.fullmatch(re.escape("okcti") + m._STAMP_SUFFIX, name)
    assert not re.fullmatch(re.escape("okcti") + m._STAMP_SUFFIX,
                            f"okcti-test-{datetime.now(timezone.utc).strftime(m._STAMP_FORMAT)}.tar.gz")


def test_dir_modes_skip_a_directory_removed_since_the_walk(tmp_path):
    m = _mod()
    (tmp_path / "kept").mkdir()
    (tmp_path / "kept").chmod(0o750)
    (tmp_path / "kept").chmod(0o755)
    (tmp_path / "link").symlink_to(tmp_path / "kept")
    assert m._dir_modes(tmp_path, [Path("gone"), Path("link"), Path("kept")]) == {"kept": 0o755}


def test_loading_the_module_puts_the_engine_src_on_the_import_path(monkeypatch):
    """The in-process half of the direct-invocation fix: the module adds src/ itself."""
    import sys
    src = str(REPO / "src")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != src])
    _mod()
    assert sys.path[0] == src


def test_restore_without_a_job_store_and_with_a_file_where_a_dir_was(tmp_path):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data/cron-plus/jobs.json").unlink()
    archive, manifest = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    target = tmp_path / "restored"
    target.mkdir()
    (target / "crons").write_text("a file where the archive had a directory")
    manifest["dir_modes"] = {"crons": 0o700, "zz": 0o751}
    m._restore_dir_modes(target, manifest)
    assert (target / "crons").is_file()
    assert oct((target / "zz").stat().st_mode & 0o777) == oct(0o751), "one blocked dir stopped the rest"
    assert m.restore(archive, tmp_path / "restored2", force=False)[0]
    assert not (tmp_path / "restored2/.hermes-data/cron-plus/jobs.json").exists()


@pytest.mark.parametrize("window", ["SETTLE", "HOT"])
def test_the_copy_windows_include_their_exact_boundary(tmp_path, monkeypatch, window):
    """A file modified exactly SETTLE_NS (or _HOT_NS) before the fence is still inside the window."""
    m = _mod()
    pack = _pack(tmp_path)
    page = pack / "wiki/page.md"
    page.write_text("page", encoding="utf-8")
    mtime = page.stat().st_mtime_ns
    span = m.SETTLE_NS if window == "SETTLE" else m._HOT_NS
    monkeypatch.setattr(m.time, "time_ns", lambda: mtime + span)
    if window == "HOT":
        monkeypatch.setattr(m, "_FORCED_COPY_MAX_FILES", 0)
    else:
        monkeypatch.setattr(m, "_COPY_BUDGET_BYTES", 0)
    staged, refs = m._snapshot_corpus(pack, [Path("wiki/page.md")], tmp_path / "s", link=True)
    assert Path("wiki/page.md") in staged and Path("wiki/page.md") not in refs
    monkeypatch.setattr(m.time, "time_ns", lambda: mtime + span + 1)
    staged, refs = m._snapshot_corpus(pack, [Path("wiki/page.md")], tmp_path / "s2", link=True)
    assert refs[Path("wiki/page.md")][0] == "link", "one nanosecond past the window is linked"


def test_a_forced_copy_never_follows_a_symlink(tmp_path):
    m = _mod()
    pack = _pack(tmp_path)
    (pack / "wiki/real.md").write_text("real", encoding="utf-8")
    (pack / "wiki/log.md").symlink_to(pack / "wiki/real.md")
    staged, _refs = m._snapshot_corpus(pack, [Path("wiki/log.md")], tmp_path / "s", link=False)
    assert (tmp_path / "s/wiki/log.md").is_symlink()


def test_the_restored_job_store_keeps_its_format_and_archived_mode_rule(tmp_path):
    """Owner rw is added; group/other write is kept like every other restored file's
    (okengine#1044 [23]: a dropped write bit locked a --fix-perms gateway out of its own store)."""
    m = _mod()
    pack = _pack(tmp_path)
    jobs = pack / ".hermes-data/cron-plus/jobs.json"
    jobs.write_text('{"jobs": [{"name": "a", "next_run_at": "x"}]}')
    jobs.chmod(0o467)
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    target = tmp_path / "restored"
    assert m.restore(archive, target, force=False)[0]
    restored = target / ".hermes-data/cron-plus/jobs.json"
    assert oct(restored.stat().st_mode & 0o777) == oct(0o667)
    assert restored.read_text() == json.dumps(
        {"jobs": [{"name": "a", "next_run_at": None}]}, indent=2) + "\n"


def test_a_job_store_alone_in_its_directories_is_restored(tmp_path):
    m = _mod()
    pack = tmp_path / "solo"
    (pack / ".hermes-data/cron-plus").mkdir(parents=True)
    (pack / "pack.yaml").write_text("name: solo\n")
    (pack / ".hermes-data/cron-plus/jobs.json").write_text('[{"name": "a", "next_run_at": "x"}]')
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    target = tmp_path / "restored"
    if _REAL_ROOT:      # the directories here are made by the job-store restore, not extractall
        real = m._restore_job_store
        m._restore_job_store = lambda tar, member, tgt: (real(tar, member, tgt),
                                                         _as_deploy_user(tgt))[0]
    assert m.restore(archive, target, force=False)[0]
    assert json.loads((target / ".hermes-data/cron-plus/jobs.json").read_text()) == [
        {"name": "a", "next_run_at": None}]


# --- okengine#1044 [19]/[22]/[23]: restore reports what it could not restore ------------------

def _without_dir_modes(archive: Path, out: Path) -> Path:
    """The archive a pre-v0.15.13 backup wrote: the same files, a manifest with no dir_modes."""
    with tarfile.open(archive, "r:gz") as source, tarfile.open(out, "w:gz") as legacy:
        for member in source.getmembers():
            data = source.extractfile(member).read()
            if member.name == "MANIFEST.json":
                manifest = json.loads(data)
                del manifest["dir_modes"]
                data = json.dumps(manifest).encode()
                member.size = len(data)
            legacy.addfile(member, io.BytesIO(data))
    return out


def test_restoring_an_archive_without_dir_modes_says_directory_modes_were_not_restored(
        tmp_path, capsys):
    """[19]: a pre-v0.15.13 archive has no dir_modes; restore skipped them without a word."""
    m = _mod()
    pack = _pack(tmp_path)
    (pack / ".hermes-data").chmod(0o700)
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    legacy = _without_dir_modes(archive, tmp_path / "legacy.tar.gz")

    ok, _, warnings = m.restore(legacy, tmp_path / "t", force=False)
    capsys.readouterr()
    assert m.main(["restore", str(legacy), str(tmp_path / "t2"), "--no-validate"]) == 0
    out = capsys.readouterr().out

    assert ok
    assert [name for name, _ in warnings] == ["dir_modes"], warnings
    assert "directory modes were not restored" in warnings[0][1]
    assert "⚠" in out and "directory modes were not restored" in out, out


def test_a_current_archive_restores_without_warnings(tmp_path):
    m = _mod()
    archive, _ = m.create(_pack(tmp_path), tmp_path / "bk", False, "20260101T000000Z")
    assert m.restore(archive, tmp_path / "t", force=False) == (True, m.verify(archive)[1], [])


def test_a_directory_mode_that_cannot_be_applied_is_reported_and_identity_still_rebinds(
        tmp_path, monkeypatch, capsys):
    """[22]: chmod on a foreign-owned directory raises EPERM. It used to escape restore() after
    extraction and before HERMES_UID/HERMES_GID were rebound in .env."""
    m = _mod()
    pack = _pack(tmp_path)
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    real_chmod = os.chmod

    def chmod(path, mode, *args, **kwargs):
        if Path(path).as_posix().endswith(".hermes-data/cron-plus"):
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_chmod(path, mode, *args, **kwargs)

    target = tmp_path / "t"
    with monkeypatch.context() as patched:   # undone before the tree-cleanup fixture chmods
        patched.setattr(m.os, "chmod", chmod)
        ok, _, warnings = m.restore(archive, target, force=False)
        capsys.readouterr()
        assert m.main(["restore", str(archive), str(tmp_path / "t2"), "--no-validate"]) == 0
        out = capsys.readouterr().out

    assert ok
    assert "HERMES_UID=" in (target / ".env").read_text()
    assert [name for name, _ in warnings] == [".hermes-data/cron-plus"], warnings
    assert warnings[0][1].startswith("directory mode 0o"), warnings
    assert warnings[0][1].endswith(" not restored: Operation not permitted"), warnings
    assert "⚠" in out and ".hermes-data/cron-plus" in out, out


def test_a_directory_that_cannot_be_created_is_reported(tmp_path):
    m = _mod()
    target = tmp_path / "t"
    target.mkdir()
    (target / "crons").write_text("a file where the archive had a directory")
    warnings = m._restore_dir_modes(target, {"dir_modes": {"crons": 0o700, "zz": 0o751}})
    assert warnings == [("crons", "directory not recreated: File exists")]


def test_restore_reapplies_the_archived_group_and_other_write_bits_on_files(tmp_path):
    """[23]: the `data` filter strips group/other write from every file, while dir_modes restores
    a --fix-perms `.hermes-data` at 0777. The container uid then passed ensure-runtime's
    directory-only writability check but could not write config.yaml or jobs.json."""
    m = _mod()
    pack = _pack(tmp_path)
    modes = {".hermes-data/config.yaml": 0o666, ".hermes-data/cron-plus/jobs.json": 0o666,
             "wiki/entities/a/acme.md": 0o664, "pack.yaml": 0o644, "engine.version": 0o600}
    script = pack / "wiki/run.sh"
    script.write_text("#!/bin/sh\n")
    modes["wiki/run.sh"] = 0o775
    for rel, mode in modes.items():
        (pack / rel).chmod(mode)
    for directory in (".hermes-data", ".hermes-data/cron-plus"):
        (pack / directory).chmod(0o777)
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    target = tmp_path / "t"
    previous = os.umask(0o022)
    try:
        ok, _, warnings = m.restore(archive, target, force=False)
    finally:
        os.umask(previous)
    assert ok and warnings == []
    restored = {rel: oct((target / rel).stat().st_mode & 0o7777) for rel in modes}
    assert restored == {rel: oct(mode) for rel, mode in modes.items()}


def test_file_modes_never_follow_a_symlink_or_set_special_bits(tmp_path):
    m = _mod()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("x")
    (outside / "f").chmod(0o644)
    target = tmp_path / "t"
    (target / "real").mkdir(parents=True)
    (target / "real").chmod(0o755)
    files = {"real/f": 0o644, "real/open": 0o666, "real/exec": 0o644}
    for rel, mode in files.items():
        (target / rel).write_text("x")
        (target / rel).chmod(mode)
    (target / "link").symlink_to(outside)

    def member(name, mode, kind=tarfile.REGTYPE):
        info = tarfile.TarInfo(name)
        info.mode, info.type = mode, kind
        return info

    warnings = m._restore_file_modes(target, [
        member("link/f", 0o666), member("real/f", 0o6666), member("real", 0o777, tarfile.DIRTYPE),
        member("real/open", 0o666), member("real/exec", 0o645), member("gone", 0o4667)])
    modes = {rel: oct((target / rel).stat().st_mode & 0o7777)
             for rel in ("real", "real/f", "real/open", "real/exec")}
    assert oct((outside / "f").stat().st_mode & 0o7777) == oct(0o644), "chmod followed a symlink"
    assert modes == {"real": oct(0o755),            # directories are dir_modes' job
                     "real/f": oct(0o666),          # write bits back, special bits never
                     "real/open": oct(0o666),       # already writable: unchanged
                     "real/exec": oct(0o644)}       # only write bits are re-added
    assert warnings == [("gone", "file mode 0o667 not restored: No such file or directory")]


def test_a_crafted_job_store_mode_never_restores_special_bits(tmp_path):
    m = _mod()
    pack = _pack(tmp_path)
    archive, _ = m.create(pack, tmp_path / "bk", False, "20260101T000000Z")
    crafted = tmp_path / "crafted.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(crafted, "w:gz") as out:
        for member in source.getmembers():
            if member.name == ".hermes-data/cron-plus/jobs.json":
                member.mode = 0o4666
            out.addfile(member, source.extractfile(member))
    assert m.restore(crafted, tmp_path / "t", force=False)[0]
    restored = tmp_path / "t/.hermes-data/cron-plus/jobs.json"
    assert oct(restored.stat().st_mode & 0o7777) == oct(0o666)
