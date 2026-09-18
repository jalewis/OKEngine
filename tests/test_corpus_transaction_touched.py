"""okengine#666: every MCP write SHA-256'd the entire vault twice under the exclusive fence.

`corpus_transaction.mutation` snapshotted every `wiki/**/*.md` before AND after the tool ran
(plus once more on recovery). Measured: 20k pages -> 1.3 s per no-op call warm; 60k -> 3.5 s.
Paid on rejected calls, serialized fleet-wide, against a 30 s lock timeout -- the id-index
first-write timeout and the #650 wedged-lock window, re-created at the fence layer.

The fence now has two tracking modes. `tracking="snapshot"` keeps the full diff for batch writers
(operations, projection reconciliation). `tracking="touched"` is what the per-tool MCP fence uses:
the write services call `corpus_transaction.touch(path)` BEFORE mutating a file, which records the
before-digest of that one file; the journal is built from the touched set only. Cost is O(files
touched), never O(vault), and a killed writer's recovery replays the touched set it left behind.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
corpus = importlib.import_module("okengine.corpus_transaction")


def _vault(tmp_path: Path, n: int) -> Path:
    wiki = tmp_path / "wiki" / "entities"
    wiki.mkdir(parents=True)
    for i in range(n):
        (wiki / f"p{i}.md").write_text(f"---\ntype: entity\n---\nbody {i}\n")
    return tmp_path


def _journal(tmp_path: Path) -> list[dict]:
    p = tmp_path / ".okengine" / "corpus" / "journal.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_touched_mode_journals_only_touched_paths_and_advances_the_epoch(tmp_path):
    v = _vault(tmp_path, 200)
    a = v / "wiki" / "entities" / "p1.md"
    new = v / "wiki" / "entities" / "fresh.md"
    with corpus.mutation(v, writer="t", operation="op", tracking="touched"):
        corpus.touch(a)
        a.write_text("---\ntype: entity\n---\nchanged\n")
        corpus.touch(new)                      # a page that does not exist yet
        new.write_text("---\ntype: entity\n---\nnew\n")
        corpus.touch(v / "wiki" / "entities" / "p2.md")   # touched but NOT changed
    rec = _journal(v)[-1]
    assert rec["status"] == "committed" and rec["epoch"] == 1
    assert [x["path"] for x in rec["affected_paths"]] == ["wiki/entities/fresh.md", "wiki/entities/p1.md"]
    fresh = next(x for x in rec["affected_paths"] if x["path"].endswith("fresh.md"))
    assert fresh["before_sha256"] is None and fresh["after_sha256"]
    assert corpus.read_epoch(v) == 1


def test_touched_mode_never_hashes_the_rest_of_the_vault(tmp_path, monkeypatch):
    v = _vault(tmp_path, 500)
    calls = []
    real = corpus._digest
    monkeypatch.setattr(corpus, "_digest", lambda p: (calls.append(p), real(p))[1])
    with corpus.mutation(v, writer="t", operation="noop", tracking="touched"):
        pass
    assert calls == [], "a no-op mutation must not read a single page"
    with corpus.mutation(v, writer="t", operation="one", tracking="touched"):
        target = v / "wiki" / "entities" / "p7.md"
        corpus.touch(target)
        target.write_text("x")
    assert len(calls) == 2 and all(p == target for p in calls), calls
    assert corpus.read_epoch(v) == 1


def test_snapshot_mode_is_unchanged_for_batch_writers(tmp_path):
    v = _vault(tmp_path, 5)
    with corpus.mutation(v, writer="batch", operation="op"):     # default: snapshot
        (v / "wiki" / "entities" / "p3.md").write_text("y")      # no touch() call
    rec = _journal(v)[-1]
    assert [x["path"] for x in rec["affected_paths"]] == ["wiki/entities/p3.md"]


def test_touch_outside_a_touched_transaction_is_a_harmless_no_op(tmp_path):
    v = _vault(tmp_path, 2)
    corpus.touch(v / "wiki" / "entities" / "p0.md")                # no transaction at all
    with corpus.mutation(v, writer="batch", operation="op"):        # snapshot mode
        corpus.touch(v / "wiki" / "entities" / "p0.md")
        (v / "wiki" / "entities" / "p0.md").write_text("z")
    rec = _journal(v)[-1]
    assert [x["path"] for x in rec["affected_paths"]] == ["wiki/entities/p0.md"]


def test_killed_touched_writer_is_recovered_from_its_touched_set(tmp_path):
    v = _vault(tmp_path, 50)
    target = v / "wiki" / "entities" / "p9.md"
    before = corpus._digest(target)
    # simulate a writer killed after publishing the file but before finishing the receipt
    state = v / ".okengine" / "corpus"
    state.mkdir(parents=True)
    corpus._atomic_json(state / "active.json", {
        "transaction_id": "dead", "writer": "w", "operation": "op", "started_at": corpus._now(),
        "tracking": "touched", "touched": {"wiki/entities/p9.md": before},
    })
    target.write_text("published by the dead writer")
    with corpus.stable_corpus(v) as epoch:
        assert epoch == 1
    rec = _journal(v)[-1]
    assert rec["status"] == "recovered"
    assert [x["path"] for x in rec["affected_paths"]] == ["wiki/entities/p9.md"]
    assert rec["affected_paths"][0]["before_sha256"] == before


def test_mcp_write_fence_uses_touched_mode_end_to_end(tmp_path, monkeypatch):
    """The per-tool fence in write_server must not scan the vault: create one page in a vault of
    300 and count digest calls."""
    pytest.importorskip("mcp")
    pytest.importorskip("yaml")
    v = _vault(tmp_path, 300)
    (v / "wiki" / "schema.yaml").write_text("types:\n  entity: {required: [type]}\n")
    monkeypatch.setenv("WIKI_PATH", str(v))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-16")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    spec = importlib.util.spec_from_file_location("write_server_touched", REPO / "okengine-mcp" / "write_server.py")
    ws = importlib.util.module_from_spec(spec)
    sys.modules["write_server_touched"] = ws
    spec.loader.exec_module(ws)
    calls = []
    real = corpus._digest
    monkeypatch.setattr(corpus, "_digest", lambda p: (calls.append(p), real(p))[1])
    tool = ws.mcp._tool_manager._tools["create_entity"].fn
    out = tool("entities/z/zeta", "type: entity\ntitle: Zeta", "Body.")
    assert out.startswith("created"), out
    rec = _journal(v)[-1]
    touched = {x["path"] for x in rec["affected_paths"]}
    assert any(p.endswith("zeta.md") for p in touched), touched
    assert len(calls) < 12, f"fence hashed {len(calls)} files for one create"


def test_touch_ignores_paths_outside_the_deployment_and_non_wiki_files(tmp_path, monkeypatch):
    v = _vault(tmp_path, 3)
    outside = tmp_path.parent / f"{tmp_path.name}-elsewhere.md"
    outside.write_text("x")
    calls = []
    real = corpus._digest
    monkeypatch.setattr(corpus, "_digest", lambda p: (calls.append(p), real(p))[1])
    with corpus.mutation(v, writer="t", operation="op", tracking="touched"):
        corpus.touch(outside)                                   # not under the deployment
        corpus.touch(v / "raw" / "capture.json")                # not a wiki page
        corpus.touch(v / "wiki" / "entities" / "p0.md")         # counted once ...
        corpus.touch(v / "wiki" / "entities" / "p0.md")         # ... even when declared twice
        (v / "wiki" / "entities" / "p0.md").write_text("changed")
    rec = _journal(v)[-1]
    assert [x["path"] for x in rec["affected_paths"]] == ["wiki/entities/p0.md"]
    assert calls.count(v / "wiki" / "entities" / "p0.md") == 2 and len(calls) == 2


def test_unreadable_touched_page_journals_an_unknown_before_digest(tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    v = _vault(tmp_path, 2)
    target = v / "wiki" / "entities" / "p1.md"
    target.chmod(0)
    try:
        with corpus.mutation(v, writer="t", operation="op", tracking="touched"):
            corpus.touch(target)
            target.chmod(0o644)
            target.write_text("now readable")
    finally:
        target.chmod(0o644)
    rec = _journal(v)[-1]
    assert rec["affected_paths"][0]["before_sha256"] is None
    assert rec["affected_paths"][0]["after_sha256"]


def test_unknown_tracking_mode_is_refused_before_taking_the_lock(tmp_path):
    v = _vault(tmp_path, 1)
    with pytest.raises(ValueError, match="tracking must be one of"):
        with corpus.mutation(v, writer="t", operation="op", tracking="bogus"):
            pass
    assert not (v / ".okengine" / "corpus" / "lock").exists()
