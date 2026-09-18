"""Behavioral coverage for serving qualification and fixture preparation tools."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str):
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_recall_generators_prompt_and_exact_scoring(monkeypatch):
    recall = _load("serving_recall_eval")

    assert len(recall._sha()) == 64
    assert recall._cve().startswith("CVE-")
    assert "-rc" in recall._ver()

    monkeypatch.setattr(
        recall,
        "KINDS",
        [
            ("sha256", lambda: "abc123"),
            ("cve", lambda: "CVE-2026-12345"),
            ("version", lambda: "1.2.3-rc4"),
        ],
    )
    prompt, facts = recall.build_prompt(1, [0.0, 0.5, 1.0])
    assert "REF-00" in prompt
    assert facts["REF-02"]["value"] == "1.2.3-rc4"

    scored = recall.score(
        'prefix {"REF-00": "abc123", "REF-01": 7} suffix',
        {
            "REF-00": {"value": "abc123", "kind": "sha256", "depth": 0.0},
            "REF-01": {"value": "7", "kind": "cve", "depth": 0.5},
        },
    )
    assert all(item["ok"] for item in scored.values())

    fallback = recall.score(
        "not-json but contains wanted",
        {"REF-00": {"value": "wanted", "kind": "version", "depth": 1.0}},
    )
    assert fallback["REF-00"]["ok"] is True
    missing = recall.score(
        "{broken}",
        {"REF-00": {"value": "absent", "kind": "version", "depth": 1.0}},
    )
    assert missing["REF-00"]["ok"] is False
    assert missing["REF-00"]["got"] == ""


def test_recall_ask_and_main_report_success_miss_and_error(monkeypatch, capsys):
    recall = _load("serving_recall_eval")
    call = {}

    def fake_chat(prompt, **kwargs):
        call.update(prompt=prompt, **kwargs)
        return '{"REF-00":"value"}'

    monkeypatch.setattr(recall.llm_lib, "chat", fake_chat)
    assert recall.ask("http://local/v1", "qwen", "document", ["REF-00"], 17)
    assert call["model"] == "qwen"
    assert call["timeout"] == 17
    assert "Labels: REF-00" in call["prompt"]

    attempts = iter(
        [
            json.dumps({f"REF-{i:02d}": ("wrong" if i == 4 else f"value-{i}")
                        for i in range(5)}),
            RuntimeError("offline"),
        ]
    )

    def fake_prompt(_size, depths):
        facts = {
            f"REF-{i:02d}": {
                "value": f"value-{i}",
                "kind": "sha256",
                "depth": depth,
            }
            for i, depth in enumerate(depths)
        }
        return "prompt", facts

    def fake_ask(*_args, **_kwargs):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(recall, "build_prompt", fake_prompt)
    monkeypatch.setattr(recall, "ask", fake_ask)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "serving_recall_eval",
            "--base-url",
            "http://local/v1",
            "--model",
            "qwen",
            "--sizes",
            "10",
            "-n",
            "2",
            "--label",
            "test",
        ],
    )
    assert recall.main() == 0
    output = capsys.readouterr().out
    assert "ERROR offline" in output
    report = json.loads(output.strip().splitlines()[-1])
    assert "MISS size=10 REF-04" in output
    assert report["cells"][0]["exact"] == 4
    assert report["cells"][0]["rate"] == 0.8


def test_prepare_qualification_writes_all_pack_fixtures(tmp_path, monkeypatch, capsys):
    prepare = _load("prepare_backfill_qualification")
    packs = ["alpha", "beta", "gamma", "delta", "epsilon"]
    for index, pack in enumerate(packs):
        base = tmp_path / pack
        base.mkdir()
        (base / "pack.yaml").write_text(f"name: {pack}\n", encoding="utf-8")
        if index < 2:
            ext = base / ".okengine" / "extensions.yaml"
            ext.parent.mkdir()
            ext.write_text("enabled:\n  okengine.predictions: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_backfill_qualification",
            "g-test1",
            "--pack-root",
            str(tmp_path),
            "--allow-unattributable",
            *[item for pack in packs for item in ("--pack", pack)],
        ],
    )

    assert prepare.main() == 0
    assert "across 5 packs" in capsys.readouterr().out
    for pack in packs:
        base = tmp_path / pack
        assert (
            base / "wiki/sources/2026/07/27/qwen-final-control-g-test1.md"
        ).exists()
        queue = (
            base / "wiki/operational/qwen-page-quality-qualification-g-test1.json"
        )
        assert json.loads(queue.read_text())[0]["tier"] == "thin"
        prediction = base / "wiki/predictions/qwen-qualification-g-test1.md"
        assert prediction.exists() is (pack in packs[:2])

    with pytest.raises(SystemExit, match="already exists"):
        prepare.main()


def test_prepare_refuses_missing_pack_checkout(tmp_path, monkeypatch):
    prepare = _load("prepare_backfill_qualification")
    monkeypatch.setattr(sys, "argv", ["prepare", "g1", "--pack-root", str(tmp_path),
                                      "--pack", "missing"])
    with pytest.raises(SystemExit, match="no pack.yaml"):
        prepare.main()


def test_prepare_refuses_unattributable_engine_checkout(tmp_path, monkeypatch):
    prepare = _load("prepare_backfill_qualification")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "pack.yaml").write_text("name: pack\n", encoding="utf-8")
    monkeypatch.setattr(
        prepare.review_context, "collect", lambda _root: {"sha": "a" * 40}
    )
    monkeypatch.setattr(
        prepare.review_context, "problems", lambda _context: ["dirty workspace"]
    )
    monkeypatch.setattr(sys, "argv", [
        "prepare", "g1", "--pack-root", str(tmp_path), "--pack", "pack",
    ])
    with pytest.raises(SystemExit, match="dirty workspace"):
        prepare.main()


@pytest.mark.parametrize("generation", ["", "bad_name", "bad space"])
def test_prepare_qualification_rejects_invalid_generation(
    tmp_path, monkeypatch, generation
):
    prepare = _load("prepare_backfill_qualification")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_backfill_qualification",
            generation,
            "--pack-root",
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit, match="generation must contain"):
        prepare.main()
