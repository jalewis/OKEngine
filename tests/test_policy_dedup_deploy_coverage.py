"""Behavioral coverage for release policy, entity dedup, and deployment matrix."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent


def _load(name: str):
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"coverage_{name.replace('-', '_')}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _page(root: Path, key: str, typ: str, body: str, **fields) -> Path:
    path = root / "wiki" / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": typ, "title": key.rsplit("/", 1)[-1], **fields}
    import yaml
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False).strip() + "\n---\n" + body,
        encoding="utf-8",
    )
    return path


def test_release_skip_policy_classifies_results(monkeypatch, capsys):
    policy = _load("check-test-skips")
    forbidden = (
        "SKIPPED [1] tests/test_reader.py:12: could not import 'fastapi': "
        "No module named 'fastapi'\n"
    )
    allowed = "SKIPPED [1] tests/e2e.py:9: requires a live docker stack\n"
    assert policy._forbidden(forbidden) == [
        "tests/test_reader.py: could not import 'fastapi': No module named 'fastapi'",
    ]
    assert policy._forbidden(allowed) == []

    results = iter([
        SimpleNamespace(returncode=0, stdout=allowed, stderr=""),
        SimpleNamespace(returncode=0, stdout=forbidden, stderr=""),
        SimpleNamespace(returncode=1, stdout="1 failed\n", stderr="trace\n"),
        SimpleNamespace(returncode=2, stdout="collection interrupted\n", stderr=""),
        SimpleNamespace(returncode=1, stdout="1 failed\n" + forbidden, stderr=""),
    ])
    monkeypatch.setattr(policy.subprocess, "run", lambda *args, **kwargs: next(results))
    monkeypatch.setattr(sys, "argv", ["check-test-skips"])
    assert policy.main() == 0
    assert "only environmental skips" in capsys.readouterr().out
    assert policy.main() == 1
    assert "missing-dependency skip" in capsys.readouterr().err
    assert policy.main() == 1
    assert "test failures/errors" in capsys.readouterr().err
    assert policy.main() == 2
    assert "only environmental skips" in capsys.readouterr().out
    assert policy.main() == 1
    assert "missing-dependency skip" in capsys.readouterr().err


def test_dedup_classification_checkpoints_and_stops_on_model_error(tmp_path, monkeypatch):
    dedup = _load("dedup_entity_slugs")
    _page(tmp_path, "entities/actor/alpha", "actor", "Legacy actor.")
    _page(tmp_path, "entities/a/alpha", "actor", "Canonical actor.")
    _page(tmp_path, "entities/tool/beta", "tool", "Legacy tool.")
    _page(tmp_path, "entities/b/beta", "tool", "Canonical tool.")
    collisions = [
        ("entities/actor/alpha", "entities/a/alpha"),
        ("entities/tool/beta", "entities/b/beta"),
    ]
    checkpoints = []
    calls = iter(["same-thing", dedup.llm_lib.LLMError("capacity")])

    def classify(*_args, **_kwargs):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(dedup.llm_lib, "classify", classify)
    decisions = dedup.classify_pairs(
        tmp_path, collisions, {}, 60,
        checkpoint=lambda value: checkpoints.append(json.loads(json.dumps(value))),
    )
    assert len(decisions) == 1
    assert next(iter(decisions.values()))["verdict"] == "same-thing"
    assert len(checkpoints) == 1


def test_dedup_apply_merges_and_disambiguates(tmp_path):
    dedup = _load("dedup_entity_slugs")
    _page(tmp_path, "entities/actor/alpha", "actor", "Long legacy body.", tags=["legacy"])
    canonical = _page(
        tmp_path, "entities/a/alpha", "actor", "Short.", sources=["source-a"],
    )
    ref = _page(tmp_path, "briefs/daily", "brief", "[[entities/actor/alpha|Alpha]]")
    _page(tmp_path, "entities/tool/beta", "tool", "Tool beta.")
    _page(tmp_path, "entities/b/beta", "actor", "Actor beta.")
    decisions = {
        "merge": {
            "legacy": "entities/actor/alpha", "other": "entities/a/alpha",
            "canonical": "entities/a/alpha", "verdict": "same-thing",
        },
        "rename": {
            "legacy": "entities/tool/beta", "other": "entities/b/beta",
            "canonical": "entities/b/beta", "verdict": "different-things",
        },
        "uncertain": {
            "legacy": "missing", "other": "also-missing",
            "canonical": "missing", "verdict": "uncertain",
        },
    }
    dedup.apply_decisions(tmp_path, decisions)

    merged = canonical.read_text()
    assert "Long legacy body." in merged
    assert "source-a" in merged and "legacy" in merged
    assert not (tmp_path / "wiki/entities/actor/alpha.md").exists()
    assert "[[entities/a/alpha|Alpha]]" in ref.read_text()
    assert (tmp_path / "wiki/entities/tool/beta-tool.md").is_file()
    assert decisions["merge"]["applied"]
    assert "dedup-165 merge" in (tmp_path / "wiki/log.md").read_text()


def test_deploy_matrix_conformance_compose_and_coinstall(tmp_path, monkeypatch):
    matrix = _load("deploy_matrix")
    public_a = tmp_path / "okpack-a"
    public_b = tmp_path / "okpack-b"
    for pack in (public_a, public_b):
        (pack / "conformance").mkdir(parents=True)
        (pack / "pack.yaml").write_text("trust: public\n")
    (public_a / "conformance/run_check.py").write_text("raise SystemExit(0)\n")
    (public_a / "subdomain").mkdir()
    (public_a / "subdomain/schema.yaml").write_text("types: {}\n")

    matrix.RESULTS.clear()
    matrix.t1_conformance([public_a, public_b])
    assert ("conform:okpack-a:run_check.py", "PASS", "") in matrix.RESULTS
    assert any(row[0] == "conform:okpack-b" and row[1] == "SKIP" for row in matrix.RESULTS)

    monkeypatch.setattr(
        matrix, "framework",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="SAFE: no blocking conflicts\n", stderr="",
        ),
    )
    matrix.t1_compose([public_a, public_b])
    assert any(row[0] == "compose:a+b" and row[1] == "PASS" for row in matrix.RESULTS)

    calls = {}

    def install(host, _pack, _shape, _shapes):
        persona = host / "CLAUDE.md"
        if "## Installed domain:" not in persona.read_text():
            persona.write_text(persona.read_text() + "\n## Installed domain: a\n")
            stdout = "installed"
        else:
            stdout = "nothing to do"
        calls["count"] = calls.get("count", 0) + 1
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(matrix, "_install", install)
    matrix.t1_coinstall([public_a])
    assert calls["count"] == 2
    assert any(row[0].startswith("coinstall:okpack-a") and row[1] == "PASS"
               for row in matrix.RESULTS)


def test_deploy_matrix_live_success_tears_down(tmp_path, monkeypatch):
    matrix = _load("deploy_matrix")
    library = tmp_path / "library"
    library.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    def framework(*args, **kwargs):
        destination = Path(args[2])
        destination.mkdir(parents=True)
        (destination / ".env.example").write_text("PORT=1\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(matrix, "framework", framework)
    monkeypatch.setattr(matrix, "run", run)
    matrix.RESULTS.clear()
    matrix.t2_live("demo", library, 750, work)
    assert matrix.RESULTS == [("live:demo", "PASS", "deployed, verified live, torn down")]
    assert not (work / "okmatrix-demo").exists()
    assert any(command[:2] == ["docker", "compose"] for command in commands)


def test_deploy_matrix_helpers_multipack_and_main_live_all(tmp_path, monkeypatch):
    matrix = _load("deploy_matrix")
    library = tmp_path / "library"
    packs_dir = library / "packs"
    extra = tmp_path / "extra"
    for pack in (packs_dir / "okpack-a", packs_dir / "okpack-b", extra):
        (pack / "subdomain").mkdir(parents=True)
        (pack / "schema.yaml").write_text("types: {}\n")
        (pack / "pack.yaml").write_text("trust: private\n")
        (pack / "subdomain" / "schema.yaml").write_text("types: {}\n")
        (pack / "subdomain" / "host-schema-additions.yaml").write_text("types: {}\n")
    assert len(matrix.pack_dirs(library, [extra, tmp_path / "absent"])) == 3
    assert matrix._shapes(extra) == ["subtree", "taxonomy"]
    shape_empty = tmp_path / "shape-empty"
    shape_empty.mkdir()
    assert matrix._shapes(shape_empty) == []

    calls = []
    monkeypatch.setattr(
        matrix, "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(returncode=0),
    )
    matrix.framework("validate", "pack", env_extra={"EXTRA": "1"})
    assert calls[-1][1]["env"]["EXTRA"] == "1"
    matrix._install(tmp_path, extra, "taxonomy", ["subtree", "taxonomy"])
    assert "--shape" in calls[-1][0]
    matrix._install(tmp_path, extra, "taxonomy", ["taxonomy"])
    assert "--shape" not in calls[-1][0]

    installs = iter([
        SimpleNamespace(returncode=0, stdout="installed", stderr=""),
        SimpleNamespace(returncode=0, stdout="changed\n  - detail", stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="multipack failed"),
    ])
    monkeypatch.setattr(matrix, "_install", lambda *_a, **_k: next(installs))
    matrix.RESULTS.clear()
    matrix.t1_coinstall([extra])
    assert any("not idempotent" in row[2] for row in matrix.RESULTS)

    # Two taxonomy guests also exercise a successful multipack loop to exhaustion.
    monkeypatch.setattr(
        matrix, "_install",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="nothing to do", stderr=""),
    )
    matrix.RESULTS.clear()
    matrix.t1_coinstall([packs_dir / "okpack-a", packs_dir / "okpack-b"])
    assert any(row[0].startswith("coinstall:multipack:") and row[1] == "PASS"
               for row in matrix.RESULTS)

    (library / "catalog.json").write_text(json.dumps({"packs": [{"name": "a"}, {"name": "b"}]}))
    monkeypatch.setattr(matrix, "pack_dirs", lambda *_a: [])
    for name in ("t1_validate", "t1_conformance", "t1_compose", "t1_coinstall"):
        monkeypatch.setattr(matrix, name, lambda *_a: None)
    live = []
    monkeypatch.setattr(matrix, "t2_live", lambda *args: live.append(args))
    matrix.RESULTS.clear()
    assert matrix.main(["--library", str(library), "--live-all", "--workdir", str(tmp_path)]) == 0
    assert [call[0] for call in live] == ["a", "b"]
    live.clear()
    assert matrix.main(["--library", str(library), "--live", "a"]) == 0
    assert [call[0] for call in live] == ["a"]
