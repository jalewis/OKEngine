"""One namespace-exclusion grammar must govern write, index, reader, and cockpit surfaces."""

import importlib.util
from pathlib import Path

import pytest
import yaml

from okengine.schema_exclusions import exclusion_globs_from_schema, excluded_namespaces_from_schema
from tools import schema_validator


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", ["operational", "wiki/operational/", "/wiki/operational/"])
def test_supported_spellings_resolve_to_one_top_level_namespace(value):
    schema = {"exclude": [value]}
    assert excluded_namespaces_from_schema(schema) == {"operational"}
    assert schema_validator._excluded("wiki/operational/status.md", schema)
    assert not schema_validator._excluded("wiki/assessments/operational.md", schema)


@pytest.mark.parametrize("value", [
    "assessments/staging", "wiki/assessments/staging/", "", "../wiki/raw/",
])
def test_ambiguous_subtree_or_pattern_exclusion_fails_loudly(value):
    with pytest.raises(ValueError, match="not a namespace exclusion"):
        excluded_namespaces_from_schema({"exclude": [value]})


def test_explicit_globs_remain_page_scoped_and_never_become_fake_namespaces():
    schema = {"exclude": ["*.tmp.md", "private/**"]}
    assert excluded_namespaces_from_schema(schema) == set()
    assert exclusion_globs_from_schema(schema) == ["*.tmp.md", "private/**"]
    assert schema_validator._excluded("entities/a/draft.tmp.md", schema)
    assert schema_validator._excluded("private/nested/page.md", schema)


@pytest.mark.parametrize("value", [r"private\\*.md", "../*.md", "safe/../*.md"])
def test_unsafe_globs_fail_loudly(value):
    with pytest.raises(ValueError, match="unsafe schema exclude glob"):
        exclusion_globs_from_schema({"exclude": [value]})


def test_namespace_and_glob_parsers_process_every_entry_in_order():
    schema = {"exclude": ["*.tmp.md", "operational", "private/**", "dashboards"]}

    assert excluded_namespaces_from_schema(schema) == {"operational", "dashboards"}
    assert exclusion_globs_from_schema(schema) == ["*.tmp.md", "private/**"]


@pytest.mark.parametrize("value", ["bad namespace", "bad:name", r"bad\\namespace"])
def test_invalid_bare_namespace_characters_fail_loudly(value):
    with pytest.raises(ValueError, match="not a namespace exclusion"):
        excluded_namespaces_from_schema({"exclude": [value]})


def test_reader_uses_the_same_parser_and_does_not_drop_a_parent_for_bad_subtree(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "reader_directory_index_exclusions", ROOT / "okengine-reader/directory_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = tmp_path / "schema.yaml"
    schema.write_text("exclude: [assessments/staging]\n", encoding="utf-8")

    excluded, _cache = module.excluded_namespaces(
        cache=(0.0, frozenset()), ttl=0, schema_path=lambda: schema,
        yaml_module=yaml, surfaced=frozenset(),
    )

    assert excluded == frozenset(), "malformed subtree must not silently hide all assessments"


def test_reader_ignores_non_list_exclusion_without_hiding_namespaces(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "reader_directory_index_nonlist", ROOT / "okengine-reader/directory_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = tmp_path / "schema.yaml"
    schema.write_text("exclude: operational\n", encoding="utf-8")

    excluded, _cache = module.excluded_namespaces(
        cache=(0.0, frozenset()), ttl=0, schema_path=lambda: schema,
        yaml_module=yaml, surfaced=frozenset(),
    )
    assert excluded == frozenset()


def test_reader_namespace_resolution_uses_composed_schema_and_subdomain_offset(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "reader_directory_index_namespaces", ROOT / "okengine-reader/directory_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    generated = tmp_path / ".okengine"
    generated.mkdir()
    (generated / "composed-schema.yaml").write_text(
        "partitioning:\n"
        "  namespaces:\n"
        "    findings: {strategy: flat}\n"
        "    reports: {strategy: flat}\n"
        "exclude: [wiki/examples/]\n",
        encoding="utf-8",
    )
    (wiki / "acme-sub").mkdir()
    (wiki / "acme-sub" / "schema.yaml").write_text("types: {}\n", encoding="utf-8")
    assert module.namespace_dirs(wiki / "acme-sub/examples/x.md", wiki) == {"examples"}
    assert module.namespace_dirs(
        wiki / "acme-sub/findings/examples/x.md", wiki
    ) == {"findings"}
    assert module.namespace_dirs(wiki / "reports/examples/x.md", wiki) == {"reports"}
    assert module.namespace_dirs(wiki / "procedures/examples/x.md", wiki) == {"procedures"}


def test_reader_namespace_helpers_fail_open_on_io_and_bad_entries(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "reader_directory_index_edges", ROOT / "okengine-reader/directory_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bad = tmp_path / "bad.yaml"
    bad.write_text("exclude: [assessments/staging]\n", encoding="utf-8")
    assert module._declared_namespaces(str(bad), bad.stat().st_mtime_ns) == frozenset()
    assert module._declared_namespaces(str(tmp_path / "missing"), 0) == frozenset()

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    schema = tmp_path / "schema.yaml"
    schema.write_text("types: {}\n", encoding="utf-8")
    original_stat = Path.stat
    monkeypatch.setattr(
        Path, "stat",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("race"))
        if path == schema else original_stat(path, *args, **kwargs),
    )
    assert module._root_namespaces(wiki) == frozenset()
    assert module.reserved_path(tmp_path / "outside.md", wiki) is False


@pytest.mark.parametrize("parser", [excluded_namespaces_from_schema, exclusion_globs_from_schema])
def test_schema_exclude_must_be_a_list(parser):
    with pytest.raises(ValueError, match="must be a list"):
        parser({"exclude": "operational"})
