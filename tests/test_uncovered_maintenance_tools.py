"""Direct coverage for maintenance tools that were previously subprocess-only or untested."""
from __future__ import annotations

import importlib.util
import json
import runpy
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


def _page(path: Path, frontmatter: str, body: str = "Body.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def test_framework_review_queue_and_argument_validation(tmp_path, capsys):
    review = _load("framework_review")
    dash = tmp_path / "wiki" / "dashboards" / "review-queue.md"
    dash.parent.mkdir(parents=True)
    dash.write_text("# Queue\n", encoding="utf-8")

    assert review.main([str(tmp_path)]) == 0
    assert "# Queue" in capsys.readouterr().out
    assert review.main([str(tmp_path), "--decision", "approve"]) == 2
    assert "--decision requires --page" in capsys.readouterr().err
    assert review.main([str(tmp_path), "--approve", "entities/a/acme"]) == 2
    assert "require --by" in capsys.readouterr().err


def test_framework_review_direct_migration_and_decision_paths(
    tmp_path, monkeypatch, capsys
):
    review = _load("framework_review")
    (tmp_path / "schema.yaml").write_text(
        "okf: {required: [type]}\nstrict_types: false\n"
    )
    pages = tmp_path / "wiki" / "entities"
    _page(pages / "flagged.md",
          "type: actor\nneeds_review: true\ncreated: bad\n")
    _page(pages / "signed.md",
          "type: actor\nreviewed_by: Jane\nreviewed_on: 2026-01-01\n"
          "sources: one\n")
    _page(pages / "malformed.md", "a: [broken\n")
    _page(pages / "scalar.md", "- item\n")
    (tmp_path / "wiki" / "operational").mkdir(parents=True)
    _page(tmp_path / "wiki" / "operational" / "ignored.md",
          "type: actor\nneeds_review: true\n")
    assert review.main([str(tmp_path), "--migrate", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "open record(s) created" in out and "historical record(s) created" in out
    assert "unknown=" in out

    import write_server as ws
    target = pages / "flagged.md"
    monkeypatch.setattr(ws, "_safe", lambda _p: target)
    monkeypatch.setattr(ws, "_review_page_state",
                        lambda _p: ({}, "", "", 1, "sha256:x"))
    monkeypatch.setattr(ws, "_resolve_review",
                        lambda *a, **k: {"ok": True, "state": "approved"})
    assert review.main([
        str(tmp_path), "--decision", "approve", "--page", "entities/flagged",
        "--by", "Jane",
    ]) == 0
    assert "state: approved" in capsys.readouterr().out
    monkeypatch.setattr(ws, "_safe", lambda _p: None)
    assert review.main([
        str(tmp_path), "--decision", "approve", "--page", "missing", "--by", "Jane",
    ]) == 1


def test_framework_review_import_failures_and_missing_dashboard(
    tmp_path, monkeypatch, capsys
):
    review = _load("framework_review")
    (tmp_path / "wiki").mkdir()
    monkeypatch.setitem(sys.modules, "write_server", None)
    assert review.main([str(tmp_path), "--migrate"]) == 1
    assert "cannot load" in capsys.readouterr().err
    assert review.main([
        str(tmp_path), "--decision", "approve", "--page", "x", "--by", "Jane",
    ]) == 1
    assert review.main([str(tmp_path)]) == 0
    assert "no review-queue" in capsys.readouterr().out


def test_normalize_publishers_helpers_and_main_apply(tmp_path, monkeypatch, capsys):
    normalize = _load("normalize_publishers")
    mapping = tmp_path / "publishers.json"
    mapping.write_text(json.dumps({
        "_comment": ["ignored"],
        "Cisco Talos": ["Talos", "Cisco Talos"],
    }), encoding="utf-8")
    source = tmp_path / "wiki" / "sources" / "2026" / "report.md"
    _page(source, "type: source\npublisher: 'Talos'\n")

    inverse = normalize.load_inverse_map(mapping)
    assert inverse["Talos"] == "Cisco Talos"
    assert normalize.parse_publisher_value("'Talos'") == "Talos"
    assert normalize.parse_publisher_value("  ") is None
    assert normalize.find_frontmatter_bounds("body") is None
    assert normalize.normalize_file(source, inverse) == ("Talos", "Cisco Talos")

    monkeypatch.setattr(sys, "argv", [
        "normalize_publishers", "--wiki", str(tmp_path),
        "--mapping", str(mapping), "--apply",
    ])
    assert normalize.main() == 0
    assert 'publisher: "Cisco Talos"' in source.read_text()
    assert "APPLY" in capsys.readouterr().out

    collision = tmp_path / "collision.json"
    collision.write_text(json.dumps({"A": ["same"], "B": ["same"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="maps to both"):
        normalize.load_inverse_map(collision)


def test_normalize_publishers_remaining_edge_paths(tmp_path, monkeypatch, capsys):
    normalize=_load("normalize_publishers")
    assert normalize.parse_publisher_value('"Quoted"')=="Quoted"
    assert normalize.parse_publisher_value("plain")=="plain"
    assert normalize.find_frontmatter_bounds("---\nopen") is None
    plain=tmp_path/"plain.md";plain.write_text("body")
    assert normalize.normalize_file(plain,{"x":"y"}) is None
    no_pub=tmp_path/"none.md";_page(no_pub,"type: source\n")
    assert normalize.normalize_file(no_pub,{"x":"y"}) is None
    empty=tmp_path/"empty.md";_page(empty,"publisher: \n")
    assert normalize.normalize_file(empty,{"x":"y"}) is None
    canonical=tmp_path/"same.md";_page(canonical,"publisher: Canon\n")
    assert normalize.normalize_file(canonical,{"Canon":"Canon"}) is None
    normalize.rewrite_file(no_pub,"Never Added")
    assert "Never Added" not in no_pub.read_text()
    normalize.rewrite_file(plain,'A "quote" \\ slash')
    special=tmp_path/"special.md";special.write_text("---\npublisher: old\n---")
    normalize.rewrite_file(special,'A "quote" \\ slash')
    assert 'A \\"quote\\" \\\\ slash' in special.read_text()
    monkeypatch.setattr(sys,"argv",["normalize_publishers","--wiki",str(tmp_path/"missing"),
                                    "--mapping",str(tmp_path/"none.json")])
    assert normalize.main()==2
    mapping=tmp_path/"map.json";mapping.write_text('{"Canon":["Old"]}')
    sources=tmp_path/"wiki/sources";sources.mkdir(parents=True,exist_ok=True)
    for name in ("INDEX.md","_skip.md","INDEX-a.md"):_page(sources/name,"publisher: Old\n")
    _page(sources/"old.md","publisher: Old\n")
    _page(sources/"canonical.md","publisher: Canon\n")
    monkeypatch.setattr(sys,"argv",["normalize_publishers","--wiki",str(tmp_path),
                                    "--mapping",str(mapping)])
    assert normalize.main()==0
    assert "DRY-RUN" in capsys.readouterr().out and "publisher: Old" in (sources/"old.md").read_text()


def test_backfill_source_fields_learns_and_applies(tmp_path, capsys):
    backfill = _load("backfill_source_fields")
    sources = tmp_path / "wiki" / "sources"
    for index in range(5):
        _page(sources / f"known-{index}.md",
              "type: source\npublisher: Example Research\nsource_kind: vendor-blog\n"
              "published: 2026-01-01\n")
    target = sources / "2026-07-23-target.md"
    _page(target, "type: source\npublisher: Example Research\ndate: 2026-07-22\n")
    raw = sources / "raw" / "2026-07-23-raw.md"
    _page(raw, "type: source\npublisher: Example Research\n")

    learned = backfill._learn_publisher_kind(sources)
    assert learned == {"example research": "vendor-blog"}
    backfill.process(tmp_path, True, {"rename", "filename", "classify"})

    text = target.read_text()
    assert "published: 2026-07-22" in text
    assert "source_kind: vendor-blog" in text
    assert "date:" not in text
    assert "source_kind:" not in raw.read_text()
    assert "APPLIED" in capsys.readouterr().out


def test_backfill_source_fields_helpers_scan_and_write_errors(
    tmp_path, monkeypatch, capsys
):
    backfill = _load("backfill_source_fields")
    assert backfill._fm_of("plain") == (None, None)
    fm, match = backfill._fm_of("---\n[\n---\n")
    assert fm is None and match is not None
    fm, _ = backfill._fm_of("---\n- x\n---\n")
    assert fm is None
    assert not backfill._has({}, "x")

    text = "---\ntype: source\n---\nbody"
    fm, match = backfill._fm_of(text)
    assert backfill._rename_key(match, "missing", "new", text) is None

    sources = tmp_path / "wiki" / "sources"
    # Below minimum and non-dominant publishers are not learned.
    for index, kind in enumerate(("a", "a", "a", "b", "b")):
        _page(sources / f"mixed-{index}.md",
              f"type: source\npublisher: Mixed\nsource_kind: {kind}\n")
    _page(sources / "not-source.md", "type: actor\npublisher: Mixed\nsource_kind: a\n")
    _page(sources / "no-publisher.md", "type: source\nsource_kind: a\n")
    assert backfill._learn_publisher_kind(sources) == {}

    target = sources / "2026-01-02-target.md"
    _page(target, "type: source\npublisher: Mixed\nkind: report\n")
    original_write = Path.write_text

    def fail_target(path, *args, **kwargs):
        if path == target:
            raise OSError("readonly")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_target)
    backfill.process(tmp_path, True, {"rename", "filename"})
    assert "cannot write" in capsys.readouterr().err

    monkeypatch.setattr(Path, "write_text", original_write)
    # Exercise phase skips, an inapplicable rename result, and classify-without-a learned kind.
    backfill.process(tmp_path, False, {"filename"})
    monkeypatch.setattr(backfill, "_rename_key", lambda *_a: None)
    backfill.process(tmp_path, False, {"rename", "classify"})


def test_backfill_source_fields_main_and_entrypoint(tmp_path, monkeypatch):
    backfill = _load("backfill_source_fields")
    assert backfill.main(["--root", str(tmp_path), "--phases", "filename"]) == 0
    monkeypatch.setattr(sys, "argv", [
        str(REPO / "scripts" / "backfill_source_fields.py"),
        "--root", str(tmp_path), "--phases", "filename",
    ])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts" / "backfill_source_fields.py"),
                       run_name="__main__")
    assert exc.value.code == 0


def test_backfill_source_fields_tolerates_scan_read_races(tmp_path, monkeypatch):
    backfill = _load("backfill_source_fields")
    sources = tmp_path / "wiki" / "sources"
    vanished = sources / "vanished.md"
    _page(vanished, "type: source\npublisher: X\nsource_kind: report\n")
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("vanished"))
            if path == vanished else original_read(path, *a, **k)
        ),
    )
    assert backfill._learn_publisher_kind(sources) == {}
    backfill.process(tmp_path, False, set())


def test_backfill_typeless_type_is_selective_and_idempotent(tmp_path, monkeypatch, capsys):
    backfill = _load("backfill_typeless_type")
    source = tmp_path / "wiki" / "sources" / "report.md"
    actor = tmp_path / "wiki" / "entities" / "a" / "actor.md"
    ambiguous = tmp_path / "wiki" / "entities" / "a" / "ambiguous.md"
    _page(source, "title: Report\n")
    _page(actor, "title: Actor\ntags: [apt]\n")
    _page(ambiguous, "title: Ambiguous\ntags: [apt, malware]\n")
    monkeypatch.setattr(backfill, "_tag_to_type",
                        lambda _root: {"apt": "actor", "malware": "tool"})

    assert backfill.process(tmp_path, True) == 2
    assert "type: source" in source.read_text()
    assert "type: actor" in actor.read_text()
    assert "type:" not in ambiguous.read_text()
    assert backfill.process(tmp_path, True) == 0
    assert "left for classify-drain" in capsys.readouterr().out


def test_backfill_typeless_helpers_malformed_pages_and_dry_run(
    tmp_path, monkeypatch, capsys
):
    backfill = _load("backfill_typeless_type")
    assert backfill._classify_entity({"tags": "apt"}, {"apt": "actor"}) == "actor"
    assert backfill._classify_entity(
        {"tags": ["apt", "tool"]}, {"apt": "actor", "tool": "malware"}
    ) is None
    assert backfill._decide_type("wiki/sources/a.md", {}, {}) == "source"
    assert backfill._decide_type("wiki/other/a.md", {}, {}) is None

    source_dir = tmp_path / "wiki/sources"
    entity_dir = tmp_path / "wiki/entities"
    source_dir.mkdir(parents=True)
    entity_dir.mkdir()
    (source_dir / "no-fm.md").write_text("body")
    (source_dir / "bad-yaml.md").write_text("---\ninvalid: [\n---\nbody")
    (source_dir / "scalar.md").write_text("---\n- item\n---\nbody")
    (source_dir / "typed.md").write_text("---\ntype: source\n---\nbody")
    candidate = source_dir / "candidate.md"
    candidate.write_text("---\ntitle: Candidate\n---\nbody")
    (entity_dir / "unknown.md").write_text("---\ntags: [unknown]\n---\nbody")
    monkeypatch.setattr(backfill, "_tag_to_type", lambda _root: {})

    assert backfill.process(tmp_path, False) == 1
    assert "type:" not in candidate.read_text()
    output = capsys.readouterr().out
    assert "would set" in output and "dry-run: 1" in output
    assert backfill.main(["--root", str(tmp_path)]) == 0


def test_backfill_typeless_tag_map_failure_is_safe(tmp_path, monkeypatch):
    backfill = _load("backfill_typeless_type")
    monkeypatch.setitem(sys.modules, "schema_lib", None)
    assert backfill._tag_to_type(tmp_path) == {}


def test_backfill_typeless_tag_map_schema_shapes_and_write_error(
    tmp_path, monkeypatch, capsys
):
    backfill = _load("backfill_typeless_type")
    fake = type(sys)("schema_lib")
    fake.merged_schema = lambda _root: {"classify_hints": ["bad"]}
    monkeypatch.setitem(sys.modules, "schema_lib", fake)
    assert backfill._tag_to_type(tmp_path) == {}
    fake.merged_schema = lambda _root: {
        "classify_hints": {
            "actor": [" APT ", "shared"],
            "malware": ["shared", None],
        }
    }
    mapping = backfill._tag_to_type(tmp_path)
    assert mapping["apt"] == "actor" and "shared" not in mapping

    source = tmp_path / "wiki" / "sources" / "target.md"
    _page(source, "title: Target\n")
    original_write = Path.write_text
    monkeypatch.setattr(
        Path, "write_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("readonly"))
            if path == source else original_write(path, *a, **k)
        ),
    )
    assert backfill.process(tmp_path, True) == 0
    assert "cannot write" in capsys.readouterr().err


