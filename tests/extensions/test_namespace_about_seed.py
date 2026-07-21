"""Namespace description seeding — framework_extensions._seed_about (reader about-card).

On enable, an extension's `about.md` is copied to `<pack>/wiki/<owned-ns>/_about.md` so the
reader shows a description card for the namespace. These guard: it seeds to each owned
namespace, is idempotent / non-clobbering (operator edits survive), and no-ops without an
about.md.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
FE = REPO / "scripts" / "framework_extensions.py"


def _fe():
    spec = importlib.util.spec_from_file_location("framework_extensions", FE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_extensions"] = m
    spec.loader.exec_module(m)
    return m


def _ext(tmp_path, about_text="# About\nwhat/why/what-you-see", owns=("foo",)):
    """A fake extension dir with about.md + a schema fragment owning the given namespace(s)."""
    extdir = tmp_path / "ext" / "okengine.foo"
    (extdir / "schema").mkdir(parents=True)
    (extdir / "schema" / "frag.schema.yaml").write_text(
        yaml.safe_dump({"owns": {"namespaces": list(owns), "types": {"foo": {"required": ["type"]}}}}))
    if about_text is not None:
        (extdir / "about.md").write_text(about_text, encoding="utf-8")
    target = {"id": "okengine.foo", "dir": str(extdir),
              "manifest": {"schema": ["schema/frag.schema.yaml"]}}
    return target


def test_seeds_about_to_owned_namespace(tmp_path):
    fe = _fe()
    target = _ext(tmp_path, about_text="# Foo\nthe description")
    fe._seed_about(tmp_path, target)
    dest = tmp_path / "wiki" / "foo" / "_about.md"
    assert dest.is_file()
    assert dest.read_text() == "# Foo\nthe description"


def test_seeds_to_each_owned_namespace(tmp_path):
    fe = _fe()
    target = _ext(tmp_path, owns=("foo", "bar"))
    fe._seed_about(tmp_path, target)
    assert (tmp_path / "wiki" / "foo" / "_about.md").is_file()
    assert (tmp_path / "wiki" / "bar" / "_about.md").is_file()


def test_idempotent_does_not_clobber_operator_edits(tmp_path):
    fe = _fe()
    target = _ext(tmp_path, about_text="shipped version")
    fe._seed_about(tmp_path, target)
    dest = tmp_path / "wiki" / "foo" / "_about.md"
    dest.write_text("operator edited this", encoding="utf-8")   # operator customizes
    fe._seed_about(tmp_path, target)                            # re-run (e.g. re-enable)
    assert dest.read_text() == "operator edited this"          # preserved, not clobbered


def test_noop_without_about_md(tmp_path):
    fe = _fe()
    target = _ext(tmp_path, about_text=None)                    # extension ships no about.md
    fe._seed_about(tmp_path, target)
    assert not (tmp_path / "wiki" / "foo" / "_about.md").exists()


def test_assessment_methodology_has_a_reader_title():
    text = (REPO / "extensions" / "okengine.assessments" / "about.md").read_text()
    assert text.startswith("---\ntitle: How assessments work\n---\n")
