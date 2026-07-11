"""invariant-audit M12 — patch 01 (the Hermes file-tool write-guard) must also refuse engine-managed
reserved VAULT files, so the file tool can't be a weaker second write path around the enforced
okengine-write MCP contract (a cron agent carries both).

The guard lives in a carried patch against Hermes' tools/file_operations.py, so we can't import it
directly. Instead we (1) assert the patch WIRES the guard into both write() and patch(), and (2)
EXTRACT the actual `_reserved_file_reject` source from the diff, exec it, and test its behavior — so
the test validates the code that really ships, not a copy."""
import re
from pathlib import Path
from typing import Optional  # noqa: F401  (the extracted function annotates -> Optional[str])

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCH = REPO / "patches" / "01-file-operations-write-guard.patch"

pytestmark = pytest.mark.skipif(not PATCH.is_file(), reason="patch 01 absent (scrubbed tree)")


def _added_lines() -> str:
    return "\n".join(l[1:] for l in PATCH.read_text().splitlines()
                     if l.startswith("+") and not l.startswith("+++"))


def _hunk1_added() -> str:
    """The added lines of the FIRST hunk only — all module-level defs (the read-echo helpers,
    _schema_reject, _RESERVED_VAULT_FILES, _reserved_file_reject). Later hunks add method-body
    fragments that aren't valid at module scope, so isolate hunk 1 to exec cleanly."""
    out, hunks, in_h1 = [], 0, False
    for l in PATCH.read_text().splitlines():
        if l.startswith("@@"):
            hunks += 1
            in_h1 = hunks == 1
            continue
        if in_h1 and l.startswith("+") and not l.startswith("+++"):
            out.append(l[1:])
    return "\n".join(out)


def test_patch_wires_reserved_guard_into_every_write_leg():
    body = PATCH.read_text()
    assert "def _reserved_file_reject" in body, "patch 01 no longer defines the reserved-file guard"
    # every file-tool write leg must consult the guard: full write, str-replace patch, move, delete
    assert "WriteResult(error=f\"Write rejected: {_rf}\")" in body, "write() leg unguarded"
    assert "PatchResult(error=f\"Patch rejected: {_rf}\")" in body, "patch_replace() leg unguarded"
    assert "WriteResult(error=f\"Move denied: {_rf}\")" in body, "move_file() leg unguarded (V4A Move bypass)"
    assert "WriteResult(error=f\"Delete denied: {_rf}\")" in body, "delete leg unguarded (V4A Delete bypass)"
    # a Move into an existing DIRECTORY lands at DIR/basename(src) — the guard must evaluate the
    # EFFECTIVE destination, not the bare dir (which isn't .md and would slip the check) — re-verify.
    assert "_eff_dst" in body and "os.path.isdir(dst)" in body, \
        "move_file must guard the effective dir-destination path, not the bare directory"


def _load_guard():
    src = _hunk1_added()
    assert "def _reserved_file_reject" in src, "hunk 1 no longer defines the reserved-file guard"
    ns: dict = {"Optional": Optional, "re": re}   # hunk 1 also defines the re-based read-echo helpers
    exec(compile(src, "<patch01-hunk1>", "exec"), ns)   # noqa: S102 — executing our own patched source
    return ns["_reserved_file_reject"]


def test_reserved_guard_refuses_engine_files_allows_the_rest(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(REPO))   # the exec'd guard imports tools.schema_validator at call time
    fn = _load_guard()
    (tmp_path / "wiki").mkdir()
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))

    def under(p):
        fp = tmp_path / "wiki" / p
        fp.parent.mkdir(parents=True, exist_ok=True)
        return str(fp)

    # engine-managed structural vault files -> REFUSED (basename check; file need not exist)
    for p in ("HOT.md", "log.md", "INDEX.md", "INDEX-p02.md", "health.md", "bundle.md",
              "agents.md", "_review-queue.md", ".hidden.md"):
        assert fn(under(p)), f"{p} must be refused via the file tool"
    # normal knowledge pages -> WRITABLE
    for p in ("entities/a/apt.md", "sources/2026/07/report.md", "concepts/c2.md"):
        assert fn(under(p)) is None, f"{p} is a normal page and must stay writable"
    # the bare-`_` reshard bucket — the DIR is `_`, the BASENAME leaf x-force.md is fine (batch-2 over-drop)
    assert fn(under("entities/x/_/x-force.md")) is None, "reshard-bucket page must stay writable"
    # non-vault + non-.md paths -> WRITABLE (the file tool is used for logs/scripts too)
    assert fn("/opt/data/logs/_temp.py") is None
    assert fn(str(tmp_path.parent / "outside" / "_draft.md")) is None   # .md OUTSIDE the vault

    # (re-verify M12) PACK-declared reserved_files are refused too, mirroring the MCP write path
    (tmp_path / "wiki" / "schema.yaml").write_text("reserved_files: [mission.md]\n", encoding="utf-8")
    assert fn(under("mission.md")), "a pack-declared reserved file must be refused via the file tool"

    # (re-verify M12/M18) a tombstoned NORMAL page (reserved by CONTENT, not basename) must be refused
    ghost = under("entities/g/ghost.md")
    Path(ghost).write_text("---\ntype: entity\nstatus: tombstoned\n---\nbody\n", encoding="utf-8")
    assert fn(ghost), "a tombstoned normal page must not be resurrectable via the file tool"
    live = under("entities/g/live.md")
    Path(live).write_text("---\ntype: entity\nstatus: active\n---\nbody\n", encoding="utf-8")
    assert fn(live) is None, "a live normal page stays writable"

    # (re-verify) a tombstoned page with LARGE (>4KB) frontmatter — the status line sits past a
    # truncated read, so a 4096-byte read would fail open. The guard must read the whole page.
    big = under("entities/b/big.md")
    bulk = "\n".join(f"alias_{i}: value-{i}-padding-padding-padding" for i in range(400))  # ~>4KB
    Path(big).write_text(f"---\ntype: entity\n{bulk}\nstatus: tombstoned\n---\nbody\n", encoding="utf-8")
    assert fn(big), "a tombstoned page with >4KB frontmatter must still be refused (no read truncation)"
    # CRLF + mixed-case status must also be caught
    crlf = under("entities/c/crlf.md")
    Path(crlf).write_bytes(b"---\r\ntype: entity\r\nstatus: Tombstoned\r\n---\r\nbody\r\n")
    assert fn(crlf), "a CRLF / mixed-case tombstone must be refused"

    # fail-open when WIKI_PATH is unset (never block a legit write)
    monkeypatch.delenv("WIKI_PATH", raising=False)
    assert fn(under("HOT.md")) is None