def test_backfill_typeless_entrypoint(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        str(REPO / "scripts" / "backfill_typeless_type.py"),
        "--root", str(tmp_path),
    ])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts" / "backfill_typeless_type.py"),
                       run_name="__main__")
    assert exc.value.code == 0


def test_backfill_typeless_tolerates_scan_read_race(tmp_path, monkeypatch):
    backfill = _load("backfill_typeless_type")
    vanished = tmp_path / "wiki" / "sources" / "vanished.md"
    _page(vanished, "title: Gone\n")
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("gone"))
            if path == vanished else original_read(path, *a, **k)
        ),
    )
    assert backfill.process(tmp_path, False) == 0


def test_deploy_matrix_inventory_results_and_validation(tmp_path, monkeypatch):
    matrix = _load("deploy_matrix")
    library = tmp_path / "library"
    valid = library / "packs" / "okpack-valid"
    invalid = library / "packs" / "not-a-pack"
    valid.mkdir(parents=True)
    invalid.mkdir()
    (valid / "schema.yaml").write_text("name: valid\n")
    extra = tmp_path / "extra"
    extra.mkdir()
    assert matrix.pack_dirs(library, [extra, tmp_path / "missing"]) == [valid, extra]

    class Result:
        returncode = 1
        stdout = ""
        stderr = "validation failed\n"

    monkeypatch.setattr(matrix, "framework", lambda *args, **kwargs: Result())
    matrix.RESULTS.clear()
    matrix.t1_validate([valid])
    assert matrix.RESULTS == [
        ("validate:okpack-valid", "FAIL", "validation failed"),
    ]
    matrix.record("optional", None, "not shipped")
    assert matrix.RESULTS[-1][1] == "SKIP"


