from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "dedup_entity_slugs.py"


def _load():
    spec = importlib.util.spec_from_file_location("dedup_direct", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(root: Path, key: str, typ="actor", body="body", **fm):
    path = root / "wiki" / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"type": typ, **fm}
    import yaml
    path.write_text("---\n" + yaml.safe_dump(fields) + "---\n" + body)
    return path


def test_page_digest_bad_yaml_and_collision_job_shapes(tmp_path):
    m = _load()
    path = tmp_path / "wiki" / "entities" / "bad.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\n[\n---\nbody")
    _, fm, body = m._page(tmp_path, "entities/bad")
    assert fm == {} and body == "\nbody"
    assert "path: x" in m._digest("x", {"name": "N"}, "body")
    # A single legacy to an empty destination is not a collision.
    assert m.collision_jobs(tmp_path, [("entities/old/x", "entities/x/x")]) == []


def test_classify_pairs_skip_budget_vanish_error_uncertain_and_checkpoint(
    tmp_path, monkeypatch, capsys
):
    m = _load()
    canonical = _page(tmp_path, "entities/a/x", body="canonical")
    legacy = _page(tmp_path, "entities/actor/x", body="legacy")
    collisions = [("entities/actor/x", "entities/a/x")]
    existing = {"entities/actor/x::entities/a/x": {"verdict": "same-thing"}}
    assert m.classify_pairs(tmp_path, collisions, existing, 10) == existing

    ticks = iter([0, 2])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(ticks))
    assert m.classify_pairs(tmp_path, collisions, {}, 1) == {}
    assert "time budget reached" in capsys.readouterr().out

    legacy.unlink()
    monkeypatch.setattr(m.time, "monotonic", lambda: 0)
    assert m.classify_pairs(tmp_path, collisions, {}, 10) == {}
    assert "vanished" in capsys.readouterr().out
    legacy = _page(tmp_path, "entities/actor/x", body="legacy")

    monkeypatch.setattr(
        m.llm_lib, "classify",
        lambda *_a, **_k: (_ for _ in ()).throw(m.llm_lib.LLMError("offline")),
    )
    assert m.classify_pairs(tmp_path, collisions, {}, 10) == {}
    assert "endpoint failed" in capsys.readouterr().out

    flushed = []
    monkeypatch.setattr(m.llm_lib, "classify", lambda *_a, **_k: "uncertain")
    decisions = m.classify_pairs(
        tmp_path, collisions, {}, 10, checkpoint=lambda d: flushed.append(dict(d))
    )
    assert next(iter(decisions.values()))["verdict"] == "uncertain"
    assert flushed


def test_rewrite_refs_tolerates_reads_and_apply_all_decision_routes(
    tmp_path, monkeypatch, capsys
):
    m = _load()
    ref = _page(tmp_path, "briefings/ref", body="[[entities/actor/x]]")
    assert m._rewrite_refs(tmp_path, "entities/actor/x", "entities/a/x") == 1
    assert "entities/a/x" in ref.read_text()

    # Same-thing merge: longer body wins, additive lists merge, and references rewrite.
    _page(tmp_path, "entities/a/x", body="short", sources=["b"], tags=["two"])
    _page(tmp_path, "entities/actor/x", body="much longer body", sources=["a"], tags=["one"])
    decisions = {"merge": {
        "legacy": "entities/actor/x", "other": "entities/a/x",
        "canonical": "entities/a/x", "verdict": "same-thing",
    }}
    m.apply_decisions(tmp_path, decisions)
    assert decisions["merge"]["applied"]
    assert not (tmp_path / "wiki" / "entities/actor/x.md").exists()

    # Canonical occupied: different things renames the legacy.
    _page(tmp_path, "entities/a/y", typ="actor")
    _page(tmp_path, "entities/tool/y", typ="tool")
    decisions = {"rename": {
        "legacy": "entities/tool/y", "other": "entities/a/y",
        "canonical": "entities/a/y", "verdict": "different-things",
    }}
    m.apply_decisions(tmp_path, decisions)
    assert (tmp_path / "wiki" / "entities/tool/y-tool.md").exists()

    # Two legacy pages with the same type require an operator.
    _page(tmp_path, "entities/a/z", typ="actor")
    _page(tmp_path, "entities/b/z", typ="actor")
    decisions = {"operator": {
        "legacy": "entities/a/z", "other": "entities/b/z",
        "canonical": "entities/z/z", "verdict": "different-things",
    }, "missing": {
        "legacy": "entities/missing", "other": "entities/b/z",
        "canonical": "entities/z/z", "verdict": "same-thing",
    }, "uncertain": {
        "legacy": "entities/a/z", "other": "entities/b/z",
        "canonical": "entities/z/z", "verdict": "uncertain",
    }, "applied": {
        "legacy": "x", "canonical": "x", "verdict": "same-thing",
        "applied": "today",
    }}
    m.apply_decisions(tmp_path, decisions)
    assert "operator:" in capsys.readouterr().out


