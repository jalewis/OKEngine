"""Regression: wiki_schema_audit must classify against the MERGED schema, not the raw pack schema.

Reading `governing_schema(VAULT)` (pack-only) flagged every engine-owned base type
(dashboard/prediction/source/concept/…) as DRIFT on a norm-following vault — advising the operator
to canonize a type the engine already owns (which breaks composition). It also miscounted STIX/legacy
aliases as unsanctioned types. This pins `merged_schema` + `type_aliases`.
"""
import importlib.util
import builtins
import os
import runpy
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "wiki_schema_audit.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    spec = importlib.util.spec_from_file_location("wiki_schema_audit", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["wiki_schema_audit"] = m
    spec.loader.exec_module(m)               # CANONICAL_TYPES/TYPE_ALIASES resolve at import
    return m


def test_base_types_are_canonical_not_drift(tmp_path):
    # a pack that (correctly) declares only its own type + a STIX alias, NOT the engine base types
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type]}\n"
        "type_aliases:\n  threat-actor: actor\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    # merged_schema folds the engine base taxonomy in → base types are canonical, not DRIFT
    for base in ("dashboard", "prediction", "source", "concept"):
        assert base in m.CANONICAL_TYPES, f"{base} should be canonical via merged_schema"
    assert "actor" in m.CANONICAL_TYPES                 # the pack type too
    assert m.TYPE_ALIASES.get("threat-actor") == "actor"   # alias resolves, not counted as drift


def test_schema_lib_bootstraps_packaged_yaml_for_system_python():
    site_packages = str(Path(yaml.__file__).resolve().parents[1])
    # ``-S`` disables site initialization, including editable-install .pth files. The helper must
    # bootstrap both packaged PyYAML and its own source-layout package without ambient PYTHONPATH.
    env = {**os.environ, "OKENGINE_PACKAGED_SITE_PACKAGES": site_packages}
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import schema_lib; print(schema_lib.yaml.__name__)"],
        cwd=REPO / "scripts" / "cron", env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "yaml"


def test_schema_lib_adds_source_checkout_package(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "schema_lib_source_path_test", REPO / "scripts" / "cron" / "schema_lib.py")
    schema_lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema_lib)

    source_root = str(REPO / "src")
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != source_root])

    schema_lib._add_source_checkout_package()

    assert sys.path[0] == source_root


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_and_report_surface_drift_gaps_and_frontmatter_damage(tmp_path):
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type, title, sources]}\n", encoding="utf-8")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write(wiki / "entities" / "good.md",
           "---\ntype: actor\ntitle: Good\nsources: [report]\n---\nBody\n")
    _write(wiki / "entities" / "gap.md",
           "---\ntype: actor\ntitle: Gap\n---\nBody\n")
    _write(wiki / "entities" / "drift.md",
           "---\ntype: mystery\n---\nBody\n")
    _write(wiki / "entities" / "empty.md", "")
    _write(wiki / "entities" / "unclosed.md", "---\ntype: actor\n")
    _write(wiki / "log.md", "operational log without frontmatter\n")
    m = _load(tmp_path)

    counts, files, gaps, failures = m.scan_wiki(wiki)
    assert counts["actor"] == 2 and counts["mystery"] == 1
    assert gaps["actor"][0][1] == ["sources"]
    assert len(files["mystery"]) == 1
    assert len(failures["empty"]) == 1
    assert len(failures["unclosed-fm"]) == 1
    assert "no-fm-block" not in failures  # top-level operational log is suppressed
    report = m.render_report(counts, files, gaps, failures)
    assert "**DRIFT**" in report
    assert "Structural conformance gaps" in report
    assert "2 pages with unparseable frontmatter" in report


def test_commit_gate_and_main_paths(tmp_path, capsys):
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    valid = wiki / "valid.md"
    invalid = wiki / "invalid.md"
    _write(valid, "---\ntype: note\n---\nBody\n")
    _write(invalid, "---\ntype: [broken\n---\nBody\n")
    m = _load(tmp_path)

    assert m.check_paths([valid]) == []
    violations = m.check_paths([invalid])
    assert violations and violations[0][0] == "invalid.md"
    assert m.main(["--check", "--paths", str(valid)]) == 0
    assert "integrity OK" in capsys.readouterr().err
    assert m.main(["--check", "--paths", str(invalid)]) == 1
    assert "violation" in capsys.readouterr().err
    assert m.main([]) == 0
    assert "Schema drift audit" in capsys.readouterr().out