def test_deploy_matrix_failure_cells_and_multi_pack(tmp_path, monkeypatch):
    matrix = _load("deploy_matrix")
    packs = []
    for name in ("okpack-a", "okpack-b", "okpack-c"):
        pack = tmp_path / name
        (pack / "subdomain").mkdir(parents=True)
        (pack / "subdomain/schema.yaml").write_text("types: {}\n")
        (pack / "subdomain/host-schema-additions.yaml").write_text("types: {}\n")
        (pack / "pack.yaml").write_text("trust: public\n")
        packs.append(pack)
    assert matrix._shapes(packs[0]) == ["subtree", "taxonomy"]

    compose_calls = []

    def compose(*args, **_kwargs):
        compose_calls.append(args)
        return type("Result", (), {"returncode": 1, "stdout": "- SCHEMA conflict\n", "stderr": ""})()

    monkeypatch.setattr(matrix, "framework", compose)
    matrix.RESULTS.clear()
    matrix.t1_compose(packs)
    assert len(compose_calls) == 4
    assert all(row[1] == "FAIL" for row in matrix.RESULTS)

    attempts = {}

    def install(host, pack, shape, shapes):
        key = (pack.name, shape)
        attempts[key] = attempts.get(key, 0) + 1
        if pack.name == "okpack-a" and shape == "subtree":
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "install failed"})()
        if attempts[key] == 1:
            (host / "CLAUDE.md").write_text("# no marker\n")
            (host / "schema.yaml").write_text("comments lost\n")
            return type("Result", (), {"returncode": 0, "stdout": "installed", "stderr": ""})()
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "again"})()

    monkeypatch.setattr(matrix, "_install", install)
    matrix.RESULTS.clear()
    matrix.t1_coinstall(packs)
    assert any(row[2] == "install failed" for row in matrix.RESULTS)
    assert any("host comments destroyed" in row[2] for row in matrix.RESULTS)
    assert any(row[0].startswith("coinstall:multipack:") for row in matrix.RESULTS)