def test_disambiguation_collision_and_main_classify_apply_entrypoint(
    tmp_path, monkeypatch, capsys
):
    m = _load()
    _page(tmp_path, "entities/a/x", typ="actor")
    _page(tmp_path, "entities/a/x-actor", typ="actor")
    decisions = {"d": {
        "legacy": "entities/a/x", "other": "entities/a/x",
        "canonical": "entities/a/x", "verdict": "different-things",
    }}
    m.apply_decisions(tmp_path, decisions)
    assert not decisions["d"].get("applied")

    dpath = tmp_path / "decisions.json"
    monkeypatch.setattr(m.okf_migrate, "build_map", lambda *_a: ({}, []))
    assert m.main(["--root", str(tmp_path), "--decisions", str(dpath)]) == 0
    assert dpath.is_file() and "decisions ->" in capsys.readouterr().out
    assert m.main([
        "--root", str(tmp_path), "--decisions", str(dpath), "--apply",
    ]) == 0

    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT), "--root", str(tmp_path), "--decisions", str(dpath),
    ])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0


def test_plain_page_read_error_two_legacy_routes_and_flush(tmp_path, monkeypatch):
    m = _load()
    plain = tmp_path / "wiki/plain.md"
    plain.parent.mkdir(parents=True)
    plain.write_text("body only")
    assert m._page(tmp_path, "plain")[1:] == ({}, "body only")

    unreadable = _page(tmp_path, "unreadable", body="[[old]]")
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == unreadable else original(self, *a, **k),
    )
    assert m._rewrite_refs(tmp_path, "old", "new") == 0
    monkeypatch.setattr(Path, "read_text", original)

    # Different-type legacies: shorter page is renamed, unless its target exists.
    _page(tmp_path, "entities/a/x", typ="actor", body="short")
    _page(tmp_path, "entities/b/x", typ="tool", body="a much longer body")
    decision = {"d": {"legacy": "entities/a/x", "other": "entities/b/x",
                       "canonical": "entities/x/x", "verdict": "different-things"}}
    m.apply_decisions(tmp_path, decision)
    assert (tmp_path / "wiki/entities/a/x-actor.md").is_file()
    assert decision["d"]["applied"]

    _page(tmp_path, "entities/a/y", typ="actor", body="short")
    _page(tmp_path, "entities/b/y", typ="tool", body="longer body")
    _page(tmp_path, "entities/a/y-actor", typ="actor")
    blocked = {"d": {"legacy": "entities/a/y", "other": "entities/b/y",
                      "canonical": "entities/y/y", "verdict": "different-things"}}
    m.apply_decisions(tmp_path, blocked)
    assert not blocked["d"].get("applied")

    # Exercise the main-owned per-decision checkpoint writer.
    dpath = tmp_path / "flush.json"
    monkeypatch.setattr(m.okf_migrate, "build_map", lambda *_a: ({}, [("old", "new")]))
    def classify(_root, _collisions, decisions, _budget, checkpoint=None):
        decisions["written"] = {"verdict": "uncertain"}
        checkpoint(decisions)
        return decisions
    monkeypatch.setattr(m, "classify_pairs", classify)
    assert m.main(["--root", str(tmp_path), "--decisions", str(dpath)]) == 0
    assert json.loads(dpath.read_text())["written"]["verdict"] == "uncertain"


def test_classify_without_checkpoint_and_merge_sparse_lists(tmp_path, monkeypatch):
    m = _load()
    _page(tmp_path, "entities/a/x", body="canonical", tags=[])
    _page(tmp_path, "entities/actor/x", body="legacy", tags=["tagged"])
    monkeypatch.setattr(m.llm_lib, "classify", lambda *_a, **_k: "same-thing")
    decisions = m.classify_pairs(
        tmp_path, [("entities/actor/x", "entities/a/x")], {}, 10,
    )
    assert next(iter(decisions.values()))["verdict"] == "same-thing"
    m.apply_decisions(tmp_path, decisions)
    assert "tagged" in (tmp_path / "wiki/entities/a/x.md").read_text()