def test_schema_loaders_and_failure_categories(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    monkeypatch.setattr(m.schema_lib, "merged_schema", lambda _vault: {
        "types": {
            "one": {"required": ["type", 3]},
            "two": None,
            "three": {"required": "not-a-list"},
        },
        "operational_types": ["dashboard", 3],
    })
    assert m.load_canonical_types() == {
        "one": ["type", "3"], "two": [], "three": []}
    assert m.load_operational_types() == {"dashboard", "3"}
    monkeypatch.setattr(m.schema_lib, "merged_schema", lambda _vault: {"types": []})
    assert m.load_canonical_types() == {}
    assert m.load_operational_types() == set()

    assert m.categorize_fm_failure("  1|---\n  2|type: x") == "cat-n-prefix"
    assert m.categorize_fm_failure("---\na: x\n---\ntrailing-without-required-newline") == "other"
    assert m.is_operational_no_fm(tmp_path / "wiki/log-1.md", tmp_path / "wiki")
    nested = tmp_path / "wiki/entities/log.md"
    assert not m.is_operational_no_fm(nested, tmp_path / "wiki")
    assert not m.is_operational_no_fm(tmp_path / "wiki/content.md", tmp_path / "wiki")
    assert m._is_operational_no_fm_by_name(Path("lint-today.md"))
    assert not m._is_operational_no_fm_by_name(Path("content.md"))
    original_import = builtins.__import__
    monkeypatch.setattr(
        builtins, "__import__",
        lambda name, *a, **k: (_ for _ in ()).throw(ImportError("no yaml"))
        if name == "yaml" else original_import(name, *a, **k),
    )
    assert m.yaml_validity("type: actor\n") is None


def test_scan_handles_alias_typeless_yaml_invalid_archives_and_read_errors(
        tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "types: {actor: {required: [type]}}\ntype_aliases: {legacy: actor}\n")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write(wiki / "alias.md", "---\ntype: legacy\n---\n")
    _write(wiki / "typeless.md", "---\ntitle: no type\n---\n")
    _write(wiki / "invalid.md", "---\ntype: [broken\n---\n")
    _write(wiki / ".hidden" / "ignored.md", "---\ntype: actor\n---\n")
    unreadable = wiki / "unreadable.md"
    _write(unreadable, "---\ntype: actor\n---\n")
    (wiki / "directory.md").mkdir()
    m = _load(tmp_path)
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda path, *a, **k: (_ for _ in ()).throw(OSError())
                        if path == unreadable else original(path, *a, **k))
    counts, files, gaps, failures = m.scan_wiki(wiki)
    assert counts == Counter({"actor": 1})
    assert files["actor"] == [wiki / "alias.md"]
    assert not gaps
    assert failures["yaml-invalid"] == [wiki / "invalid.md"]


def test_render_report_all_statuses_candidates_and_long_gap_samples(tmp_path):
    (tmp_path / "schema.yaml").write_text("types: {actor: {required: [type, title]}}\n")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    m.VAULT = tmp_path

    paths = [tmp_path / "wiki" / f"candidate-{i}.md" for i in range(6)]
    counts = Counter({"candidate": 6, "actor": 6, "dashboard": 1, "typo": 1})
    files = defaultdict(list, {
        "candidate": paths,
        "actor": paths,
        "dashboard": [paths[0]],
        "typo": [paths[0]],
    })
    gaps = defaultdict(list, {
        "actor": [(p, ["title"]) for p in paths],
    })
    m.CANONICAL_TYPES = {"actor": ["type", "title"]}
    m.OPERATIONAL_TYPES = {"dashboard"}
    report = m.render_report(counts, files, gaps, {})
    assert "No parse failures" in report
    assert "canonical" in report and "operational" in report
    assert "Canonization candidates" in report
    assert "... and 3 more" in report
    assert "One-off / low-count drift" in report
    assert "... and 1 more" in report

    m.CANONICAL_TYPES = {}
    report = m.render_report(Counter({"anything": 1}), {"anything": paths[:1]}, {}, {})
    assert "distribution only" in report
    assert "| `anything` | 1 | present |" in report
    assert "Schema emergence" in report and "None." in report

    m.CANONICAL_TYPES = {"actor": ["type"]}
    candidate_files = [tmp_path / "wiki" / f"short-{i}.md" for i in range(3)]
    report = m.render_report(
        Counter({"candidate-only": 5}),
        {"candidate-only": candidate_files}, {}, {},
    )
    assert "Canonization candidates" in report
    assert "One-off / low-count drift" not in report


def test_check_paths_operational_missing_and_main_missing_wiki(tmp_path, capsys):
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    missing = tmp_path / "missing.md"
    operational = tmp_path / "lint-today.md"
    operational.write_text("plain report")
    content = tmp_path / "content.md"
    content.write_text("plain content")
    assert m.check_paths([missing, operational]) == []
    assert m.check_paths([content]) == [
        ("content.md", "frontmatter unparseable: no-fm-block")]

    m.VAULT = tmp_path / "absent"
    assert m.main([]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_wiki_schema_audit_entrypoint_check_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(MOD), "--check"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(MOD), run_name="__main__")
    assert exc.value.code == 0