def test_deploy_matrix_live_failures_and_main_summary(tmp_path, monkeypatch, capsys):
    matrix = _load("deploy_matrix")
    library = tmp_path / "library"
    (library / "packs").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    stale = work / "okmatrix-demo"
    stale.mkdir()

    result = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "pull bad"})()
    monkeypatch.setattr(matrix, "framework", lambda *_a, **_kw: result)
    matrix.RESULTS.clear()
    matrix.t2_live("demo", library, 1, work)
    assert matrix.RESULTS[-1][2] == "pull failed: pull bad"
    assert not stale.exists()

    def pull(*args, **_kwargs):
        dest = Path(args[2])
        dest.mkdir()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(matrix, "framework", pull)
    monkeypatch.setattr(
        matrix,
        "run",
        lambda *_a, **_kw: type(
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "deploy bad"}
        )(),
    )
    matrix.t2_live("demo", library, 1, work)
    assert "STACK LEFT UP" in matrix.RESULTS[-1][2]
    assert (work / "okmatrix-demo").exists()

    catalog = {"packs": [{"name": "one"}, {"name": "two"}]}
    (library / "catalog.json").write_text(json.dumps(catalog))
    monkeypatch.setattr(matrix, "pack_dirs", lambda *_a: [])
    calls = []
    monkeypatch.setattr(matrix, "t1_validate", lambda packs: calls.append("validate"))
    monkeypatch.setattr(matrix, "t1_conformance", lambda packs: calls.append("conform"))
    monkeypatch.setattr(matrix, "t1_compose", lambda packs: calls.append("compose"))
    monkeypatch.setattr(matrix, "t1_coinstall", lambda packs: calls.append("coinstall"))
    monkeypatch.setattr(
        matrix,
        "t2_live",
        lambda name, *_a: (
            calls.append(name),
            matrix.record(f"live:{name}", name != "two", "failed" if name == "two" else ""),
        ),
    )
    matrix.RESULTS.clear()
    assert matrix.main(
        [
            "--library",
            str(library),
            "--live-all",
            "--workdir",
            str(work),
        ]
    ) == 1
    assert calls == ["validate", "conform", "compose", "coinstall", "one", "two"]
    assert "1 fail" in capsys.readouterr().out


def test_dedup_collision_jobs_and_reference_rewrite(tmp_path):
    dedup = _load("dedup_entity_slugs")
    wiki = tmp_path / "wiki"
    canonical = wiki / "entities" / "a" / "alpha.md"
    _page(canonical, "type: actor\ntitle: Alpha\n")
    collisions = [
        ("entities/actor/alpha", "entities/a/alpha"),
        ("entities/tool/beta", "entities/b/beta"),
        ("entities/malware/beta", "entities/b/beta"),
    ]
    assert dedup.collision_jobs(tmp_path, collisions) == [
        ("entities/actor/alpha", "entities/a/alpha", "entities/a/alpha"),
        ("entities/malware/beta", "entities/tool/beta", "entities/b/beta"),
    ]

    ref = wiki / "briefs" / "daily.md"
    _page(ref, "type: brief\n", "[[entities/actor/alpha|Alpha]]\n")
    assert dedup._rewrite_refs(
        tmp_path, "entities/actor/alpha", "entities/a/alpha",
    ) == 1
    assert "[[entities/a/alpha|Alpha]]" in ref.read_text()
